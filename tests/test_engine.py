from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from compile_fast.changes import detect
from compile_fast.cli import main
from compile_fast.common import build_files, EngineError, write_json
from compile_fast.engine import verify
from compile_fast.maven import resolve

TEST_TEMP = Path(__file__).resolve().parents[1] / ".compile-fast/tests"
TEST_TEMP.mkdir(parents=True, exist_ok=True)


@unittest.skipUnless(shutil.which("javac"), "JDK javac required")
class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="compile fast 中文 ", dir=TEST_TEMP)
        self.root = Path(self.temp.name).resolve()
        self.model = self.root / ".compile-fast/model.json"
        self.write("core/src/p/Api.java", "package p; public class Api { public int value() { return 1; } }")
        self.write("app/src/q/Client.java", "package q; public class Client { int x = new p.Api().value(); }")
        self.config = {"schemaVersion": 1, "contextComplete": True, "buildFingerprint": {}, "modules": [
            {"id": "core", "sourceRoots": ["core/src"], "dependencies": [], "classpath": [],
             "release": "17", "annotationProcessing": False},
            {"id": "app", "sourceRoots": ["app/src"], "dependencies": ["core"], "classpath": [],
             "release": "17", "annotationProcessing": False}]}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, content):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    def save(self):
        write_json(self.model, self.config)

    def check(self, **kwargs):
        return verify(self.root, self.model, changed=kwargs.pop("changed", []), **kwargs)

    def test_real_compile_cache_and_downstream_invalidation(self):
        first = self.check()
        self.assertEqual(first["status"], "PASS", first)
        self.assertEqual(first["cacheHits"], 0)
        second = self.check()
        self.assertEqual(second["cacheHits"], 2, second)
        self.write("core/src/p/Api.java", "package p; public class Api { public int value() { return 2; } }")
        third = self.check()
        self.assertEqual(third["status"], "PASS", third)
        self.assertEqual(third["cacheHits"], 1)
        self.assertEqual(third["modules"][0]["abiChange"]["level"], "L0")

    def test_api_deletion_catches_unchanged_downstream(self):
        self.assertEqual(self.check()["status"], "PASS")
        self.write("core/src/p/Api.java", "package p; public class Api {}")
        result = self.check(changed=["core/src/p/Api.java"])
        self.assertEqual(result["status"], "FAIL", result)
        self.assertTrue(any(d.get("file") == "app/src/q/Client.java" for d in result["diagnostics"]), result)

    def test_deleted_type_does_not_reuse_old_class(self):
        self.assertEqual(self.check()["status"], "PASS")
        (self.root / "core/src/p/Api.java").unlink()
        result = self.check(changed=["core/src/p/Api.java"])
        self.assertEqual(result["status"], "FAIL", result)

    def test_local_module_change_reuses_upstream(self):
        self.check()
        self.write("app/src/q/Client.java", "package q; public class Client { int x = 2 * new p.Api().value(); }")
        result = self.check()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["cacheHits"], 1)

    def test_generics_and_interface_errors(self):
        self.write("core/src/p/Api.java", "package p; public interface Api { String value(); }")
        self.write("app/src/q/Client.java", "package q; public class Client implements p.Api { public int value() { return 1; } }")
        result = self.check()
        self.assertEqual(result["status"], "FAIL", result)
        self.assertTrue(all(d.get("line", 0) > 0 for d in result["diagnostics"]))

    def test_missing_dependency_unknown(self):
        self.config["modules"][0]["classpath"] = ["missing.jar"]
        self.save()
        result = self.check()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["diagnostics"][0]["errorCode"], "MISSING_DEPENDENCY")

    def test_processor_unknown(self):
        jar = self.root / "processor.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("META-INF/services/javax.annotation.processing.Processor", "Fake")
        self.config["modules"][0]["classpath"] = [str(jar)]
        self.save()
        self.assertEqual(self.check()["status"], "UNKNOWN")

    def test_build_change_unknown(self):
        self.write("pom.xml", "<project/>")
        result = self.check()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["diagnostics"][0]["errorCode"], "BUILD_MODEL_CHANGED")

    def test_outside_main_scope_unknown(self):
        self.write("app/test/Test.java", "class Test {}")
        result = self.check(changed=["app/test/Test.java"])
        self.assertEqual(result["status"], "UNKNOWN")

    def test_missing_jdk_infra_error(self):
        self.assertEqual(self.check(javac="does-not-exist-compile-fast")["status"], "INFRA_ERROR")

    def test_cycle_unknown(self):
        self.config["modules"][0]["dependencies"] = ["app"]
        self.save()
        self.assertEqual(self.check()["status"], "UNKNOWN")

    def test_escape_unknown(self):
        self.assertEqual(self.check(changed=["../escape.java"])["status"], "UNKNOWN")

    def test_jpms_unknown(self):
        self.write("core/src/module-info.java", "module core { exports p; }")
        self.assertEqual(self.check()["status"], "UNKNOWN")

    def test_tampered_cache_recompiled(self):
        self.check()
        files = list((self.root / ".compile-fast/cache/outputs").rglob("Api.class"))
        self.assertEqual(len(files), 1)
        files[0].write_bytes(b"corrupt")
        result = self.check()
        self.assertEqual(result["status"], "PASS", result)
        self.assertFalse(result["modules"][0]["cacheHit"])

    def test_mutation_during_compile_never_passes(self):
        from compile_fast import engine
        real = engine.run

        def mutate(args, *rest, **kwargs):
            response = real(args, *rest, **kwargs)
            if any(a.startswith("@") for a in args):
                self.write("core/src/p/Api.java", "package p; public class Api {}")
            return response

        with patch("compile_fast.engine.run", side_effect=mutate):
            result = self.check()
        self.assertEqual(result["status"], "UNKNOWN", result)
        self.assertEqual(result["diagnostics"][-1]["errorCode"], "STALE_WORKSPACE")

    def test_timeout_is_infra_error(self):
        with patch("compile_fast.engine.run", side_effect=EngineError("timeout", "INFRA_ERROR", "TIMEOUT")):
            self.assertEqual(self.check()["status"], "INFRA_ERROR")

    def test_compiler_crash_even_with_zero_exit_is_infra(self):
        from compile_fast import engine
        real = engine.run

        def crash(args, *rest, **kwargs):
            if any(a.startswith("@") for a in args):
                return subprocess.CompletedProcess(args, 0, "", "java.nio.file.AccessDeniedException: dependency.jar")
            return real(args, *rest, **kwargs)

        with patch("compile_fast.engine.run", side_effect=crash):
            result = self.check()
        self.assertEqual(result["status"], "INFRA_ERROR", result)
        self.assertEqual(result["diagnostics"][-1]["errorCode"], "COMPILER_CRASH")

    def test_cli_json_and_exit_code(self):
        self.write("core/src/p/Api.java", "package p; public class Api { broken }")
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(["verify", "--project", str(self.root), "--changed"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stream.getvalue())["status"], "FAIL")


@unittest.skipUnless(shutil.which("git"), "Git required")
class GitTests(unittest.TestCase):
    def test_staged_unstaged_untracked_rename_and_deletion(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as temp:
            root = Path(temp).resolve()

            def git(*args):
                return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)

            git("init")
            for name in ("A.java", "B.java", "C.java", "D.java"):
                (root / name).write_text("old")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline")
            git("mv", "A.java", "改名 A.java")
            (root / "B.java").unlink()
            (root / "C.java").write_text("staged")
            git("add", "C.java")
            (root / "D.java").write_text("unstaged")
            (root / "new.java").write_text("new")
            changed, commit = detect(root)
            self.assertEqual(set(changed), {"A.java", "改名 A.java", "B.java", "C.java", "D.java", "new.java"})
            self.assertTrue(commit)


class MavenTests(unittest.TestCase):
    def test_effective_pom_and_classpath_adapter(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as temp:
            root = Path(temp).resolve()
            (root / "pom.xml").write_text("<project/>")
            pom = '''<project xmlns="http://maven.apache.org/POM/4.0.0"><groupId>x</groupId>
            <artifactId>demo</artifactId><version>1</version><properties><maven.compiler.release>17</maven.compiler.release></properties>
            <build><sourceDirectory>src/main/java</sourceDirectory><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version>
            <configuration><release>17</release></configuration></plugin></plugins></build></project>'''

            def fake_run(args, *rest):
                output = next(a.split("=", 1)[1] for a in args if a.startswith("-Doutput="))
                Path(output).write_text(pom)
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch("compile_fast.maven.tool", return_value="mvn"), patch("compile_fast.maven.run", side_effect=fake_run):
                model = resolve(root)
            self.assertTrue(model["contextComplete"], model)
            self.assertEqual(model["modules"][0]["release"], "17")
            self.assertEqual(model["buildFingerprint"], build_files(root))


if __name__ == "__main__":
    unittest.main()
