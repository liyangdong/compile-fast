"""Opt-in OpenRewrite LST attribution test; pass --runtime-config from setup-rewrite."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compile_fast.common import write_json
from compile_fast.rewrite import index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-config", type=Path, required=True)
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    scratch = workspace / ".compile-fast/tests"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rewrite-", dir=scratch) as temp:
        root = Path(temp)
        (root / "src/demo").mkdir(parents=True)
        api = root / "src/demo/Api.java"
        api.write_text("package demo; public class Api { public int value() {return 1;} }", encoding="utf-8")
        (root / "src/demo/Client.java").write_text(
            "package demo; public class Client { int result = new Api().value(); }", encoding="utf-8")
        write_json(root / ".compile-fast/rewrite.json", json.loads(args.runtime_config.read_text(encoding="utf-8")))
        model = {"schemaVersion": 1, "contextComplete": True, "buildFingerprint": {}, "modules": [
            {"id": "main", "sourceRoots": ["src"], "dependencies": [], "classpath": [], "release": "17",
             "encoding": "UTF-8", "annotationProcessing": False}]}
        first = index(root, model)
        refs = first["files"]["src/demo/Client.java"]["references"]
        assert any(r["kind"] == "MethodInvocation" and "value" in r["symbol"] and r["line"] == 1 for r in refs), first
        assert index(root, model)["cacheHit"]
        api.write_text("package demo; public class Api { public int value() {return 2;} }", encoding="utf-8")
        updated = index(root, model)
        assert updated["contextKey"] != first["contextKey"] and not updated["cacheHit"], updated
        result = {"status": "PASS", "checks": ["OpenRewrite LST parses", "method call attributed with location",
                  "semantic cache reused", "source change invalidates attribution"], "reference": refs}
        write_json(workspace / ".compile-fast/rewrite-integration-result.json", result)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
