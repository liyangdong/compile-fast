"""L1/L2/L3 orchestration and persistent, bounded agent repair evidence."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import closing

from .common import EngineError, checkpoint, digest, process_metrics, write_json
from .engine import verify
from .gates import verify_build, workspace_snapshot
from .model import load
from .source import build_index, fast_verify, impact


def record_attempt(root: Path, result: dict, change_id: str | None, agent: str | None, max_attempts=3):
    if not change_id:
        return
    path = root / ".compile-fast/attempts.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    signature = digest([(d.get("file"), d.get("errorCode"), d.get("message")) for d in result["diagnostics"]])
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS attempts (seq INTEGER PRIMARY KEY AUTOINCREMENT, change_id TEXT, agent TEXT, status TEXT, signature TEXT, verification_id TEXT)")
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT status, signature FROM attempts WHERE change_id=? AND agent=? ORDER BY seq DESC LIMIT ?",
                              (change_id, agent or "", max_attempts - 1)).fetchall()
        count = 1
        if result["status"] == "FAIL":
            for status, old_signature in previous:
                if status != "FAIL" or old_signature != signature:
                    break
                count += 1
        db.execute("INSERT INTO attempts(change_id,agent,status,signature,verification_id) VALUES (?,?,?,?,?)",
                   (change_id, agent or "", result["status"], signature, result["verificationId"]))
    result["repairPolicy"] = {"consecutiveSameFailures": count if result["status"] == "FAIL" else 0,
                              "maxAttempts": max_attempts,
                              "action": "STOP_AND_REVIEW" if result["status"] == "FAIL" and count >= max_attempts else "CONTINUE"}


def _execute(root: Path, model_path: Path, *, level="auto", allow_build=False, changed=None, base=None,
            timeout=120, javac="javac", use_cache=True, change_id=None, agent=None,
            recipe_id=None, expected_model=None, expected_revision=None, verification_id=None) -> dict:
    root, model_path = root.resolve(), model_path.resolve()
    identity, started = verification_id or str(uuid.uuid4()), time.monotonic()
    stages = []
    result = {"schemaVersion": 1, "verificationId": identity, "workspaceId": str(root), "status": "UNKNOWN",
              "verificationLevel": level, "verificationScope": [], "diagnostics": []}
    try:
        if level not in {"L1", "L2", "L3", "auto"}:
            raise EngineError("Unsupported verification level.", code="INVALID_LEVEL")
        before = workspace_snapshot(root)
        if expected_revision:
            from .common import run, tool
            head = run([tool("git"), "rev-parse", "HEAD"], root)
            if head.returncode or head.stdout.strip() != expected_revision:
                raise EngineError("Requested revision is not the current checkout.", code="REVISION_MISMATCH")
        if expected_model:
            if digest(load(root, model_path)) != expected_model:
                raise EngineError("Requested project model version does not match.", code="MODEL_VERSION_MISMATCH")
        if level == "L3":
            if not allow_build:
                raise EngineError("L3 requires explicit --allow-build / server build authorization.", code="BUILD_NOT_AUTHORIZED")
            result = verify_build(root, timeout=timeout, model_path=model_path)
            stages.append(result.copy())
        else:
            if level in {"auto", "L1"}:
                try:
                    model = load(root, model_path)
                    previous_path = root / ".compile-fast/index/sources.json"
                    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.is_file() else {}
                    index = build_index(root, model)
                    report = impact(previous, index, model)
                    static = {"status": "UNKNOWN" if any(not v["syntaxValid"] for v in index["files"].values()) else "PASS",
                              "verificationLevel": "L1", "verificationScope": sorted(index["files"]),
                              "diagnostics": [d for v in index["files"].values() for d in v["diagnostics"]],
                              "requiresL2": True, "impact": report,
                              "limitations": ["Syntax/reference candidates do not prove Java type correctness."]}
                    if (root / ".compile-fast/rewrite.json").is_file():
                        from .rewrite import index as rewrite_index
                        try:
                            semantic = rewrite_index(root, model, timeout)
                            static["semanticIndex"] = {"backend": "OpenRewrite", "contextKey": semantic["contextKey"],
                                                       "cacheHit": semantic["cacheHit"], "fileCount": len(semantic["files"])}
                            if any(v["unresolvedCalls"] for v in semantic["files"].values()):
                                static["status"] = "UNKNOWN"
                        except EngineError as exc:
                            static["status"] = "UNKNOWN"
                            static["diagnostics"].append({"errorCode": exc.code, "message": str(exc)})
                except EngineError as exc:
                    static = {"status": exc.status, "verificationLevel": "L1", "verificationScope": [],
                              "diagnostics": [{"errorCode": exc.code, "message": str(exc)}], "requiresL2": True}
                stages.append(static)
                result = static.copy()
            if level != "L1":
                checkpoint()
                result = verify(root, model_path, changed=changed, base=base, timeout=timeout, javac=javac,
                                use_cache=use_cache, change_id=change_id, agent=agent)
                stages.append(result.copy())
                if level == "auto" and result["status"] in {"PASS", "UNKNOWN"} and allow_build:
                    checkpoint()
                    result = verify_build(root, timeout=timeout, model_path=model_path)
                    stages.append(result.copy())
        if before != workspace_snapshot(root):
            raise EngineError("Workspace changed during the verification pipeline.", code="STALE_WORKSPACE")
    except EngineError as exc:
        result["status"] = exc.status
        result.setdefault("diagnostics", []).append({"errorCode": exc.code, "message": str(exc)})
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        result["status"] = "INFRA_ERROR"
        result.setdefault("diagnostics", []).append({"errorCode": "PIPELINE_ERROR", "message": str(exc)})
    result.update({"schemaVersion": 1, "verificationId": identity, "workspaceId": str(root), "stages": stages,
                   "changeId": change_id, "attribution": {"agent": agent, "recipeId": recipe_id},
                   "durationMs": round((time.monotonic() - started) * 1000)})
    record_attempt(root, result, change_id, agent)
    if result.get("repairPolicy", {}).get("action") == "STOP_AND_REVIEW":
        result["nextAction"] = "Stop automatic repairs and request review; repeated failure limit reached."
    write_json(root / ".compile-fast/results" / f"{identity}.json", result)
    return result


def execute(root: Path, model_path: Path, **options) -> dict:
    with process_metrics() as measurements:
        result = _execute(root, model_path, **options)
    result["resources"] = {"processes": measurements,
                           "cpuMs": round(sum(p.get("cpuMs", 0) for p in measurements), 2),
                           "peakRssBytes": max((p.get("peakRssBytes") or 0 for p in measurements), default=0),
                           "coverage": "Spawned process leaders only; excludes build-tool descendants and Python host. Sampling is best effort."}
    write_json(root / ".compile-fast/results" / f"{result['verificationId']}.json", result)
    return result


def metrics(root: Path) -> dict:
    records = []
    for path in (root / ".compile-fast/results").glob("*.json"):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    # Pipeline stage files also exist for audit, but only count pipeline runs when available.
    runs = [r for r in records if "stages" in r] or records
    def percentile(values, fraction):
        return sorted(values)[max(0, int((len(values) - 1) * fraction))] if values else None
    categories = {}
    for name, group in (("cold", [r for r in runs if not r.get("cacheHits")]),
                        ("warm", [r for r in runs if r.get("cacheHits")])):
        values = [r["durationMs"] for r in group if "durationMs" in r]
        categories[name] = {"count": len(values), "p50Ms": percentile(values, .5), "p95Ms": percentile(values, .95)}
    modules = [m for r in runs for m in r.get("modules", [])]
    return {"status": "PASS", "runs": len(runs), "latency": categories,
            "statuses": {s: sum(r.get("status") == s for r in runs) for s in ("PASS", "FAIL", "UNKNOWN", "INFRA_ERROR")},
            "moduleCacheHitRate": sum(bool(m.get("cacheHit")) for m in modules) / len(modules) if modules else None,
            "note": "Local observed durations only; correctness recall and false-negative rates need a labeled benchmark."}
