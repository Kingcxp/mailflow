"""Docs gate: mandatory documents must exist *and* must not name things that
do not exist.

Presence alone let stale docs rot silently (references to plugins that were
deleted, event names the runtime never emits, `make` targets that were
renamed). This gate additionally cross-checks every doc against the code.

`docs/build-log/BUILD_LOG.md` is exempt from the accuracy checks: it is a
historical record and legitimately mentions things that were later removed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORICAL = {"docs/build-log/BUILD_LOG.md", "CHANGELOG.md"}
"""Records of what happened; they legitimately name removed things."""

REQUIRED = (
    "README.md",
    "README.zh-CN.md",
    "AGENTS.md",
    "CHANGELOG.md",
    "docs/architecture/overview.md",
    "docs/architecture/domain-and-mail.md",
    "docs/architecture/plugin-system.md",
    "docs/architecture/pipeline.md",
    "docs/architecture/llm.md",
    "docs/architecture/logging.md",
    "docs/architecture/storage-and-retention.md",
    "docs/architecture/replies.md",
    "docs/architecture/tui.md",
    "docs/architecture/bot-export.md",
    "docs/development/setup.md",
    "docs/development/embedding.md",
    "docs/development/tests.md",
    "docs/development/quality.md",
    "docs/development/packaging.md",
    "docs/development/deployment.md",
    "docs/plugin-development/overview.md",
    "docs/plugin-development/mail-source.md",
    "docs/plugin-development/processor.md",
    "docs/plugin-development/llm-backend.md",
    "docs/plugin-development/notifier.md",
    "docs/plugin-development/storage.md",
    "docs/plugin-development/bot-exporter.md",
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


def _doc_files() -> list[Path]:
    """Every prose document, historical records excluded."""
    paths = sorted(ROOT.glob("docs/**/*.md"))
    paths += [ROOT / name for name in ("README.md", "README.zh-CN.md", "AGENTS.md", "CHANGELOG.md")]
    return [p for p in paths if p.relative_to(ROOT).as_posix() not in HISTORICAL and p.is_file()]


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def check_paths(docs: list[Path]) -> list[str]:
    """Backtick-quoted repository paths must exist."""
    pattern = re.compile(r"`((?:packages|plugins|tests|tools|translations)/[\w./-]+)`")
    problems: list[str] = []
    for doc in docs:
        for match in pattern.finditer(doc.read_text(encoding="utf-8")):
            target = match.group(1)
            if not (ROOT / target).exists():
                problems.append(f"{_rel(doc)}: path does not exist: {target}")
    return problems


def check_make_targets(docs: list[Path]) -> list[str]:
    """`make <target>` in a fenced command block must be a real target."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-zA-Z][\w-]*):", makefile, re.M))
    problems: list[str] = []
    for doc in docs:
        for match in re.finditer(r"^\s*(?:\$ )?make ([a-z][\w-]*)", doc.read_text("utf-8"), re.M):
            target = match.group(1)
            if target not in targets:
                problems.append(f"{_rel(doc)}: unknown make target: make {target}")
    return problems


def check_event_names(docs: list[Path]) -> list[str]:
    """A quoted `mailflow.*` event name must be emitted somewhere in core."""
    core = ROOT / "packages/mailflow-core/src/mailflow"
    emitted: set[str] = set()
    for source in core.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        emitted.update(re.findall(r'emit\(\s*f?"([\w.]+)"', text))
        emitted.update(
            f"mailflow.{name}"
            for name in re.findall(r'emit\(\s*f"\{_EVENT_PREFIX\}([\w.]+)"', text)
        )
    problems: list[str] = []
    for doc in docs:
        for match in re.finditer(
            r"`(mailflow\.(?:mail|action|cleanup|account|runtime|update)\.[\w.]+)`",
            doc.read_text("utf-8"),
        ):
            name = match.group(1)
            if name not in emitted:
                problems.append(f"{_rel(doc)}: event never emitted: {name}")
    return problems


def check_service_methods(docs: list[Path]) -> list[str]:
    """`service.<name>(` must be a real MailFlowService attribute."""
    source = (ROOT / "packages/mailflow-core/src/mailflow/service.py").read_text("utf-8")
    known = set(re.findall(r"^    (?:async )?def (\w+)", source, re.M))
    known.update(re.findall(r"^        self\.(\w+)(?::| =)", source, re.M))
    known.update({"commands", "market", "config", "storage", "runtime", "i18n", "events"})
    problems: list[str] = []
    for doc in docs:
        for match in re.finditer(r"`service\.(\w+)\(", doc.read_text("utf-8")):
            name = match.group(1)
            if name not in known:
                problems.append(f"{_rel(doc)}: no such service method: service.{name}()")
    return problems


def check_tracked_plugin_ids(docs: list[Path]) -> list[str]:
    """A `mailflow-<something>` id must be a real package or plugin.

    "Real" means it carries a `pyproject.toml` (or, in the marketplace, a
    `plugin.json`): a leftover directory holding only stale `__pycache__`
    files must not make a deleted plugin look alive.
    """

    def packaged(root: Path, pattern: str, *markers: str) -> set[str]:
        return {
            path.name
            for path in root.glob(pattern)
            if path.is_dir() and any((path / marker).is_file() for marker in markers)
        }

    known = packaged(ROOT / "plugins", "*", "pyproject.toml")
    known |= packaged(ROOT / "packages", "*", "pyproject.toml")
    sibling = ROOT.parent / "mailflow-repo"
    if not sibling.is_dir():
        return []  # cannot verify marketplace ids without the checkout
    known |= packaged(sibling, "*/mailflow-*", "plugin.json", "pyproject.toml")
    known |= {"mailflow-core", "mailflow-repo", "mailflow-workspace"}
    problems: list[str] = []
    for doc in docs:
        for match in re.finditer(r"`(mailflow-[a-z0-9-]+)`", doc.read_text("utf-8")):
            name = match.group(1)
            if name not in known:
                problems.append(f"{_rel(doc)}: unknown plugin/package id: {name}")
    return problems


def main() -> int:
    missing = [path for path in REQUIRED if not (ROOT / path).is_file()]
    if missing:
        print("mandatory documentation missing:")
        for path in missing:
            print(f"  - {path}")
        return 1

    docs = _doc_files()
    problems = [
        *check_paths(docs),
        *check_make_targets(docs),
        *check_event_names(docs),
        *check_service_methods(docs),
        *check_tracked_plugin_ids(docs),
    ]
    if problems:
        print("documentation is out of sync with the code:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(
        f"docs gate OK: {len(REQUIRED)} mandatory documents present; "
        f"{len(docs)} documents cross-checked against the code"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
