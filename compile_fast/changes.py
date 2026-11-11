"""Git changes include tracked, staged, deleted, renamed and untracked files."""
from pathlib import Path
import codecs
import json

from .common import EngineError, run, tool


def from_patch(path: Path) -> list[str]:
    paths = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        name = line[4:].split("\t", 1)[0]
        if name == "/dev/null":
            continue
        if name.startswith('"') and name.endswith('"'):
            name = codecs.escape_decode(name[1:-1].encode("utf-8"))[0].decode("utf-8")
        if name.startswith(("a/", "b/")):
            name = name[2:]
        paths.add(name)
    if not paths:
        raise EngineError("Patch contains no unified-diff file headers.", code="INVALID_PATCH")
    return sorted(paths)


def from_record(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    paths = data.get("changedFiles") if isinstance(data, dict) else None
    if paths is None and isinstance(data, dict) and isinstance(data.get("changes"), list):
        paths = [entry[key] for entry in data["changes"] if isinstance(entry, dict)
                 for key in ("beforePath", "afterPath") if entry.get(key)]
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise EngineError("Change record needs changedFiles or changes[{beforePath,afterPath}].", code="INVALID_CHANGE_RECORD")
    return sorted(set(paths))


def detect(root: Path, base: str | None = None) -> tuple[list[str], str | None]:
    git = tool("git")
    head = run([git, "rev-parse", "--verify", "HEAD"], root)
    commit = head.stdout.strip() if head.returncode == 0 else None
    check = run([git, "rev-parse", "--show-toplevel"], root)
    if check.returncode or Path(check.stdout.strip()).resolve() != root.resolve():
        raise EngineError("Use the Git repository root, or pass --changed explicitly.", code="GIT_CONTEXT")
    changed = set()
    if base or commit:
        revision = base or "HEAD"
        verified = run([git, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"], root)
        if verified.returncode:
            raise EngineError(f"Unknown Git revision: {revision}", code="GIT_REVISION")
        response = run([git, "diff", "--name-only", "--no-renames", "-z",
                        verified.stdout.strip(), "--"], root)
    else:
        response = run([git, "ls-files", "--cached", "-z"], root)
    if response.returncode:
        raise EngineError(response.stderr, "INFRA_ERROR", "GIT_ERROR")
    changed.update(filter(None, response.stdout.split("\0")))
    untracked = run([git, "ls-files", "--others", "--exclude-standard", "-z"], root)
    if untracked.returncode:
        raise EngineError(untracked.stderr, "INFRA_ERROR", "GIT_ERROR")
    changed.update(filter(None, untracked.stdout.split("\0")))
    return sorted(changed), commit
