"""Install the packaged skill without overwriting an existing skill."""
import argparse
import os
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "skills/compile-fast"
    target = args.destination.expanduser().resolve() / "compile-fast"
    if target.exists() and not (target / "SKILL.md").is_file():
        parser.error(f"Destination is not an existing skill: {target}")
    shutil.copytree(source, target, dirs_exist_ok=True)
    shutil.copytree(source.parents[1] / "compile_fast", target / "scripts/compile_fast",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), dirs_exist_ok=True)
    print(f"Installed skill: {target}")
    print("Python engine bundled. Start a new agent session to discover the skill.")


if __name__ == "__main__":
    main()
