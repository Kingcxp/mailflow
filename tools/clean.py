"""Cross-platform cleanup of build artifacts, caches and local runtime data.

Keeps complex deletion logic out of the Makefile so it behaves the same
on Windows and POSIX shells.

Local runtime data is deliberately KEPT by default: ``data/`` (the mail
database, trash and preferences) and ``logs/`` (rotating logs) are user
data, not build output. ``make clean`` must never delete a user's mailbox
history.

Targeted cleanups (all ask for confirmation when run interactively):
- ``--gateways``  delete data/gateways/ (bot gateway installs: NapCat,
  WeChaty) so a broken/partial install can be redone from scratch.
- ``--config``    delete local config files (configs/development.toml,
  configs/local.toml, configs/*.local.toml) so they are regenerated
  from the example.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Subdirectories holding user data; their contents are never removed by
# the default clean.
_PRESERVED_DIRS = (
    "data",
    "logs",
)

# Targeted cleanups: name -> (paths to delete, confirmation label).
_TARGETS: dict[str, tuple[tuple[str, ...], str]] = {
    "gateways": (
        ("data/gateways",),
        "gateway installs under data/gateways/ (NapCat, WeChaty)",
    ),
    "config": (
        ("configs/development.toml", "configs/local.toml"),
        "local config files (regenerated from configs/example.toml)",
    ),
}

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


def _preserved(rel: Path) -> bool:
    """True when ``rel`` sits under a preserved user-data directory."""
    return bool(rel.parts) and rel.parts[0] in _PRESERVED_DIRS


def _confirm(label: str) -> bool:
    """Interactive confirmation; non-TTY runs proceed (make passes -y)."""
    if not sys.stdin.isatty():
        return True
    answer = input(f"Delete {label}? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def clean_target(name: str, force: bool = False) -> int:
    """Delete one targeted cleanup; returns number of removed paths."""
    if name not in _TARGETS:
        print(f"unknown cleanup target {name!r} (use --gateways or --config)")
        return 1
    paths, label = _TARGETS[name]
    if not force and not _confirm(label):
        print("aborted")
        return 0
    removed = 0
    for rel in paths:
        path = ROOT / rel
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
            print(f"  removed dir  {rel}")
        elif path.is_file():
            path.unlink()
            removed += 1
            print(f"  removed file {rel}")
    print(f"cleanup '{name}': removed {removed} path(s)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateways",
        action="store_true",
        help="delete data/gateways/ (bot gateway installs)",
    )
    parser.add_argument(
        "--config",
        action="store_true",
        help="delete local config files (regenerated from the example)",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip confirmation (CI / make targets)",
    )
    args = parser.parse_args()

    if args.gateways or args.config:
        code = 0
        if args.gateways:
            code |= clean_target("gateways", force=args.yes)
        if args.config:
            code |= clean_target("config", force=args.yes)
        sys.exit(code)

    removed_dirs: list[Path] = []
    removed_files: list[Path] = []

    for pattern in DIR_PATTERNS:
        for path in ROOT.glob(pattern):
            if path.is_dir() and not _preserved(path.relative_to(ROOT)):
                shutil.rmtree(path, ignore_errors=True)
                removed_dirs.append(path)
    for pattern in DIR_PATTERNS:
        for path in ROOT.rglob(pattern):
            if path.is_dir() and path != ROOT and not _preserved(path.relative_to(ROOT)):
                shutil.rmtree(path, ignore_errors=True)
                removed_dirs.append(path)
    for pattern in FILE_PATTERNS:
        for path in ROOT.rglob(pattern):
            if path.is_file() and not _preserved(path.relative_to(ROOT)):
                path.unlink()
                removed_files.append(path)

    print(f"removed {len(removed_dirs)} directories, {len(removed_files)} files")
    for path in removed_dirs:
        print(f"  dir  {path.relative_to(ROOT)}")
    for path in removed_files:
        print(f"  file {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
