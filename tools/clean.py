"""Cross-platform cleanup of build artifacts, caches and local runtime data.

Keeps complex deletion logic out of the Makefile so it behaves the same
on Windows and POSIX shells.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DIR_PATTERNS = (
    # NOTE: the root .venv is intentionally NOT removed: `make clean` runs
    # through `uv run`, which needs it. Recreate/upgrade it with `make sync`
    # or delete it manually for a from-scratch environment.
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".pyright",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    ".eggs",
    "*.egg-info",
)

FILE_PATTERNS = (
    ".coverage",
    "*.log",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
)


def main() -> None:
    removed_dirs: list[Path] = []
    removed_files: list[Path] = []

    for pattern in DIR_PATTERNS:
        for path in ROOT.glob(pattern):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                removed_dirs.append(path)
    for pattern in DIR_PATTERNS:
        for path in ROOT.rglob(pattern):
            if path.is_dir() and path != ROOT:
                shutil.rmtree(path, ignore_errors=True)
                removed_dirs.append(path)
    for pattern in FILE_PATTERNS:
        for path in ROOT.rglob(pattern):
            if path.is_file():
                path.unlink()
                removed_files.append(path)

    print(f"removed {len(removed_dirs)} directories, {len(removed_files)} files")
    for path in removed_dirs:
        print(f"  dir  {path.relative_to(ROOT)}")
    for path in removed_files:
        print(f"  file {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
