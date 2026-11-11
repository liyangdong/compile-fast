"""Real Lombok + JUnit fixture: incomplete L2 context escalates to Maven L3."""
from pathlib import Path
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compile_fast.common import write_json
from compile_fast.maven import resolve
from compile_fast.pipeline import execute


def main():
    workspace = Path(__file__).resolve().parents[1]
    scratch = workspace / ".compile-fast/tests"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="maven-processor-", dir=scratch) as temp:
        root = Path(temp)
        (root / "src/main/java/demo").mkdir(parents=True)
        (root / "src/test/java/demo").mkdir(parents=True)
        (root / "pom.xml").write_text('''<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
        <groupId>compile.fast.fixture</groupId><artifactId>processor</artifactId><version>1</version>
        <properties><maven.compiler.release>17</maven.compiler.release><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
        <dependencies><dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId><version>1.18.36</version><scope>provided</scope></dependency>
        <dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
        <build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version></plugin></plugins></build></project>''', encoding="utf-8")
        (root / "src/main/java/demo/Person.java").write_text(
            "package demo; @lombok.Getter public class Person { private String name; }", encoding="utf-8")
        (root / "src/test/java/demo/PersonTest.java").write_text(
            "package demo; public class PersonTest { @org.junit.Test public void generatedGetter() { org.junit.Assert.assertNull(new Person().getName()); } }", encoding="utf-8")
        model = resolve(root, timeout=240, maven_args=[f"-Dmaven.repo.local={workspace / '.compile-fast/m2'}"])
        assert not model["contextComplete"] and any("processor" in r for r in model["unsupportedReasons"]), model
        model_path = root / ".compile-fast/model.json"
        write_json(model_path, model)
        result = execute(root, model_path, changed=[], allow_build=True, timeout=240)
        assert result["status"] == "PASS" and result["verificationLevel"] == "L3", result
        reports = list(root.glob("target/surefire-reports/TEST-*.xml"))
        assert reports and 'tests="1"' in reports[0].read_text(encoding="utf-8"), reports
        report = {"status": "PASS", "checks": ["Lombok processor detected", "L2 returns UNKNOWN",
                  "automatic L3 escalation", "Maven clean verify", "JUnit generated-getter test passes"],
                  "stages": [{"level": s["verificationLevel"], "status": s["status"]} for s in result["stages"]]}
        write_json(workspace / ".compile-fast/maven-processor-result.json", report)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
