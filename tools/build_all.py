"""Build all workspace packages into wheels via uv."""

from __future__ import annotations

import shutil
import subprocess


def main() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv executable not found on PATH")
    subprocess.run([uv, "build", "--all-packages"], check=True)


if __name__ == "__main__":
    main()
