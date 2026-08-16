"""Emit GitHub Actions annotations for pytest failures from a junit.xml.

Used by the CI workflow so failed test names and messages surface in the
check-run annotations (readable without downloading logs). Cross-platform:
no shell heredocs, plain stdlib.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python tools/annotate_pytest_failures.py pytest.xml", file=sys.stderr)
        return 2
    root = ET.parse(sys.argv[1]).getroot()
    count = 0
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            for item in case.iter(kind):
                lines = (item.get("message") or "").strip().splitlines()
                message = lines[0][:500] if lines else kind
                print(f"::error file={case.get('classname') or ''}::{case.get('name')} {message}")
                count += 1
    return 1 if count else 0


if __name__ == "__main__":
    sys.exit(main())
