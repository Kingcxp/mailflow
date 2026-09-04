"""Extract one version's section from CHANGELOG.md for release notes.

Usage: python tools/changelog_for_release.py <version>
Prints the section body for ``## [<version>]``; exits 1 with a hint when
the section is missing (the workflow then falls back to a generic note).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def extract(version: str) -> str:
    text = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = re.compile(r"^## \[" + re.escape(version) + r"\]\s*$", re.MULTILINE)
    match = pattern.search(text)
    if match is None:
        raise SystemExit(f"no '## [{version}]' section in CHANGELOG.md")
    rest = text[match.end() :]
    next_section = re.search(r"^## \[", rest, re.MULTILINE)
    body = rest[: next_section.start()] if next_section else rest
    return body.strip()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(extract(sys.argv[1]))


if __name__ == "__main__":
    main()
