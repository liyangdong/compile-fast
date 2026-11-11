"""Isolated JVM worker. Each process has its own parser/classpath/type caches."""
import json
from pathlib import Path
import sys


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    config = request["runtime"]
    if config.get("pythonPath"):
        sys.path.insert(0, config["pythonPath"])
    import jpype
    home = Path(config["javaHome"])
    candidates = [home / "bin/server/jvm.dll", home / "lib/server/libjvm.so", home / "lib/server/libjvm.dylib"]
    jvm = next((p for p in candidates if p.is_file()), None)
    if jvm is None:
        raise RuntimeError("Cannot locate the JVM belonging to the selected Java executable")
    jpype.startJVM(str(jvm), classpath=config["classpath"], convertStrings=True)
    J = jpype.JClass
    Paths, ArrayList = J("java.nio.file.Paths"), J("java.util.ArrayList")
    JavaParser, Context = J("org.openrewrite.java.JavaParser"), J("org.openrewrite.InMemoryExecutionContext")
    Tree, Iterable = J("org.openrewrite.Tree"), J("java.lang.Iterable")
    files, errors = {}, []
    for module in request["modules"]:
        inputs, cp = ArrayList(), ArrayList()
        for path in module["files"]:
            inputs.add(Paths.get(path))
        for path in module["classpath"]:
            cp.add(Paths.get(path))
        builder = JavaParser.fromJavaVersion().classpath(cp)
        builder.charset(J("java.nio.charset.Charset").forName(module["encoding"]))
        parser = builder.build()
        context = Context()
        stream = parser.parse(inputs, Paths.get(request["root"]), context)
        try:
            iterator = stream.iterator()
            while iterator.hasNext():
                unit = iterator.next()
                relative = str(unit.getSourcePath()).replace("\\", "/")
                absolute = (Path(request["root"]) / relative).resolve()
                if str(absolute) not in module["ownedFiles"]:
                    continue
                if "ParseError" in str(unit.getClass().getName()):
                    errors.append({"file": relative, "error": str(unit.getMarkers())})
                    continue
                source = absolute.read_text(encoding=module["encoding"])
                used = unit.getTypesInUse()
                types = sorted(str(t) for t in used.getTypesInUse())
                methods = sorted(str(t) for t in used.getUsedMethods())
                variables = sorted(str(t) for t in used.getVariables())
                references, visited, queue = [], set(), [(unit, None)]
                while queue:
                    node, caller = queue.pop()
                    identity = str(node.getId())
                    if identity in visited:
                        continue
                    visited.add(identity)
                    kind = str(node.getClass().getSimpleName())
                    if kind == "MethodDeclaration" and node.getMethodType() is not None:
                        caller = str(node.getMethodType())
                    getter = {"MethodInvocation": "getMethodType", "NewClass": "getConstructorType",
                              "Identifier": "getFieldType", "FieldAccess": "getType"}.get(kind)
                    if getter:
                        resolved = getattr(node, getter)()
                        if resolved is not None:
                            snippet = str(node.printTrimmed())
                            offset = source.find(snippet)
                            unique = offset >= 0 and source.find(snippet, offset + 1) < 0
                            line = source.count("\n", 0, offset) + 1 if unique else None
                            column = offset - source.rfind("\n", 0, offset) if unique else None
                            references.append({"kind": kind, "symbol": str(resolved), "text": snippet,
                                               "line": line, "column": column, "caller": caller, "resolution": "ATTRIBUTED"})
                        elif kind in {"MethodInvocation", "NewClass"}:
                            references.append({"kind": kind, "symbol": None, "resolution": "UNKNOWN"})
                    for method in node.getClass().getMethods():
                        name = str(method.getName())
                        if method.getParameterCount() or not name.startswith("get") or name in {
                                "getClass", "getTypesInUse", "getCoordinates", "getPadding", "getMarkers", "getPrefix", "getComments"}:
                            continue
                        try:
                            value = getattr(node, name)()
                            if isinstance(value, Tree):
                                queue.append((value, caller))
                            elif isinstance(value, Iterable):
                                queue.extend((v, caller) for v in value if isinstance(v, Tree))
                        except Exception:
                            continue
                files[relative] = {"types": types, "methods": methods, "variables": variables,
                                   "references": references, "module": module["id"],
                                   "unresolvedCalls": sum(r["resolution"] == "UNKNOWN" for r in references)}
        finally:
            stream.close()
    calls = [{"file": name, "caller": r["caller"], "callee": r["symbol"], "line": r["line"], "column": r["column"]}
             for name, entry in files.items() for r in entry["references"]
             if r.get("caller") and r["kind"] in {"MethodInvocation", "NewClass"} and r["resolution"] == "ATTRIBUTED"]
    result = {"status": "PASS" if not errors else "UNKNOWN", "files": files, "errors": errors, "callGraph": calls,
              "complete": False, "message": "LST attribution is evidence, not a replacement for javac or a complete dependency graph."}
    Path(request["output"]).write_text(json.dumps(result, ensure_ascii=True), encoding="utf-8")
    jpype.shutdownJVM()


if __name__ == "__main__":
    main()
