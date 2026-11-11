"""Run the skill's bundled engine, or the source checkout without installation."""
from pathlib import Path
import sys

repository = Path(__file__).resolve().parents[3]
if (repository / "compile_fast/__init__.py").is_file():
    sys.path.insert(0, str(repository))

from compile_fast.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
