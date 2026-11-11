"""Opt-in real Maven test. Downloads plugins into a workspace-local repository."""
from pathlib import Path
import json
import shutil
import sys
import tempfile
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compile_fast.common import run, tool, write_json
from compile_fast.engine import verify
from compile_fast.maven import resolve


def main():
    workspace = Path(__file__).resolve().parents[1]
    scratch = workspace / ".compile-fast/tests"
    scratch.mkdir(parents=True, exist_ok=True)
    repository = workspace / ".compile-fast/m2"
    artifact = "parent-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="maven-reactor-", dir=scratch) as temp:
        root = Path(temp)
        parent = f'''<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
        <groupId>compile.fast.fixture</groupId><artifactId>{artifact}</artifactId><version>1.0</version><packaging>pom</packaging>
        <modules><module>core</module><module>app</module></modules><properties><maven.compiler.release>17</maven.compiler.release>
        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties><build><plugins><plugin>
        <artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version></plugin></plugins></build></project>'''
        (root / "pom.xml").write_text(parent, encoding="utf-8")
        poms = {artifact: root / "pom.xml"}
        for module in ("core", "app"):
            folder = root / module
            (folder / "src/main/java/demo").mkdir(parents=True)
            dependency = f'''<dependencies><dependency><groupId>compile.fast.fixture</groupId>
            <artifactId>{artifact}-core</artifactId><version>1.0</version></dependency></dependencies>''' if module == "app" else ""
            pom = f'''<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><parent>
            <groupId>compile.fast.fixture</groupId><artifactId>{artifact}</artifactId><version>1.0</version></parent>
            <artifactId>{artifact}-{module}</artifactId>{dependency}</project>'''
            (folder / "pom.xml").write_text(pom, encoding="utf-8")
            poms[f"{artifact}-{module}"] = folder / "pom.xml"
        api = root / "core/src/main/java/demo/Api.java"
        api.write_text("package demo; public class Api { public int value() { return 1; } }", encoding="utf-8")
        (root / "app/src/main/java/demo/Client.java").write_text(
            "package demo; public class Client { int result = new Api().value(); }", encoding="utf-8")
        # Seed only this fixture's prior published artifacts. The resolver must replace this old JAR with fresh outputs.
        compiled = root / "baseline-classes"
        compiled.mkdir()
        response = run([tool("javac"), "--release", "17", "-d", str(compiled), str(api)], root)
        assert response.returncode == 0, response.stderr
        for name in (artifact, f"{artifact}-core"):
            dest = repository / "compile/fast/fixture" / name / "1.0"
            dest.mkdir(parents=True)
            shutil.copy2(poms[name], dest / f"{name}-1.0.pom")
            if name.endswith("-core"):
                with zipfile.ZipFile(dest / f"{name}-1.0.jar", "w") as archive:
                    archive.write(compiled / "demo/Api.class", "demo/Api.class")
        model = resolve(root, timeout=180, maven_args=[f"-Dmaven.repo.local={repository}"])
        assert model["contextComplete"], model
        assert len(model["modules"]) == 2, model
        model_path = root / ".compile-fast/model.json"
        write_json(model_path, model)
        first = verify(root, model_path, changed=[])
        assert first["status"] == "PASS", first
        hot = verify(root, model_path, changed=[])
        assert hot["status"] == "PASS" and hot["cacheHits"] == 2, hot
        api.write_text("package demo; public class Api {}", encoding="utf-8")
        broken = verify(root, model_path, changed=["core/src/main/java/demo/Api.java"])
        assert broken["status"] == "FAIL", broken
        assert any(d.get("file") == "app/src/main/java/demo/Client.java" for d in broken["diagnostics"]), broken
        report = {"status": "PASS", "checks": ["real Maven effective POM", "real Maven reactor classpath",
                  "old reactor JAR replaced", "javac passes", "two module cache hits", "API deletion fails downstream"],
                  "coldMs": first["durationMs"], "hotMs": hot["durationMs"], "deletionDiagnostics": broken["diagnostics"]}
        write_json(workspace / ".compile-fast/maven-integration-result.json", report)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
