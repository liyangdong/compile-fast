"""Resolve Maven's effective model instead of guessing inheritance from raw POMs."""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from .common import EngineError, build_files, inside, run, tool


def parse_xml(path: Path) -> ET.Element:
    node = ET.parse(path).getroot()
    for element in node.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    return node


def text(node, name, default=""):
    return node.findtext(name, default=default).strip()


def has_processor(path: Path) -> bool:
    service = "META-INF/services/javax.annotation.processing.Processor"
    if path.is_dir():
        return (path / service).is_file()
    if path.suffix.lower() == ".jar":
        try:
            with zipfile.ZipFile(path) as jar:
                return service in jar.namelist()
        except (OSError, zipfile.BadZipFile) as exc:
            raise EngineError(f"Invalid dependency JAR: {path}", code="MISSING_DEPENDENCY") from exc
    return False


def resolve(root: Path, timeout: float = 120, maven_args: list[str] | None = None) -> dict:
    executable = tool("mvn")
    args = maven_args or []
    if any(not (a.startswith("-P") or a.startswith("-D")) for a in args):
        raise EngineError("--maven-arg supports -Pprofile and -Dproperty=value only.", code="INVALID_ARGUMENT")
    before = build_files(root)
    pending, effective, seen = [root], [], set()
    reasons = []
    if any(arg.startswith("-Dmaven.compiler.") for arg in args):
        reasons.append("Command-line compiler property overrides need an explicit model or L3")
    scratch = root / ".compile-fast/tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="model-", dir=scratch) as temp:
        temp_root = Path(temp)
        while pending:
            folder = pending.pop(0)
            if folder in seen:
                continue
            seen.add(folder)
            pom = folder / "pom.xml"
            if not pom.is_file():
                raise EngineError(f"Missing Maven POM: {pom}", code="MODEL_MISSING")
            output = temp_root / f"effective-{len(effective)}.xml"
            result = run([executable, "-B", "-N", "-f", str(pom), *args,
                          "org.apache.maven.plugins:maven-help-plugin:3.3.0:effective-pom",
                          f"-Doutput={output}"], root, timeout)
            if result.returncode or not output.is_file():
                raise EngineError("Maven effective-pom failed; check settings, profiles and repository access.\n" +
                                  (result.stdout + result.stderr)[-6000:], code="MAVEN_MODEL_ERROR")
            project = parse_xml(output)
            if project.tag != "project":
                raise EngineError("Unexpected effective POM output.", code="MAVEN_MODEL_ERROR")
            effective.append((folder, project))
            for entry in project.findall("modules/module"):
                pending.append(inside(root, str(folder / (entry.text or ""))))

        coordinates = {f"{text(p, 'groupId')}:{text(p, 'artifactId')}:{text(p, 'version')}":
                       f"{text(p, 'groupId')}:{text(p, 'artifactId')}"
                       for _, p in effective if text(p, "packaging", "jar") != "pom"}
        modules = []
        for index, (folder, project) in enumerate(effective):
            if text(project, "packaging", "jar") == "pom":
                continue
            name = f"{text(project, 'groupId')}:{text(project, 'artifactId')}"
            if text(project, "packaging", "jar") != "jar":
                reasons.append(f"{name}: packaging other than jar requires L3")
            compiler = None
            harmless = {"maven-clean-plugin", "maven-resources-plugin", "maven-jar-plugin",
                        "maven-surefire-plugin", "maven-install-plugin", "maven-deploy-plugin",
                        "maven-site-plugin", "maven-help-plugin", "maven-dependency-plugin"}
            for plugin in project.findall("build/plugins/plugin"):
                artifact = text(plugin, "artifactId")
                if text(plugin, "groupId", "org.apache.maven.plugins") != "org.apache.maven.plugins":
                    reasons.append(f"{name}: custom build plugin {artifact}")
                elif artifact == "maven-compiler-plugin":
                    compiler = plugin
                elif artifact not in harmless:
                    reasons.append(f"{name}: unsupported build plugin {artifact}")
            config = compiler.find("configuration") if compiler is not None else None
            config = config if config is not None else ET.Element("configuration")
            allowed = {"release", "source", "target", "encoding", "parameters", "debug",
                       "showWarnings", "showDeprecation", "proc"}
            for option in config:
                if option.tag not in allowed:
                    reasons.append(f"{name}: unsupported compiler option {option.tag}")
            if compiler is not None:
                for execution in compiler.findall("executions/execution"):
                    if execution.find("configuration") is not None:
                        reasons.append(f"{name}: execution-specific compiler configuration")
            if project.find("build/extensions/extension") is not None:
                reasons.append(f"{name}: Maven build extensions require L3")
            props = project.find("properties")
            props = props if props is not None else ET.Element("properties")
            supported_properties = {"maven.compiler.release", "maven.compiler.source", "maven.compiler.target",
                                    "maven.compiler.parameters"}
            for prop in props:
                if prop.tag.startswith("maven.compiler.") and prop.tag not in supported_properties:
                    reasons.append(f"{name}: unsupported compiler property {prop.tag}")
            release = text(config, "release", text(props, "maven.compiler.release"))
            source = text(config, "source", text(props, "maven.compiler.source"))
            target = text(config, "target", text(props, "maven.compiler.target"))
            if not release and not (source and target):
                reasons.append(f"{name}: explicitly set maven.compiler.release or source/target")
            compiler_version = text(compiler, "version") if compiler is not None else ""
            version_match = re.fullmatch(r"3\.(\d+)(?:\.\d+)?", compiler_version)
            if release and (not version_match or int(version_match[1]) < 8):
                reasons.append(f"{name}: release modeling requires an explicit compiler plugin version 3.8+ (3.x)")
            src = text(project, "build/sourceDirectory", str(folder / "src/main/java"))
            src_path = inside(root, src)
            if any(src_path.rglob("module-info.java")):
                reasons.append(f"{name}: JPMS requires L3")
            dependencies = []
            external = False
            for dependency in project.findall("dependencies/dependency"):
                scope = text(dependency, "scope", "compile")
                if scope not in {"compile", "provided", "system"}:
                    continue
                gav = ":".join(text(dependency, k) for k in ("groupId", "artifactId", "version"))
                if gav in coordinates and not text(dependency, "classifier") and text(dependency, "type", "jar") == "jar":
                    dependencies.append(coordinates[gav])
                else:
                    external = True
            classpath = []
            # Maven resolves transitive external dependencies, including those reached through reactor artifacts.
            if external or dependencies:
                cp_file = temp_root / f"classpath-{index}.txt"
                response = run([executable, "-B", "-N", "-f", str(folder / "pom.xml"), *args,
                                "org.apache.maven.plugins:maven-dependency-plugin:3.6.1:build-classpath",
                                "-DincludeScope=compile", f"-Dmdep.outputFile={cp_file}"], root, timeout)
                if response.returncode or not cp_file.is_file():
                    raise EngineError("Maven classpath resolution failed. Reactor dependencies may need an existing "
                                      "local-repository install; run your normal Maven build or supply an explicit model.\n" +
                                      (response.stdout + response.stderr)[-6000:], code="MAVEN_CLASSPATH_ERROR")
                classpath = [p for p in cp_file.read_text(encoding="utf-8").strip().split(os.pathsep) if p]
                # Remove every reactor artifact, including transitively reached ones; use fresh compiler outputs.
                reactor_entries = set()
                for gav, module_id in coordinates.items():
                    group, artifact, version = gav.split(":")
                    suffix = "/" + group.replace(".", "/") + f"/{artifact}/{version}/{artifact}-{version}.jar"
                    for item in classpath:
                        if Path(item).as_posix().endswith(suffix):
                            reactor_entries.add(item)
                            if module_id != name and module_id not in dependencies:
                                dependencies.append(module_id)
                classpath = [p for p in classpath if p not in reactor_entries]
            for item in classpath:
                if has_processor(Path(item)):
                    reasons.append(f"{name}: annotation processor on classpath requires L3")
            if text(config, "proc") not in {"", "none"}:
                reasons.append(f"{name}: annotation processing enabled")
            modules.append({"id": name, "sourceRoots": [src_path.relative_to(root).as_posix()],
                            "dependencies": sorted(set(dependencies)), "classpath": classpath,
                            "release": release or None, "source": source or None, "target": target or None,
                            "encoding": text(config, "encoding", text(props, "project.build.sourceEncoding", "UTF-8")),
                            "parameters": text(config, "parameters", text(props, "maven.compiler.parameters", "false")) == "true",
                            "debug": text(config, "debug", "true") == "true",
                            "annotationProcessing": False})
    if before != build_files(root):
        raise EngineError("Build files changed during model resolution.", code="STALE_WORKSPACE")
    if not modules:
        reasons.append("No Java jar modules were found")
    return {"schemaVersion": 1, "contextComplete": not reasons, "buildFingerprint": before,
            "origin": "maven-effective-pom", "mavenArgs": args,
            "unsupportedReasons": sorted(set(reasons)), "modules": modules}
