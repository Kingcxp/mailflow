"""Build all workspace packages into wheels via uv."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="use cached build dependencies only")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist/wheels")
    parser.add_argument("--version", help="require the release tag to match mailflow-core")
    args = parser.parse_args()
    if args.version:
        project = tomllib.loads((ROOT / "packages/mailflow-core/pyproject.toml").read_text("utf-8"))
        if project["project"]["version"] != args.version.removeprefix("v"):
            parser.error("release tag does not match mailflow-core version")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv executable not found on PATH")
    command = [
        uv,
        "build",
        "--all-packages",
        "--wheel",
        "--out-dir",
        str(args.out_dir),
        "--python",
        sys.executable,
    ]
    if args.offline:
        command.append("--offline")
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
