"""OpenRewrite LST adapter using a Python/JPype worker process (no Java glue code)."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile

from .common import EngineError, digest, file_hash, run, tool, tree_hash, write_json
from .model import sources


def setup(root: Path, *, version="8.63.0", timeout=180, repository=None, python_path=None):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise EngineError("Rewrite version must be a fixed x.y.z version.", code="INVALID_ARGUMENT")
    runtime = root / ".compile-fast/rewrite"
    runtime.mkdir(parents=True, exist_ok=True)
    pom = runtime / "pom.xml"
    pom.write_text(f'''<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
    <groupId>compile.fast</groupId><artifactId>rewrite-runtime</artifactId><version>1</version><dependencies>
    <dependency><groupId>org.openrewrite</groupId><artifactId>rewrite-java-21</artifactId><version>{version}</version></dependency>
    <dependency><groupId>org.openrewrite</groupId><artifactId>rewrite-java-17</artifactId><version>{version}</version></dependency>
    </dependencies></project>''', encoding="utf-8")
    classpath = runtime / "classpath.txt"
    args = [tool("mvn"), "-B", "-f", str(pom), "org.apache.maven.plugins:maven-dependency-plugin:3.6.1:build-classpath",
            f"-Dmdep.outputFile={classpath}"]
    if repository:
        args.append(f"-Dmaven.repo.local={Path(repository).resolve()}")
    response = run(args, root, timeout)
    if response.returncode or not classpath.is_file():
        raise EngineError("OpenRewrite runtime resolution failed.\n" + (response.stdout + response.stderr)[-8000:], code="REWRITE_SETUP")
    import os
    config = {"version": version, "classpath": classpath.read_text(encoding="utf-8").strip().split(os.pathsep)}
    if python_path:
        config["pythonPath"] = str(Path(python_path).resolve())
    write_json(root / ".compile-fast/rewrite.json", config)
    return {"status": "PASS", "config": str(root / ".compile-fast/rewrite.json"),
            "message": "OpenRewrite JARs resolved. Install compile-fast[rewrite] (JPype1) for the Python JVM bridge."}


def index(root: Path, model: dict, timeout=120) -> dict:
    config_path = root / ".compile-fast/rewrite.json"
    if not config_path.is_file():
        raise EngineError("Run setup-rewrite to configure OpenRewrite.", code="REWRITE_MISSING")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    modules = []
    java_home = Path(tool("java")).resolve().parent.parent
    config["javaHome"] = str(java_home)
    config_hash = file_hash(config_path)
    context = {"model": model, "runtime": {p: file_hash(Path(p)) for p in config["classpath"]},
               "jdk": {str(p): file_hash(p) for p in (java_home / "release", java_home / "lib/modules") if p.is_file()}}
    for module in model["modules"]:
        files = sources(root, module)
        # Parse all upstream sources together, so no stale reactor bytecode is needed for attribution.
        upstream = set(module["dependencies"])
        while True:
            expanded = upstream | {d for m in model["modules"] if m["id"] in upstream for d in m["dependencies"]}
            if expanded == upstream:
                break
            upstream = expanded
        context_files = sorted({p for m in model["modules"] if m["id"] in upstream | {module["id"]} for p in sources(root, m)})
        cp = [str((root / p).resolve()) for p in module["classpath"]]
        modules.append({"id": module["id"], "files": [str(p) for p in context_files],
                        "ownedFiles": [str(p) for p in files], "classpath": cp,
                        "encoding": module.get("encoding", "UTF-8")})
        context[module["id"]] = {"sources": {str(p): file_hash(p) for p in context_files},
                                 "classpath": {p: tree_hash(Path(p)) for p in cp}}
    key = digest(context)
    cached = root / ".compile-fast/index" / f"rewrite-{key}.json"
    if cached.is_file():
        result = json.loads(cached.read_text(encoding="utf-8"))
        result["cacheHit"] = True
        return result
    scratch = root / ".compile-fast/tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="rewrite-") as temp:
        request, output = Path(temp) / "request.json", Path(temp) / "result.json"
        write_json(request, {"root": str(root), "runtime": config, "modules": modules, "output": str(output)})
        response = run([sys.executable, str(Path(__file__).with_name("rewrite_worker.py")), str(request)], root, timeout)
        if response.returncode or not output.is_file():
            raise EngineError("OpenRewrite worker failed.\n" + (response.stdout + response.stderr)[-8000:], code="REWRITE_ERROR")
        result = json.loads(output.read_text(encoding="utf-8"))
    if result.get("status") != "PASS":
        raise EngineError(result.get("message", "Incomplete OpenRewrite attribution"), code="REWRITE_INCOMPLETE")
    for module in modules:
        baseline = context[module["id"]]
        if any(not Path(p).is_file() or file_hash(Path(p)) != h for p, h in baseline["sources"].items()) or any(
                tree_hash(Path(p)) != h for p, h in baseline["classpath"].items()):
            raise EngineError("Semantic context changed during attribution.", code="STALE_WORKSPACE")
    if file_hash(config_path) != config_hash:
        raise EngineError("OpenRewrite runtime configuration changed during attribution.", code="STALE_WORKSPACE")
    result.update({"contextKey": key, "cacheHit": False, "backend": "OpenRewrite", "version": config["version"]})
    write_json(cached, result)
    write_json(root / ".compile-fast/index/rewrite.json", result)
    return result
