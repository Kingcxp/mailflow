"""Fail the docs gate when mandatory documents are missing."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED = (
    "README.md",
    "AGENTS.md",
    "CHANGELOG.md",
    "MAILFLOW_FROM_ZERO.md",
    "docs/architecture/overview.md",
    "docs/architecture/domain-and-mail.md",
    "docs/architecture/plugin-system.md",
    "docs/architecture/pipeline.md",
    "docs/architecture/llm.md",
    "docs/architecture/logging.md",
    "docs/architecture/storage-and-retention.md",
    "docs/architecture/replies.md",
    "docs/architecture/tui.md",
    "docs/development/setup.md",
    "docs/development/embedding.md",
    "docs/development/tests.md",
    "docs/development/quality.md",
    "docs/development/packaging.md",
    "docs/plugin-development/overview.md",
    "docs/plugin-development/mail-source.md",
    "docs/plugin-development/processor.md",
    "docs/plugin-development/llm-backend.md",
    "docs/plugin-development/notifier.md",
    "docs/plugin-development/storage.md",
    "docs/configuration/overview.md",
    "docs/configuration/i18n.md",
    "docs/agent/README.md",
    "docs/agent/invariants.md",
    "docs/agent/module-map.md",
    "docs/agent/change-playbook.md",
    "docs/adr/0001-uv-workspace.md",
    "docs/adr/0002-pluggy-pipeline.md",
    "docs/adr/0003-host-independent-core.md",
    "docs/build-log/BUILD_LOG.md",
)


def main() -> int:
    missing = [path for path in REQUIRED if not (ROOT / path).is_file()]
    if missing:
        print("mandatory documentation missing:")
        for path in missing:
            print(f"  - {path}")
        return 1
    print(f"docs gate OK: all {len(REQUIRED)} mandatory documents present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
