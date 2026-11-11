"""SQLite metadata plus immutable per-compilation output directories."""
from pathlib import Path
import sqlite3
import json

from .common import tree_hash


class Cache:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "cache.sqlite3", timeout=30)
        self.db.execute("CREATE TABLE IF NOT EXISTS modules (key TEXT PRIMARY KEY, output TEXT, hash TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, data TEXT)")

    def metadata(self, key):
        row = self.db.execute("SELECT data FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_metadata(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))

    def get(self, key: str) -> Path | None:
        row = self.db.execute("SELECT output, hash FROM modules WHERE key=?", (key,)).fetchone()
        if row:
            output = Path(row[0])
            if output.is_relative_to(self.root) and output.is_dir() and tree_hash(output) == row[1]:
                return output
        return None

    def put(self, key: str, output: Path):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO modules VALUES (?, ?, ?)",
                            (key, str(output), tree_hash(output)))

    def close(self):
        self.db.close()
