"""Real build/test gates, serialized per workspace with cancellable processes."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
import uuid

from .common import EngineError, checkpoint, digest, file_hash, run, tool, write_json


def workspace_snapshot(root: Path) -> str:
    excluded = {".git", ".compile-fast", ".gradle", "target", "build", ".venv", "node_modules", "__pycache__"}
    entries = []
    for parent, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in excluded)
        for name in sorted(files):
            checkpoint()
            path = Path(parent) / name
            if path.is_symlink():
                entries.append((path.relative_to(root).as_posix(), os.readlink(path)))
            elif path.is_file():
                entries.append((path.relative_to(root).as_posix(), file_hash(path)))
    return digest(entries)


@contextmanager
def build_lock(root: Path, timeout: float):
    path = root / ".compile-fast/build.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    stream.seek(0, 2)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    started = time.monotonic()
    acquired = False
    try:
        while not acquired:
            checkpoint()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() - started > timeout:
                    raise EngineError("Workspace build lock timed out.", "INFRA_ERROR", "WORKSPACE_BUSY")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def default_gates(root: Path) -> list[dict]:
    config = root / ".compile-fast/gates.json"
    if config.is_file():
        gates = json.loads(config.read_text(encoding="utf-8"))
        if not isinstance(gates, list) or not gates:
            raise EngineError("gates.json must be a nonempty array.", code="INVALID_GATES")
        for gate in gates:
            if not isinstance(gate, dict) or not isinstance(gate.get("name"), str):
                raise EngineError("Each gate needs a name.", code="INVALID_GATES")
            if not isinstance(gate.get("command"), list) or not gate["command"] or not all(
                    isinstance(x, str) and x for x in gate["command"]):
                raise EngineError("Gate command must be a nonempty argv array.", code="INVALID_GATES")
        return gates
    if (root / "pom.xml").is_file():
        return [{"name": "maven-verify", "command": ["mvn", "-B", "clean", "verify"]}]
    if any((root / name).is_file() for name in ("build.gradle", "build.gradle.kts")):
        wrapper = root / ("gradlew.bat" if os.name == "nt" else "gradlew")
        executable = str(wrapper) if wrapper.is_file() else "gradle"
        return [{"name": "gradle-build", "command": [executable, "--no-daemon", "clean", "build"]}]
    raise EngineError("No normal build detected. Configure .compile-fast/gates.json.", code="GATE_MISSING")


def verify_build(root: Path, *, timeout=600, gates=None, model_path=None) -> dict:
    root = root.resolve()
    identity, started = str(uuid.uuid4()), time.monotonic()
    result = {"verificationId": identity, "workspaceId": str(root), "verificationLevel": "L3",
              "verificationScope": ["workspace:configured-build-and-test-gates"], "status": "UNKNOWN",
              "diagnostics": [], "gates": [], "limitations": ["Only configured gates are proven; business behavior is not inferred."]}
    try:
        commands = gates if gates is not None else default_gates(root)
        if model_path and model_path.is_file() and gates is None and not (root / ".compile-fast/gates.json").is_file():
            model = json.loads(model_path.read_text(encoding="utf-8"))
            if model.get("origin") == "maven-effective-pom":
                commands[0]["command"][1:1] = model.get("mavenArgs", [])
            elif model.get("origin") == "gradle-javacompile":
                commands[0]["command"][1:1] = model.get("gradleArgs", [])
        with build_lock(root, timeout):
            before = workspace_snapshot(root)
            config = root / ".compile-fast/gates.json"
            config_hash = file_hash(config) if config.is_file() else None
            for gate in commands:
                checkpoint()
                args = list(gate["command"])
                args[0] = tool(args[0])
                response = run(args, root, timeout)
                stdout = root / ".compile-fast/logs" / f"{identity}-{len(result['gates'])}.log"
                stdout.parent.mkdir(parents=True, exist_ok=True)
                raw = response.stdout + response.stderr
                stdout.write_text(raw, encoding="utf-8")
                status = "PASS" if response.returncode == 0 else "FAIL"
                infrastructure = any(token in raw for token in (
                    "Could not resolve", "Could not transfer artifact", "PluginResolutionException",
                    "Could not find or load main class", "Permission denied: connect", "Could not GET",
                    "java.lang.OutOfMemoryError", "Could not determine java version"))
                if infrastructure:
                    status = "UNKNOWN"
                result["gates"].append({"name": gate["name"], "command": args, "status": status,
                                        "exitCode": response.returncode, "log": str(stdout)})
                if status != "PASS":
                    result["status"] = status
                    result["diagnostics"].append({"errorCode": "BUILD_CONTEXT" if infrastructure else "BUILD_FAILED",
                                                  "message": raw[-12000:], "gate": gate["name"]})
                    break
            else:
                result["status"] = "PASS"
            if before != workspace_snapshot(root) or config_hash != (file_hash(config) if config.is_file() else None):
                raise EngineError("Workspace or gate configuration changed during build.", code="STALE_WORKSPACE")
            result["sourceSnapshot"] = before
    except EngineError as exc:
        result["status"] = exc.status
        result["diagnostics"].append({"errorCode": exc.code, "message": str(exc)})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result["status"] = "INFRA_ERROR"
        result["diagnostics"].append({"errorCode": "GATE_ERROR", "message": str(exc)})
    result["durationMs"] = round((time.monotonic() - started) * 1000)
    write_json(root / ".compile-fast/results" / f"{identity}.json", result)
    return result
