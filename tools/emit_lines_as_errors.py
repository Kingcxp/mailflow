"""Emit each stdin line as a GitHub Actions error annotation (debug aid)."""

from __future__ import annotations

import sys


def main() -> int:
    for raw in sys.stdin:
        line = raw.rstrip()
        if line:
            print(f"::error::{line[:400]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
