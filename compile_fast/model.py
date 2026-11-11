"""Explicit compilation context with conservative validation."""
from __future__ import annotations

import json
from pathlib import Path

from .common import EngineError, build_files, inside


def load(root: Path, path: Path) -> dict:
    if not path.is_file():
        raise EngineError("No project model. Run resolve-maven or supply --model.", code="MODEL_MISSING")
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise EngineError(f"Cannot read model: {exc}", code="INVALID_MODEL") from exc
    if not isinstance(model, dict) or model.get("schemaVersion") != 1:
        raise EngineError("Expected model schemaVersion 1.", code="INVALID_MODEL")
    if model.get("contextComplete") is not True:
        raise EngineError("Model context is not confirmed complete. " +
                          "; ".join(model.get("unsupportedReasons", [])), code="INCOMPLETE_MODEL")
    if "buildFingerprint" not in model or model["buildFingerprint"] != build_files(root):
        raise EngineError("Build files changed or model has no build fingerprint; resolve again.",
                          code="BUILD_MODEL_CHANGED")
    modules = model.get("modules")
    if not isinstance(modules, list) or not modules:
        raise EngineError("Model must contain at least one module.", code="INVALID_MODEL")
    ids = set()
    roots = []
    for module in modules:
        if not isinstance(module, dict) or not isinstance(module.get("id"), str) or not module["id"]:
            raise EngineError("Each module needs a nonempty string id.", code="INVALID_MODEL")
        if module["id"] in ids:
            raise EngineError("Duplicate module id.", code="INVALID_MODEL")
        ids.add(module["id"])
        if module.get("annotationProcessing") is not False:
            raise EngineError(f"{module['id']}: annotation processing needs L3.", code="PROCESSOR_CONTEXT")
        if not module.get("release") and not (module.get("source") and module.get("target")):
            raise EngineError("Set release, or both source and target.", code="INVALID_MODEL")
        for field in ("sourceRoots", "classpath", "dependencies"):
            if not isinstance(module.get(field), list) or not all(isinstance(x, str) for x in module[field]):
                raise EngineError(f"{field} must be a list of strings.", code="INVALID_MODEL")
        for source in module["sourceRoots"]:
            resolved = inside(root, source)
            if any(resolved == previous or resolved.is_relative_to(previous)
                   or previous.is_relative_to(resolved) for previous in roots):
                raise EngineError("Source roots overlap.", code="INVALID_MODEL")
            roots.append(resolved)
        if module.get("compilerArgs") or module.get("modulepath"):
            raise EngineError("Custom compiler arguments / JPMS require L3 in this version.", code="UNSUPPORTED_CONTEXT")
    for module in modules:
        if set(module["dependencies"]) - ids:
            raise EngineError(f"Unknown reactor dependency in {module['id']}", code="INVALID_MODEL")
    ordered, visiting, visited = [], set(), set()
    by_id = {m["id"]: m for m in modules}

    def visit(name):
        if name in visiting:
            raise EngineError("Module dependency cycle.", code="MODULE_CYCLE")
        if name in visited:
            return
        visiting.add(name)
        for dependency in by_id[name]["dependencies"]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(by_id[name])

    for name in by_id:
        visit(name)
    model["modules"] = ordered
    return model


def sources(root: Path, module: dict) -> list[Path]:
    paths = set()
    for source in module["sourceRoots"]:
        folder = inside(root, source)
        for path in folder.rglob("*.java"):
            paths.add(inside(root, str(path)))
    return sorted(paths)
