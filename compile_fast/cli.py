"""Command-line JSON adapter for humans, skills and other agents."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .common import EngineError, build_files, inside, write_json
from .maven import resolve
from .pipeline import execute, metrics

EXIT_CODES = {"PASS": 0, "FAIL": 1, "UNKNOWN": 2, "INFRA_ERROR": 3}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compile-fast", description="Python Java compilation verifier (L2)")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("verify", "resolve-maven", "resolve-gradle", "init", "index", "impact", "metrics", "setup-rewrite", "rewrite-index"):
        sub = commands.add_parser(command)
        sub.add_argument("--project", type=Path, default=Path.cwd())
        sub.add_argument("--model", type=Path, help="Default: PROJECT/.compile-fast/model.json")
        if command == "verify":
            sub.add_argument("--level", choices=["L1", "L2", "L3", "auto"], default="auto")
            sub.add_argument("--allow-build", action="store_true", help="Authorize configured L3 build/test commands")
            sub.add_argument("--changed", nargs="*", help="Explicit paths, including deletions; empty means verify all modeled sources")
            sub.add_argument("--patch", type=Path, help="Unified diff; read file paths without applying the patch")
            sub.add_argument("--change-record", type=Path, help="Agent/OpenRewrite change record JSON")
            sub.add_argument("--base", help="Git revision to compare to working tree (default HEAD)")
            sub.add_argument("--javac", default="javac")
            sub.add_argument("--timeout", type=float, default=120)
            sub.add_argument("--no-cache", action="store_true")
            sub.add_argument("--change-id")
            sub.add_argument("--agent")
            sub.add_argument("--recipe-id")
            sub.add_argument("--revision")
            sub.add_argument("--project-model-version")
            sub.add_argument("--output", type=Path)
        elif command == "resolve-maven":
            sub.add_argument("--timeout", type=float, default=120)
            sub.add_argument("--maven-arg", action="append", default=[], help="Example: --maven-arg=-Pdev")
        elif command == "resolve-gradle":
            sub.add_argument("--timeout", type=float, default=180)
            sub.add_argument("--gradle-arg", action="append", default=[])
        elif command == "setup-rewrite":
            sub.add_argument("--version", default="8.63.0")
            sub.add_argument("--timeout", type=float, default=180)
            sub.add_argument("--repository", type=Path)
            sub.add_argument("--python-path", type=Path)
        elif command == "init":
            sub.add_argument("--release", default="17")
            sub.add_argument("--source-root", action="append", default=None)
            sub.add_argument("--classpath", action="append", default=[])
            sub.add_argument("--confirm-context", action="store_true",
                             help="Confirm roots/dependencies/options are complete and no annotation processing is required")
    abi = commands.add_parser("abi")
    abi.add_argument("--path", type=Path, required=True)
    abi.add_argument("--baseline", type=Path)
    abi.add_argument("--output", type=Path)
    server = commands.add_parser("serve")
    server.add_argument("--workspace", action="append", required=True, help="id=absolute-workspace-path")
    server.add_argument("--state", type=Path, default=Path(".compile-fast/server"))
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--workers", type=int, default=4)
    server.add_argument("--allow-builds", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .server import serve
            registrations = dict(entry.split("=", 1) for entry in args.workspace)
            serve({key: Path(value) for key, value in registrations.items()}, args.state.resolve(),
                  args.host, args.port, args.workers, args.allow_builds)
            return 0
        if args.command == "abi":
            from .abi import scan, compare
            result = {"status": "PASS", **scan(args.path.resolve())}
            if args.baseline:
                result["changes"] = compare(json.loads(args.baseline.read_text(encoding="utf-8")), result)
            if args.output:
                write_json(args.output, result)
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        root = args.project.resolve()
        model_path = args.model.resolve() if args.model else root / ".compile-fast/model.json"
        if not root.is_dir():
            raise EngineError("Project directory does not exist.", "INFRA_ERROR", "PROJECT_MISSING")
        if args.command == "verify":
            if args.timeout <= 0:
                raise EngineError("Timeout must be positive.", code="INVALID_ARGUMENT")
            if sum(x is not None for x in (args.changed, args.patch, args.change_record)) > 1:
                raise EngineError("Use only one of --changed, --patch, --change-record.", code="INVALID_ARGUMENT")
            from .changes import from_patch, from_record
            changed = from_patch(args.patch) if args.patch else from_record(args.change_record) if args.change_record else args.changed
            result = execute(root, model_path, changed=changed, base=args.base, javac=args.javac,
                            timeout=args.timeout, use_cache=not args.no_cache,
                            change_id=args.change_id, agent=args.agent, level=args.level, allow_build=args.allow_build,
                            recipe_id=args.recipe_id, expected_revision=args.revision, expected_model=args.project_model_version)
            if args.output:
                write_json(args.output, result)
        elif args.command == "metrics":
            result = metrics(root)
        elif args.command == "setup-rewrite":
            from .rewrite import setup
            result = setup(root, version=args.version, timeout=args.timeout, repository=args.repository, python_path=args.python_path)
        elif args.command == "rewrite-index":
            from .rewrite import index
            from .model import load
            result = index(root, load(root, model_path))
        elif args.command in {"index", "impact"}:
            from .model import load
            from .source import build_index, impact
            model = load(root, model_path)
            previous_path = root / ".compile-fast/index/sources.json"
            previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.is_file() else {}
            index = build_index(root, model)
            result = {"status": "PASS", **(index if args.command == "index" else impact(previous, index, model))}
        else:
            if args.command == "resolve-maven":
                model = resolve(root, args.timeout, args.maven_arg)
            elif args.command == "resolve-gradle":
                from .gradle import resolve as resolve_gradle
                model = resolve_gradle(root, args.timeout, args.gradle_arg)
            else:
                roots = args.source_root or ["src/main/java"]
                for source in roots:
                    inside(root, source)
                model = {"schemaVersion": 1, "origin": "explicit", "contextComplete": args.confirm_context,
                         "buildFingerprint": build_files(root), "unsupportedReasons": [],
                         "modules": [{"id": "main", "sourceRoots": roots, "dependencies": [],
                                      "classpath": args.classpath, "release": args.release,
                                      "encoding": "UTF-8", "annotationProcessing": False}]}
            write_json(model_path, model)
            result = {"status": "PASS" if model["contextComplete"] else "UNKNOWN", "modelPath": str(model_path),
                      "operation": args.command, "message": "Model generated; this is not a compilation result.",
                      "unsupportedReasons": model.get("unsupportedReasons", [])}
    except EngineError as exc:
        result = {"status": exc.status, "diagnostics": [{"errorCode": exc.code, "message": str(exc)}]}
    except (OSError, ValueError) as exc:
        result = {"status": "INFRA_ERROR", "diagnostics": [{"errorCode": "ENGINE_ERROR", "message": str(exc)}]}
    # ASCII escapes keep stdout a valid JSON transport even in legacy Windows code pages.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
