"""Conservative module incremental verifier; javac is the semantic authority."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid
import difflib

from . import __version__
from .cache import Cache
from .abi import scan as scan_abi, compare as compare_abi
from .changes import detect
from .common import EngineError, build_files, checkpoint, digest, file_hash, inside, run, tool, tree_hash, write_json
from .maven import has_processor
from .model import load, sources


def snapshot(root: Path, model: dict) -> dict:
    return {m["id"]: {p.relative_to(root).as_posix(): file_hash(p) for p in sources(root, m)}
            for m in model["modules"]}


def diagnostics(output: str, files: list[Path], root: Path) -> list[dict]:
    by_name = {}
    for path in files:
        by_name.setdefault(path.name, []).append(path)
    entries = []
    for line in output.splitlines():
        match = re.match(r"^(.*\.java):(\d+):(\d+): (compiler\.(?:err|warn)\.[^: ]+)(?:: (.*))?$", line)
        if match:
            name, row, column, code, message = match.groups()
            path = Path(name)
            if path.is_absolute() and path.is_relative_to(root):
                filename = path.relative_to(root).as_posix()
            else:
                candidates = by_name.get(path.name, [])
                filename = candidates[0].relative_to(root).as_posix() if len(candidates) == 1 else None
            entries.append({"file": filename, "reportedFile": name, "line": int(row), "column": int(column),
                            "severity": "error" if ".err." in code else "warning",
                            "errorCode": code, "message": message or code})
    return entries


def arg_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def verify(root: Path, model_path: Path, *, changed: list[str] | None = None,
           base: str | None = None, javac: str = "javac", timeout: float = 120,
           use_cache: bool = True, change_id: str | None = None, agent: str | None = None) -> dict:
    root = root.resolve()
    start = time.monotonic()
    identity = str(uuid.uuid4())
    result = {"schemaVersion": 1, "verificationId": identity, "workspaceId": str(root),
              "commitId": None, "changeId": change_id, "status": "UNKNOWN", "verificationLevel": "L2",
              "verificationScope": [], "diagnostics": [], "modules": [], "changedFiles": [],
              "attribution": {"agent": agent, "recipeId": None},
              "limitations": ["Main sources only; no tests, annotation processing, runtime or business guarantees.",
                              "Whole changed modules are compiled; downstream caches use verified bytecode ABI."],
              "nextAction": "Run the project's normal build/test gate before merging."}
    cache = None
    try:
        model = load(root, model_path)
        model_hash = file_hash(model_path)
        result["projectModelVersion"] = digest(model)
        if changed is None:
            changed, result["commitId"] = detect(root, base)
        normalized = [inside(root, f).relative_to(root).as_posix() for f in changed]
        result["changedFiles"] = sorted(set(normalized))
        before = snapshot(root, model)
        source_roots = [inside(root, s) for m in model["modules"] for s in m["sourceRoots"]]
        for item in normalized:
            path = inside(root, item)
            if path.suffix == ".java" and not any(path.is_relative_to(s) for s in source_roots):
                raise EngineError(f"Changed source outside modeled main roots: {item}", code="SOURCE_SET_UNSUPPORTED")
        if not any(before.values()):
            raise EngineError("No Java sources in the modeled scope.", code="EMPTY_SCOPE")
        compiler = tool(javac)
        version = run([compiler, "-version"], root, timeout)
        if version.returncode:
            raise EngineError(version.stderr, "INFRA_ERROR", "JDK_ERROR")
        # Include compiler/runtime bytes, not only a version string that can remain unchanged after replacement.
        jdk_home = Path(compiler).resolve().parent.parent
        jdk_files = [Path(compiler), jdk_home / "release", jdk_home / "lib/modules"]
        compiler_key = [(str(p), file_hash(p)) for p in jdk_files if p.is_file()]
        result["compiler"] = (version.stdout + version.stderr).strip()
        external_hashes = {}
        for module in model["modules"]:
            if module.get("javaHome") and Path(module["javaHome"]).resolve() != jdk_home:
                raise EngineError(f"{module['id']}: select javac from the configured toolchain {module['javaHome']}", code="TOOLCHAIN_MISMATCH")
            for entry in module["classpath"]:
                path = (root / entry).resolve()
                external_hashes[str(path)] = tree_hash(path)
                if has_processor(path):
                    raise EngineError(f"Annotation processor found: {path}", code="PROCESSOR_CONTEXT")
        cache = Cache(root / ".compile-fast" / "cache")
        outputs, keys, abi_keys, abi_models = {}, {}, {}, {}
        all_outputs = {}
        for module in model["modules"]:
            checkpoint()
            name = module["id"]
            files = sources(root, module)
            if any(p.name == "module-info.java" for p in files):
                raise EngineError("JPMS compilation requires L3.", code="JPMS_UNSUPPORTED")
            cp = [(root / x).resolve() for x in module["classpath"]]
            # dependencies is the actual resolved reactor compile classpath, not guessed from package names.
            cp = [outputs[d] for d in module["dependencies"]] + cp
            key = digest({"engine": __version__, "module": module, "sources": before[name],
                          "jdk": compiler_key, "model": result["projectModelVersion"],
                          "external": external_hashes, "dependencies": {d: abi_keys[d] for d in module["dependencies"]}})
            keys[name] = key
            covered = [p.relative_to(root).as_posix() for p in files]
            output = cache.get(key) if use_cache else None
            if output is not None:
                outputs[name] = output
                abi_models[name] = cache.metadata("abi:" + key) or scan_abi(output)
                abi_keys[name] = abi_models[name]["abiHash"]
                all_outputs[str(output)] = tree_hash(output)
                result["verificationScope"].extend(covered)
                result["modules"].append({"id": name, "status": "PASS", "cacheHit": True, "scope": covered,
                                          "abiHash": abi_keys[name]})
                continue
            working = cache.root / "outputs" / str(uuid.uuid4())
            output = working / "classes"
            output.mkdir(parents=True)
            empty = working / "empty"
            empty.mkdir()
            if files:
                args = ["-XDrawDiagnostics", "-proc:none", "-implicit:none", "-encoding", module.get("encoding", "UTF-8"),
                        "-sourcepath", str(empty), "-classpath", os.pathsep.join(map(str, cp)) or str(empty),
                        "-d", str(output)]
                if module.get("release"):
                    args.extend(["--release", str(module["release"])])
                else:
                    args.extend(["-source", str(module["source"]), "-target", str(module["target"])])
                args.append("-g" if module.get("debug", True) else "-g:none")
                if module.get("parameters"):
                    args.append("-parameters")
                args.extend(map(str, files))
                argument_file = working / "javac.args"
                argument_file.write_text("\n".join(arg_quote(a) for a in args), encoding="utf-8")
                response = run([compiler, "-J-Duser.language=en", "-J-Duser.country=US",
                                "-J-Dfile.encoding=UTF-8", f"@{argument_file}"], root, timeout)
                raw = response.stdout + response.stderr
                if re.search(r"(?:java\.(?:lang|nio|io)\.[\w.]+(?:Exception|Error)|An exception has occurred in the compiler)", raw):
                    shutil.rmtree(working)
                    raise EngineError(raw[-10000:], "INFRA_ERROR", "COMPILER_CRASH")
                parsed = diagnostics(raw, files, root)
                result["diagnostics"].extend(parsed)
                result["verificationScope"].extend(covered)
                if response.returncode:
                    shutil.rmtree(working)
                    infra = response.returncode not in (1,)
                    result["status"] = "INFRA_ERROR" if infra else "FAIL"
                    result["rawCompilerOutput"] = raw[-20000:]
                    if not parsed:
                        result["diagnostics"].append({"errorCode": "COMPILER_ERROR", "message": raw[-10000:]})
                    result["modules"].append({"id": name, "status": result["status"], "cacheHit": False, "scope": covered})
                    break
            else:
                covered = []
            if before != snapshot(root, model):
                shutil.rmtree(working)
                raise EngineError("Sources changed during compilation; retry latest version.", code="STALE_WORKSPACE")
            outputs[name] = output
            abi_models[name] = scan_abi(output)
            abi_keys[name] = abi_models[name]["abiHash"]
            previous_abi = cache.metadata("latest-abi:" + name)
            all_outputs[str(output)] = tree_hash(output)
            result["modules"].append({"id": name, "status": "PASS", "cacheHit": False, "scope": covered,
                                      "abiHash": abi_keys[name],
                                      "abiChange": compare_abi(previous_abi or {}, abi_models[name])})
        else:
            result["status"] = "PASS"
        if (before != snapshot(root, model) or model_hash != file_hash(model_path)
                or model["buildFingerprint"] != build_files(root)
                or any(tree_hash(Path(p)) != h for p, h in external_hashes.items())
                or any(tree_hash(Path(p)) != h for p, h in all_outputs.items())):
            raise EngineError("Inputs changed during verification; discard stale diagnostics and retry.", code="STALE_WORKSPACE")
        if result["status"] == "PASS":
            for name, output in outputs.items():
                cache.put(keys[name], output)
                cache.set_metadata("abi:" + keys[name], abi_models[name])
                cache.set_metadata("latest-abi:" + name, abi_models[name])
            write_json(root / ".compile-fast/index/abi.json", abi_models)
        result["sourceSnapshot"] = digest(before)
        if result["status"] == "FAIL":
            for diagnostic in result["diagnostics"]:
                missing = re.search(r"kindname.method, ([\w$]+)", diagnostic.get("message", ""))
                owner = re.search(r"location: kindname.class, ([\w.$]+)", diagnostic.get("message", ""))
                if missing and owner:
                    candidate_methods = [m for abi in abi_models.values() for c in abi["classes"].values()
                                         if c["className"].replace("/", ".") == owner[1]
                                         for m in c["methods"] if not m["name"].startswith("<")]
                    names = difflib.get_close_matches(missing[1], [m["name"] for m in candidate_methods], n=3, cutoff=.3)
                    diagnostic["possibleCandidates"] = [m["name"] + m["descriptor"] for m in candidate_methods if m["name"] in names]
                    diagnostic["candidateEvidence"] = "ABI-declared names with similar spelling; suggestions, not confirmed fixes."
            result["nextAction"] = "Repair the reported compiler errors, then verify again; stop after 3 repeated failures."
    except EngineError as exc:
        result["status"] = exc.status
        result["diagnostics"].append({"errorCode": exc.code, "message": str(exc)})
        result["nextAction"] = "Restore compilation context or use the normal Maven/Gradle build (L3)."
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        result["status"] = "INFRA_ERROR"
        result["diagnostics"].append({"errorCode": "ENGINE_ERROR", "message": str(exc)})
    finally:
        if cache:
            cache.close()
    result["durationMs"] = round((time.monotonic() - start) * 1000)
    result["cacheHits"] = sum(m.get("cacheHit", False) for m in result["modules"])
    result["verificationScope"] = sorted(set(result["verificationScope"]))
    try:
        write_json(root / ".compile-fast" / "results" / f"{identity}.json", result)
    except OSError as exc:
        result["status"] = "INFRA_ERROR"
        result["diagnostics"].append({"errorCode": "EVIDENCE_WRITE_ERROR", "message": str(exc)})
    return result
