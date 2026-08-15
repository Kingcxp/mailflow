"""Build a frozen MailFlow executable with Nuitka.

Usage:
    python tools/build_exe.py --mode standalone   # test this first
    python tools/build_exe.py --mode onefile      # only after a standalone smoke test

The official plugin set is included explicitly (mailflow-bundled registers it
via static imports), so frozen builds do not depend on entry-point metadata.
Data files (locale JSON, TUI stylesheet) are bundled with their packages.

Note: arbitrary post-build Python plugin discovery is not promised for frozen
mode — bundle any third-party plugins by adding them to the include list.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

INCLUDE_PACKAGES = (
    "mailflow",
    "mailflow_bundled",
    "mailflow_cli",
    "mailflow_tui",
    "mailflow_mail_fake",
    "mailflow_storage_sqlite",
    "mailflow_llm_openai_compatible",
    "mailflow_processor_rules",
    "mailflow_processor_llm_importance",
    "mailflow_notify_console",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("standalone", "onefile"),
        default="standalone",
        help="Nuitka distribution mode (default: standalone)",
    )
    args = parser.parse_args()

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--assume-yes-for-downloads",
        "--output-dir=dist",
        "--enable-plugin=no-qt",
        "--no-deployment-flag=self-execution",  # all resources bundled; no re-exec
    ]
    cmd.append("--standalone" if args.mode == "standalone" else "--onefile")
    for package in INCLUDE_PACKAGES:
        cmd.append(f"--include-package={package}")
    for package in ("mailflow", "mailflow_tui"):
        cmd.append(f"--include-package-data={package}")
    cmd.append("tools/frozen_entry.py")
    subprocess.run(cmd, check=True)
    print(f"frozen {args.mode} build written under dist/")


if __name__ == "__main__":
    main()
