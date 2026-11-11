"""Read JVM class files in Python; code/debug attributes are excluded from ABI.

Unknown attributes retain the entire constant pool as a conservative fallback.
Package-private and private members are retained to cover nestmate/package callers.
"""
from __future__ import annotations

from pathlib import Path
import struct
import zipfile

from .common import EngineError, checkpoint, digest


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, length):
        if length < 0 or self.pos + length > len(self.data):
            raise ValueError("Truncated class file")
        data = self.data[self.pos:self.pos + length]
        self.pos += length
        return data

    def u1(self):
        return int.from_bytes(self.take(1), "big")

    def u2(self):
        return int.from_bytes(self.take(2), "big")

    def u4(self):
        return int.from_bytes(self.take(4), "big")


def extract(data: bytes) -> dict:
    try:
        reader = Reader(data)
        if reader.u4() != 0xCAFEBABE:
            raise ValueError("Invalid class magic")
        minor, major = reader.u2(), reader.u2()
        pool = [None] * reader.u2()
        i = 1
        while i < len(pool):
            tag = reader.u1()
            if tag == 1:
                value = reader.take(reader.u2()).replace(b"\xc0\x80", b"\x00").decode("utf-8", "surrogatepass")
            elif tag in (3, 4):
                value = reader.take(4).hex()
            elif tag in (5, 6):
                value = reader.take(8).hex()
            elif tag in (7, 8, 16, 19, 20):
                value = reader.u2()
            elif tag in (9, 10, 11, 12, 17, 18):
                value = [reader.u2(), reader.u2()]
            elif tag == 15:
                value = [reader.u1(), reader.u2()]
            else:
                raise ValueError(f"Unknown constant tag {tag}")
            pool[i] = (tag, value)
            i += 2 if tag in (5, 6) else 1

        def cp(index, depth=0):
            if index == 0:
                return None
            if depth > 12 or index >= len(pool) or pool[index] is None:
                raise ValueError("Invalid constant pool reference")
            tag, value = pool[index]
            if tag == 1:
                return value
            if tag in (7, 8, 16, 19, 20):
                return cp(value, depth + 1)
            if tag in (9, 10, 11, 12):
                return [tag, *[cp(v, depth + 1) for v in value]]
            return [tag, value]

        def annotation(r):
            return {"type": cp(r.u2()), "values": [[cp(r.u2()), element(r)] for _ in range(r.u2())]}

        def element(r):
            tag = chr(r.u1())
            if tag in "BCDFIJSZsc":
                return [tag, cp(r.u2())]
            if tag == "e":
                return [tag, cp(r.u2()), cp(r.u2())]
            if tag == "@":
                return [tag, annotation(r)]
            if tag == "[":
                return [tag, [element(r) for _ in range(r.u2())]]
            raise ValueError("Invalid annotation value")

        def attributes(r):
            result = {}
            for _ in range(r.u2()):
                name = cp(r.u2())
                raw = r.take(r.u4())
                a = Reader(raw)
                if name in {"Code", "SourceFile", "SourceDebugExtension", "BootstrapMethods"}:
                    continue
                if name in {"Signature", "ConstantValue", "NestHost", "ModuleMainClass"}:
                    value = cp(a.u2())
                elif name in {"Exceptions", "NestMembers", "PermittedSubclasses", "ModulePackages"}:
                    value = [cp(a.u2()) for _ in range(a.u2())]
                elif name == "EnclosingMethod":
                    value = [cp(a.u2()), cp(a.u2())]
                elif name == "InnerClasses":
                    value = [[cp(a.u2()), cp(a.u2()), cp(a.u2()), a.u2()] for _ in range(a.u2())]
                elif name in {"Synthetic", "Deprecated"}:
                    value = True
                elif name in {"RuntimeVisibleAnnotations", "RuntimeInvisibleAnnotations"}:
                    value = [annotation(a) for _ in range(a.u2())]
                elif name in {"RuntimeVisibleParameterAnnotations", "RuntimeInvisibleParameterAnnotations"}:
                    value = [[annotation(a) for _ in range(a.u2())] for _ in range(a.u1())]
                elif name == "AnnotationDefault":
                    value = element(a)
                elif name == "MethodParameters":
                    value = [[cp(a.u2()), a.u2()] for _ in range(a.u1())]
                elif name == "Record":
                    value = [{"name": cp(a.u2()), "descriptor": cp(a.u2()), "attributes": attributes(a)}
                             for _ in range(a.u2())]
                else:
                    # Includes type annotations and module directives; cannot miss an ABI change.
                    value = {"raw": raw.hex(), "constantPool": pool}
                result[name] = value
            return result

        access, name, parent = reader.u2(), cp(reader.u2()), cp(reader.u2())
        interfaces = [cp(reader.u2()) for _ in range(reader.u2())]

        def members():
            values = []
            for _ in range(reader.u2()):
                flags, member_name, descriptor = reader.u2(), cp(reader.u2()), cp(reader.u2())
                attrs = attributes(reader)
                values.append({"name": member_name, "descriptor": descriptor, "accessFlags": flags,
                               "signature": attrs.get("Signature"), "attributes": attrs})
            return sorted(values, key=lambda v: (v["name"], v["descriptor"]))

        fields, methods = members(), members()
        attrs = attributes(reader)
        refs = sorted({cp(n) for n in range(1, len(pool)) if pool[n] and pool[n][0] == 7})
        member_refs = [cp(n) for n in range(1, len(pool)) if pool[n] and pool[n][0] in (9, 10, 11)]
        model = {"className": name, "superName": parent, "interfaces": interfaces, "accessFlags": access,
                 "version": [major, minor], "fields": fields, "methods": methods, "attributes": attrs}
        return {**model, "abiHash": digest(model), "references": refs, "memberReferences": member_refs}
    except (ValueError, IndexError, UnicodeError, struct.error) as exc:
        raise EngineError(f"Cannot read class ABI: {exc}", code="ABI_UNSUPPORTED") from exc


def scan(path: Path) -> dict:
    classes = {}
    if path.is_dir():
        entries = ((p.relative_to(path).as_posix(), p.read_bytes()) for p in sorted(path.rglob("*.class")))
        for name, data in entries:
            checkpoint()
            classes[name] = extract(data)
    elif path.suffix.lower() == ".jar":
        with zipfile.ZipFile(path) as jar:
            for name in sorted(jar.namelist()):
                if name.endswith(".class"):
                    checkpoint()
                    classes[name] = extract(jar.read(name))
    else:
        classes[path.name] = extract(path.read_bytes())
    return {"abiHash": digest({n: c["abiHash"] for n, c in classes.items()}), "classes": classes}


def compare(previous: dict, current: dict) -> dict:
    old, new = previous.get("classes", {}), current.get("classes", {})
    removed, added = sorted(old.keys() - new.keys()), sorted(new.keys() - old.keys())
    changed = [name for name in old.keys() & new.keys() if old[name]["abiHash"] != new[name]["abiHash"]]
    hierarchy = [name for name in changed if any(old[name][key] != new[name][key]
                 for key in ("superName", "interfaces", "accessFlags"))]
    return {"level": "L3" if hierarchy or removed else "L2" if added or changed else "L0",
            "addedTypes": added, "removedTypes": removed, "changedTypes": sorted(changed),
            "hierarchyChanges": hierarchy}
