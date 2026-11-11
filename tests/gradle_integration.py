"""Opt-in real Gradle model, L2 and L3 integration test; needs Gradle on PATH."""
from pathlib import Path
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compile_fast.common import write_json
from compile_fast.engine import verify
from compile_fast.gates import verify_build
from compile_fast.gradle import resolve


def main():
    workspace = Path(__file__).resolve().parents[1]
    scratch = workspace / ".compile-fast/tests"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gradle-reactor-", dir=scratch) as temp:
        root = Path(temp)
        (root / "settings.gradle").write_text("rootProject.name = 'compile-fast-fixture'\ninclude 'core', 'app'\n", encoding="utf-8")
        (root / "build.gradle").write_text('''subprojects {
            apply plugin: 'java'
            tasks.withType(JavaCompile).configureEach { options.release = 17 }
        }
        project(':app') { dependencies { implementation project(':core') } }
        ''', encoding="utf-8")
        for module in ("core", "app"):
            (root / module / "src/main/java/demo").mkdir(parents=True)
        api = root / "core/src/main/java/demo/Api.java"
        good = "package demo; public class Api { public int value() { return 1; } }"
        api.write_text(good, encoding="utf-8")
        (root / "app/src/main/java/demo/Client.java").write_text(
            "package demo; public class Client { int result = new Api().value(); }", encoding="utf-8")
        model = resolve(root, timeout=180)
        assert model["contextComplete"], model
        model_path = root / ".compile-fast/model.json"
        write_json(model_path, model)
        result = verify(root, model_path, changed=[])
        assert result["status"] == "PASS", result
        api.write_text("package demo; public class Api {}", encoding="utf-8")
        broken = verify(root, model_path, changed=[])
        assert broken["status"] == "FAIL", broken
        api.write_text(good, encoding="utf-8")
        gate = verify_build(root, timeout=180, model_path=model_path)
        assert gate["status"] == "PASS", gate
        report = {"status": "PASS", "checks": ["Gradle JavaCompile export", "reactor dependency mapping",
                  "L2 passes", "API deletion detected", "L3 clean build passes"]}
        write_json(workspace / ".compile-fast/gradle-integration-result.json", report)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
