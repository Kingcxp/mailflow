"""Build a frozen MailFlow executable with Nuitka.

Usage:
    python tools/build_exe.py --mode standalone   # test this first
    python tools/build_exe.py --mode onefile      # only after a standalone smoke test

The official plugin set is included explicitly (mailflow-bundled registers it
via explicit module names), so frozen builds do not depend on entry-point metadata.
Data files (locale JSON, TUI stylesheet) are bundled with their packages.

Note: arbitrary post-build Python plugin discovery is not promised for frozen
mode — bundle any third-party plugins by adding them to the include list.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from mailflow_bundled import BUNDLED_PLUGINS

INCLUDE_PACKAGES = (
    "mailflow",
    "mailflow_bundled",
    "mailflow_cli",
    "mailflow_server",
    "mailflow_tui",
    *BUNDLED_PLUGINS,
)
ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("standalone", "onefile"),
        default="standalone",
        help="Nuitka distribution mode (default: standalone)",
    )
    parser.add_argument(
        "--download-tools", action="store_true", help="allow Nuitka to download compiler tools"
    )
    args = parser.parse_args()

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        f"--output-dir={ROOT / 'dist' / args.mode}",
        "--output-filename=mailflow.exe"
        if sys.platform == "win32"
        else "--output-filename=mailflow",
        "--enable-plugin=no-qt",
        "--no-deployment-flag=self-execution",  # all resources bundled; no re-exec
    ]
    if args.download_tools:
        cmd.append("--assume-yes-for-downloads")
    cmd.append("--standalone" if args.mode == "standalone" else "--onefile")
    for package in INCLUDE_PACKAGES:
        cmd.append(f"--include-package={package}")
    for package in ("mailflow", "mailflow_tui"):
        cmd.append(f"--include-package-data={package}")
    cmd.append(str(ROOT / "tools/frozen_entry.py"))
    subprocess.run(cmd, cwd=ROOT, check=True, stdin=subprocess.DEVNULL)
    print(f"frozen {args.mode} build written under dist/{args.mode}/")


if __name__ == "__main__":
    main()
