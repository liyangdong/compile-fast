"""Python adapter to Gradle's configured JavaCompile tasks via an init script."""
import json
import os
from pathlib import Path
import tempfile

from .common import EngineError, build_files, inside, run, tool


def resolve(root: Path, timeout=180, gradle_args=None):
    wrapper = root / ("gradlew.bat" if os.name == "nt" else "gradlew")
    executable = str(wrapper) if wrapper.is_file() else tool("gradle")
    before = build_files(root)
    scratch = root / ".compile-fast/tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="gradle-model-") as temporary:
        output = Path(temporary) / "model.json"
        script = Path(__file__).with_name("gradle_model.gradle")
        args = gradle_args or []
        if any(not (a.startswith("-P") or a.startswith("-D")) for a in args):
            raise EngineError("Gradle arguments must be -P/-D properties.", code="INVALID_ARGUMENT")
        response = run([executable, "--no-daemon", "--console=plain", "-I", str(script), *args,
                        f"-DcompileFast.output={output}", "compileFastExportModel"], root, timeout)
        if response.returncode or not output.is_file():
            raise EngineError("Gradle model export failed.\n" + (response.stdout + response.stderr)[-8000:], code="GRADLE_MODEL_ERROR")
        model = json.loads(output.read_text(encoding="utf-8"))
    for module in model["modules"]:
        module["sourceRoots"] = [inside(root, p).relative_to(root).as_posix() for p in module["sourceRoots"]]
        module["testSourceRoots"] = [inside(root, p).relative_to(root).as_posix() for p in module.get("testSourceRoots", [])]
    if before != build_files(root):
        raise EngineError("Build files changed during Gradle resolution.", code="STALE_WORKSPACE")
    model.update({"schemaVersion": 1, "buildFingerprint": before, "origin": "gradle-javacompile",
                  "gradleArgs": args, "contextComplete": not model["unsupportedReasons"]})
    return model
