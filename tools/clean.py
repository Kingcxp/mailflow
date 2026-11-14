"""Cross-platform cleanup of build artifacts, caches and local runtime data.

Keeps complex deletion logic out of the Makefile so it behaves the same
on Windows and POSIX shells.

Local runtime data is deliberately KEPT by default: ``data/`` (the mail
database, trash and preferences) and ``logs/`` (rotating logs) are user
data, not build output. ``make clean`` must never delete a user's mailbox
history.

Targeted cleanups (all ask for confirmation when run interactively):
- ``--gateways``  delete data/gateways/ (bot gateway installs: NapCat,
) so a broken/partial install can be redone from scratch.
- ``--config``    delete local config files (configs/development.toml,
  configs/local.toml, configs/*.local.toml) so they are regenerated
  from the example.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
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
        "gateway installs under data/gateways/ (NapCat, OpenWeChat)",
    ),
    "config": (
        ("configs/development.toml", "configs/local.toml"),
        "local config files (regenerated from configs/example.toml)",
    ),
}

DIR_PATTERNS = (
    # Environments are not build output. Only `make sync` installs/updates
    # dependencies; cleaning never recreates or changes an environment.
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
    ".coverage.*",
    "*.coverage",
)
ROOT_ARTIFACTS = (
    "dist-wheels",
    "dist-release",
    "artifacts",
    "release",
    "pytest.xml",
    "coverage.xml",
)


def _preserved(rel: Path) -> bool:
    """True when ``rel`` sits under a preserved user-data directory."""
    return bool(rel.parts) and rel.parts[0] in _PRESERVED_DIRS


def _confirm(label: str) -> bool:
    """Destructive targets require an answer or an explicit --yes."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"Delete {label}? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
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
    targets = [ROOT / rel for rel in paths]
    if name == "config":
        targets.extend((ROOT / "configs").glob("*.local.toml"))
    root = ROOT.resolve()
    for path in targets:
        rel = path.relative_to(ROOT)
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            continue
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


def clean_artifacts(root: Path) -> tuple[list[Path], list[Path]]:
    """Prune environments and user data before walking disposable output."""
    root = root.resolve()
    removed_dirs: list[Path] = []
    removed_files: list[Path] = []

    def remove(path: Path) -> None:
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root)
            or (path / "pyvenv.cfg").is_file()
        ):
            return
        if path.is_dir():
            shutil.rmtree(path)
            removed_dirs.append(path)
        elif path.is_file():
            path.unlink()
            removed_files.append(path)

    for name in ROOT_ARTIFACTS:
        remove(root / name)
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories[:]:
            path = Path(parent) / name
            if (
                _preserved(path.relative_to(root))
                or name in {".git", "venv", "env"}
                or name.startswith(".venv")
                or (path / "pyvenv.cfg").is_file()
                or path.is_symlink()
                or not path.resolve().is_relative_to(root)
                or (path / ".git").exists()
            ):
                directories.remove(name)
            elif any(fnmatch.fnmatch(name, pattern) for pattern in DIR_PATTERNS):
                remove(path)
                directories.remove(name)
        for name in files:
            if any(fnmatch.fnmatch(name, pattern) for pattern in FILE_PATTERNS):
                remove(Path(parent) / name)
    return removed_dirs, removed_files


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

    removed_dirs, removed_files = clean_artifacts(ROOT)

    print(f"removed {len(removed_dirs)} directories, {len(removed_files)} files")
    for path in removed_dirs:
        print(f"  dir  {path.relative_to(ROOT)}")
    for path in removed_files:
        print(f"  file {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
