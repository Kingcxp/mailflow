"""Bot-framework export: turn a configured MailFlow instance into a plugin
for a chatbot framework (NoneBot, AstrBot, ...).

The exporters themselves are plugins: a plugin with kind ``bot_exporter``
registers one factory per target framework through
``registrar.add_bot_exporter(framework_id, factory)``. The factory receives
a :class:`BotExportContext` (the resolved config, the enabled plugin ids and
the target directory) and writes the framework plugin package, returning a
:class:`BotExportResult`. New frameworks arrive as plugins — the CLI, the
TUI and the make targets all share this single entry point, and developers
can ship their own migration plugin for any other framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mailflow.config import MailFlowConfig
from mailflow.domain import ComponentKind

if TYPE_CHECKING:
    from mailflow.registry import ComponentRegistry


@dataclass
class BotExportContext:
    """Everything an exporter factory needs to generate a framework plugin."""

    config: MailFlowConfig
    """The resolved live configuration (accounts, llms, plugins, ...)."""
    plugin_ids: list[str]
    """Enabled plugin ids; the generated plugin depends on these packages."""
    output_dir: Path
    """Directory the exporter writes the framework plugin package into."""
    version: str = ""
    """MailFlow version stamped into the generated metadata."""
    language: str = "en"
    """Active application language, for any generated text."""


@dataclass
class BotExportResult:
    """What an exporter factory produced."""

    framework: str
    """Target framework id (e.g. ``nonebot``)."""
    plugin_name: str
    """Name of the generated plugin package (e.g. ``nonebot-plugin-mailflow``)."""
    created: list[str] = field(default_factory=lambda: [])
    """Relative paths of the files written under ``output_dir``."""
    notes: str = ""
    """Human-readable notes (e.g. secret hygiene reminders)."""


def available_frameworks(registry: ComponentRegistry) -> list[str]:
    """Framework ids for which an exporter plugin is registered."""
    return registry.component_ids(ComponentKind.BOT_EXPORTER)


def export_bot_plugin(
    registry: ComponentRegistry,
    config: MailFlowConfig,
    *,
    framework: str,
    output_dir: Path,
    plugin_ids: list[str] | None = None,
    version: str = "",
    language: str = "en",
) -> BotExportResult:
    """Run the exporter registered for ``framework``.

    The exporter factory writes the framework plugin package into
    ``output_dir`` (created if missing). Raises ``KeyError`` when no
    exporter is registered for the framework.
    """
    factory = registry.bot_exporter_factory(framework)
    context = BotExportContext(
        config=config,
        plugin_ids=list(plugin_ids or []),
        output_dir=Path(output_dir),
        version=version,
        language=language,
    )
    return factory(context)


__all__ = [
    "BotExportContext",
    "BotExportResult",
    "available_frameworks",
    "export_bot_plugin",
]
