"""Authenticated workspace-scoped HTTP verification queue with durable task state."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid

from .common import EngineError, cancellation, write_json
from .pipeline import execute


class Scheduler:
    def __init__(self, workspaces: dict[str, Path], state: Path, *, workers=4, allow_build=False, debounce=0.15):
        self.workspaces = {name: path.resolve() for name, path in workspaces.items()}
        if not self.workspaces or any(not p.is_dir() for p in self.workspaces.values()):
            raise ValueError("Register at least one existing workspace")
        state.mkdir(parents=True, exist_ok=True)
        self.database = state / "tasks.sqlite3"
        self.allow_build, self.debounce = allow_build, debounce
        self.lock = threading.RLock()
        self.events, self.latest = {}, {}
        self.workspace_locks = {str(root): threading.Lock() for root in self.workspaces.values()}
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="compile-fast")
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, workspace TEXT, state TEXT, request TEXT, result TEXT, created REAL)")
            for row in db.execute("SELECT id FROM tasks WHERE state IN ('QUEUED','RUNNING')").fetchall():
                result = {"verificationId": row[0], "status": "UNKNOWN", "diagnostics": [
                    {"errorCode": "SERVICE_RESTARTED", "message": "Interrupted job was not resumed; submit the current workspace again."}]}
                db.execute("UPDATE tasks SET state='CANCELLED', result=? WHERE id=?", (json.dumps(result), row[0]))

    @contextmanager
    def db(self):
        database = sqlite3.connect(self.database, timeout=30)
        try:
            with database:
                yield database
        finally:
            database.close()

    def get(self, identity):
        with self.db() as db:
            row = db.execute("SELECT workspace,state,request,result,created FROM tasks WHERE id=?", (identity,)).fetchone()
        if not row:
            return None
        return {"verificationId": identity, "workspaceId": row[0], "executionState": row[1],
                "request": json.loads(row[2]), "result": json.loads(row[3]) if row[3] else None, "createdAt": row[4]}

    def submit(self, request: dict):
        if not isinstance(request, dict):
            raise ValueError("Expected a JSON object")
        allowed = {"workspaceId", "revision", "commitId", "changeId", "projectModelVersion", "changedFiles",
                   "verificationLevel", "allowBuild", "agent", "recipeId", "timeout"}
        if request.keys() - allowed:
            raise ValueError("Unsupported request fields: " + ", ".join(sorted(request.keys() - allowed)))
        for key in ("workspaceId", "revision", "commitId", "changeId", "projectModelVersion", "agent", "recipeId"):
            if key in request and (not isinstance(request[key], str) or len(request[key]) > 1024):
                raise ValueError(f"{key} must be a string up to 1024 characters")
        workspace = request.get("workspaceId")
        if workspace not in self.workspaces:
            raise ValueError("Unknown workspaceId")
        level = request.get("verificationLevel", "auto")
        if level not in {"L1", "L2", "L3", "auto"}:
            raise ValueError("Invalid verificationLevel")
        if "allowBuild" in request and not isinstance(request["allowBuild"], bool):
            raise ValueError("allowBuild must be boolean")
        if (request.get("allowBuild") or level == "L3") and not self.allow_build:
            raise ValueError("Server was not started with --allow-builds")
        timeout = request.get("timeout", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
            raise ValueError("timeout must be between 0 and 3600 seconds")
        if "changedFiles" in request and (not isinstance(request["changedFiles"], list)
                or len(request["changedFiles"]) > 10000 or not all(isinstance(p, str) for p in request["changedFiles"])):
            raise ValueError("changedFiles must be a list of paths")
        identity, event = str(uuid.uuid4()), threading.Event()
        with self.lock:
            if len(self.events) >= 128:
                raise ValueError("Queue capacity reached")
            root_key = str(self.workspaces[workspace])
            previous = self.latest.get(root_key)
            if previous:
                self.cancel(previous, superseded=True)
            self.latest[root_key] = identity
            self.events[identity] = event
            with self.db() as db:
                db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)", (identity, workspace, "QUEUED", json.dumps(request), None, time.time()))
            self.executor.submit(self._work, identity, request, event)
        return self.get(identity)

    def cancel(self, identity, superseded=False):
        with self.lock:
            task = self.get(identity)
            if task is None:
                return None
            if task["executionState"] in {"QUEUED", "RUNNING"}:
                event = self.events.get(identity)
                if event:
                    event.set()
                state = "SUPERSEDED" if superseded else "CANCELLED"
                result = {"verificationId": identity, "status": "UNKNOWN", "diagnostics": [
                    {"errorCode": state, "message": "A newer request superseded this job." if superseded else "Job cancelled."}]}
                with self.db() as db:
                    db.execute("UPDATE tasks SET state=?,result=? WHERE id=?", (state, json.dumps(result), identity))
            return self.get(identity)

    def _work(self, identity, request, event):
        root = self.workspaces[request["workspaceId"]]
        lock = self.workspace_locks[str(root)]
        acquired = False
        try:
            if event.wait(self.debounce):
                return
            while not event.is_set() and not acquired:
                acquired = lock.acquire(timeout=.1)
            if event.is_set():
                return
            with self.lock, self.db() as db:
                if event.is_set():
                    return
                db.execute("UPDATE tasks SET state='RUNNING' WHERE id=?", (identity,))
            with cancellation(event):
                result = execute(root, root / ".compile-fast/model.json",
                                 level=request.get("verificationLevel", "auto"),
                                 allow_build=self.allow_build and (request.get("allowBuild", False) or request.get("verificationLevel") == "L3"),
                                 changed=request.get("changedFiles"), timeout=request.get("timeout", 120),
                                 change_id=request.get("changeId"), agent=request.get("agent"), recipe_id=request.get("recipeId"),
                                 expected_model=request.get("projectModelVersion"),
                                 expected_revision=request.get("revision") or request.get("commitId"), verification_id=identity)
            with self.lock, self.db() as db:
                if not event.is_set():
                    db.execute("UPDATE tasks SET state='COMPLETED',result=? WHERE id=?", (json.dumps(result), identity))
        except Exception as exc:
            with self.lock, self.db() as db:
                if not event.is_set():
                    result = {"verificationId": identity, "status": "INFRA_ERROR", "diagnostics": [
                        {"errorCode": "WORKER_ERROR", "message": str(exc)}]}
                    db.execute("UPDATE tasks SET state='COMPLETED',result=? WHERE id=?", (json.dumps(result), identity))
        finally:
            if event.is_set():
                cancelled = self.get(identity)
                if cancelled and cancelled["result"]:
                    write_json(root / ".compile-fast/results" / f"{identity}.json", cancelled["result"])
            if acquired:
                lock.release()
            with self.lock:
                self.events.pop(identity, None)

    def close(self):
        with self.lock:
            for identity in list(self.events):
                self.cancel(identity)
        self.executor.shutdown(wait=True, cancel_futures=True)


def make_server(scheduler: Scheduler, token: str, host="127.0.0.1", port=8765):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CompileFast/0.2"

        def log_message(self, *_):
            pass

        def send(self, code, body):
            data = json.dumps(body, ensure_ascii=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send(401, {"error": "Bearer token required"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            parts = urlsplit(self.path).path.strip("/").split("/")
            if parts == ["health"]:
                self.send(200, {"status": "ok"})
            elif len(parts) in (3, 4) and parts[:2] == ["api", "verifications"]:
                task = scheduler.get(parts[2])
                if task is None:
                    self.send(404, {"error": "Unknown verification"})
                elif len(parts) == 3:
                    self.send(200, task)
                elif parts[3] == "diagnostics":
                    self.send(200, {"verificationId": parts[2], "executionState": task["executionState"],
                                    "diagnostics": (task["result"] or {}).get("diagnostics", [])})
                else:
                    self.send(404, {"error": "Unknown route"})
            else:
                self.send(404, {"error": "Unknown route"})

        def do_POST(self):
            if not self.authorized():
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 1024 * 1024:
                    self.send(413, {"error": "Request too large"})
                    return
                self.connection.settimeout(10)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("Truncated request")
                data = json.loads(raw or b"{}")
                parts = urlsplit(self.path).path.strip("/").split("/")
                if parts == ["api", "verifications"]:
                    self.send(202, scheduler.submit(data))
                elif len(parts) == 4 and parts[:2] == ["api", "verifications"] and parts[3] == "cancel":
                    task = scheduler.cancel(parts[2])
                    self.send(200 if task else 404, task or {"error": "Unknown verification"})
                else:
                    self.send(404, {"error": "Unknown route"})
            except (ValueError, TypeError, OSError) as exc:
                self.send(400, {"error": str(exc)})

    return ThreadingHTTPServer((host, port), Handler)


def serve(workspaces, state, host="127.0.0.1", port=8765, workers=4, allow_build=False):
    state.mkdir(parents=True, exist_ok=True)
    token_file = state / "auth.token"
    if not token_file.exists():
        token_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        token_file.chmod(0o600)
    token = token_file.read_text(encoding="utf-8").strip()
    if len(token) < 24:
        raise ValueError("Server token must contain at least 24 characters")
    scheduler = Scheduler(workspaces, state, workers=workers, allow_build=allow_build)
    server = make_server(scheduler, token, host, port)
    print(json.dumps({"url": f"http://{host}:{server.server_port}", "tokenFile": str(token_file),
                      "workspaceIds": list(workspaces)}), flush=True)
    try:
        server.serve_forever(poll_interval=.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        scheduler.close()
