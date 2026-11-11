"""Lossless Java syntax trees, positioned declarations, and honest candidate references.

Tree-sitter does not attribute Java types. Candidate edges never authorize scope
pruning; javac and bytecode ABI supply semantic evidence after compilation.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import hashlib

from .common import EngineError, checkpoint, digest, file_hash, write_json
from .model import sources


def parse(data: bytes, filename: str) -> dict:
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_java
    except ImportError as exc:
        raise EngineError("Install compile-fast[semantic] for Java syntax indexing.", code="PARSER_MISSING") from exc
    parser = Parser(Language(tree_sitter_java.language()))
    tree = parser.parse(data)
    declarations, references, errors, signatures = [], [], [], []
    package, imports = "", []

    def text(node):
        return data[node.start_byte:node.end_byte].decode("utf-8") if node else ""

    def position(node):
        # Tree-sitter columns are bytes; expose character columns in UTF-8 input.
        prefix = data[node.start_byte - node.start_point.column:node.start_byte].decode("utf-8")
        return {"file": filename, "line": node.start_point.row + 1, "column": len(prefix) + 1,
                "startByte": node.start_byte, "endByte": node.end_byte}

    type_kinds = {"class_declaration", "interface_declaration", "enum_declaration",
                  "record_declaration", "annotation_type_declaration"}

    def walk(node, owner=""):
        nonlocal package
        if node.type == "package_declaration":
            package = text(node).removeprefix("package").strip().rstrip(";").strip()
        if node.type == "import_declaration":
            imports.append(text(node).removeprefix("import").strip().rstrip(";").strip())
        if node.type == "ERROR" or node.is_missing:
            errors.append({**position(node), "errorCode": "SYNTAX_ERROR", "message": text(node)[:300] or "Missing token"})
        if node.type in type_kinds:
            name = text(node.child_by_field_name("name"))
            owner = f"{owner}${name}" if owner else f"{package}.{name}".strip(".")
            body = node.child_by_field_name("body")
            header = data[node.start_byte:body.start_byte if body else node.end_byte].decode("utf-8")
            declarations.append({**position(node), "kind": node.type, "name": name, "symbol": owner, "header": header})
            signatures.append(header)
        elif node.type in {"method_declaration", "constructor_declaration", "compact_constructor_declaration",
                           "field_declaration", "constant_declaration", "annotation_type_element_declaration"}:
            body = node.child_by_field_name("body")
            header = data[node.start_byte:body.start_byte if body else node.end_byte].decode("utf-8")
            name = text(node.child_by_field_name("name"))
            declarations.append({**position(node), "kind": node.type, "name": name, "owner": owner, "header": header})
            signatures.append(header)
        if node.type in {"type_identifier", "scoped_type_identifier", "method_invocation", "field_access",
                         "marker_annotation", "annotation"}:
            name = text(node.child_by_field_name("name")) or text(node)
            references.append({**position(node), "kind": node.type, "name": name, "owner": owner,
                               "text": text(node)[:500], "resolution": "UNATTRIBUTED"})
        for child in node.named_children:
            walk(child, owner)

    walk(tree.root_node)
    return {"file": filename, "package": package, "imports": imports, "declarations": declarations,
            "references": references, "diagnostics": errors, "syntaxValid": not tree.root_node.has_error,
            "declarationHash": digest(signatures), "contentHash": digest(data.hex()),
            "attributionComplete": False}


def build_index(root: Path, model: dict, *, persist=True) -> dict:
    directory = root / ".compile-fast/index"
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "sources.sqlite3", timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS source_cache (key TEXT PRIMARY KEY, data TEXT)")
    files, hits = {}, 0
    try:
        for module in model["modules"]:
            for path in sources(root, module):
                checkpoint()
                name = path.relative_to(root).as_posix()
                raw = path.read_bytes()
                raw_hash = hashlib.sha256(raw).hexdigest()
                key = digest(["source-v2", raw_hash, name, module.get("encoding", "UTF-8")])
                row = db.execute("SELECT data FROM source_cache WHERE key=?", (key,)).fetchone()
                if row:
                    item = json.loads(row[0])
                    hits += 1
                else:
                    data = raw.decode(module.get("encoding", "UTF-8")).encode("utf-8")
                    item = parse(data, name)
                    item["rawHash"] = raw_hash
                    with db:
                        db.execute("INSERT OR REPLACE INTO source_cache VALUES (?,?)", (key, json.dumps(item)))
                item["module"] = module["id"]
                files[name] = item
    finally:
        db.close()
    types = {}
    for filename, item in files.items():
        for declaration in item["declarations"]:
            if "symbol" in declaration:
                types.setdefault(declaration["name"], []).append((declaration["symbol"], filename))
    edges = []
    for filename, item in files.items():
        for reference in item["references"]:
            simple = reference["name"].split(".")[-1]
            for symbol, target in types.get(simple, []):
                if filename != target:
                    edges.append({"source": filename, "target": target, "symbol": symbol,
                                  "line": reference["line"], "kind": reference["kind"], "confidence": "CANDIDATE"})
    result = {"schemaVersion": 1, "files": files, "edges": edges, "cacheHits": hits,
              "complete": False, "reason": "Syntax candidates are not Java type attribution; module fallback is mandatory."}
    current_paths = {p.relative_to(root).as_posix() for m in model["modules"] for p in sources(root, m)}
    if current_paths != set(files) or any(file_hash(root / p) != item["rawHash"] for p, item in files.items()):
        raise EngineError("Sources changed while building syntax index.", code="STALE_WORKSPACE")
    if persist:
        write_json(directory / "sources.json", result)
    return result


def impact(previous: dict, current: dict, model: dict) -> dict:
    old, new = previous.get("files", {}), current.get("files", {})
    changed = sorted(p for p in old.keys() | new.keys() if old.get(p, {}).get("contentHash") != new.get(p, {}).get("contentHash"))
    levels = {p: "L0" if p in old and p in new and old[p]["declarationHash"] == new[p]["declarationHash"] else "L2"
              for p in changed}
    affected = set(changed)
    edges = previous.get("edges", []) + current.get("edges", [])
    queue = list(changed)
    while queue:
        target = queue.pop()
        for edge in edges:
            if edge["target"] == target and edge["source"] not in affected:
                affected.add(edge["source"])
                queue.append(edge["source"])
    modules = {item["module"] for path in affected for item in [new.get(path) or old.get(path)] if item}
    while True:
        expanded = modules | {m["id"] for m in model["modules"] if set(m["dependencies"]) & modules}
        if expanded == modules:
            break
        modules = expanded
    return {"changedFiles": changed, "changeLevels": levels, "candidateAffectedFiles": sorted(affected),
            "affectedModules": sorted(modules), "strategy": "MODULE_FALLBACK",
            "reason": "Unattributed candidate edges cannot prove a complete file-level compilation closure."}


def fast_verify(root: Path, model: dict) -> dict:
    index = build_index(root, model)
    diagnostics = [d for item in index["files"].values() for d in item["diagnostics"]]
    return {"status": "UNKNOWN" if diagnostics else "PASS", "verificationLevel": "L1",
            "verificationScope": sorted(index["files"]), "diagnostics": diagnostics, "cacheHits": index["cacheHits"],
            "checks": ["Java syntax tree parsing"], "requiresL2": True,
            "limitations": ["Parser errors require javac confirmation; overloads, generics and types are not proven by L1."]}
