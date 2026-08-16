"""Plugin scaffolding: generate a complete, loadable MailFlow plugin.

The generator emits the canonical marketplace layout for one plugin — a
``plugin.json`` metadata file, ``pyproject.toml`` with a ``mailflow.plugins``
entry point, and a ``src/<package>/`` package whose component implements the
contract of the chosen category. The output registers cleanly with a
``PluginManager`` (this is what the marketplace validation workflow checks),
so a developer can scaffold, implement and open a pull request against the
plugin repository without ever touching another plugin's files.

Category templates (mirrors of the marketplace category folders):

- ``mail_source``  — MailSource: stream messages, send replies
- ``processor``    — MailProcessor: one step of the classification chain
- ``llm_backend``  — LLMBackend: chat-completions transport
- ``notifier``     — Notifier: deliver analyses to a channel
- ``storage``      — StorageBackend: durable persistence
- ``bot_exporter`` — BotExporter: generate a chatbot-framework plugin from a
  configured MailFlow instance (NoneBot, AstrBot, or any framework of yours)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CATEGORIES = ("mail_source", "processor", "llm_backend", "notifier", "storage", "bot_exporter")

_CATEGORY_LABEL = {
    "mail_source": "Mail source",
    "processor": "Mail processor",
    "llm_backend": "LLM backend",
    "notifier": "Notifier",
    "storage": "Storage backend",
    "bot_exporter": "Bot exporter",
}

_DESCRIPTION = {
    "mail_source": "MailFlow mail source: streams normalized messages into the pipeline",
    "processor": "MailFlow processor: one step of the ordered classification chain",
    "llm_backend": "MailFlow LLM backend: chat-completions transport",
    "notifier": "MailFlow notifier: delivers computed analyses to a channel",
    "storage": "MailFlow storage backend: durable persistence for records and drafts",
    "bot_exporter": "MailFlow bot exporter: turns a configured instance into a chatbot-framework plugin",
}

# plugin.json readme shown in the marketplace; the <<SPAN_COLOR>> placeholder
# demonstrates the `<span style="color:...">` rich-text support.
_README_TEMPLATE = """# <<NAME>>

<<DESCRIPTION>>

A MailFlow <<LABEL>> plugin scaffolded from the template. Replace this readme
with your plugin's documentation.

### Supported syntax

- **bold**, *italic*, ~~strikethrough~~ and `inline code`
- <span style="color:#ff5500">colored text via span tags</span>
- headings, lists, quotes and fenced code blocks

### Localization

Add translations under `descriptions` / `readmes` in `plugin.json`; MailFlow
picks the translation matching the active app language automatically.

- `descriptions`: locale code -> one-line summary (e.g. `"zh-CN"`)
- `readmes`: locale code -> translated markdown readme

### Development environment

```bash
# uv is the only tool you need; install it when missing (skipped if present)
curl -LsSf https://astral.sh/uv/install.sh | sh       # Linux / macOS
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
uv sync --group dev      # creates .venv with pytest/ruff/mypy
uv run pytest            # run your tests
```
"""


def _package_name(plugin_id: str) -> str:
    """mailflow-notify-foo -> mailflow_notify_foo (valid python module)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", plugin_id)


def _component_id(plugin_id: str) -> str:
    return plugin_id.removeprefix("mailflow-")


def _pyproject(plugin_id: str, description: str) -> str:
    package = _package_name(plugin_id)
    return f"""[project]
name = "{plugin_id}"
version = "0.1.0"
description = "{description}"
requires-python = ">=3.11"
dependencies = ["mailflow-core"]

[project.entry-points."mailflow.plugins"]
{plugin_id} = "{package}.plugin:plugin"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{package}"]

# One-command development environment: `uv sync --group dev` (uv installs
# itself on first use when missing, on every major platform).
[dependency-groups]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "ruff>=0.12",
  "mypy>=1.17",
]
"""


def _plugin_json(plugin_id: str, category: str, description: str, name: str) -> str:
    readme = (
        _README_TEMPLATE.replace("<<NAME>>", name)
        .replace("<<DESCRIPTION>>", description)
        .replace("<<LABEL>>", _CATEGORY_LABEL[category])
    )
    return json.dumps(
        {
            "id": plugin_id,
            "name": name,
            "version": "0.1.0",
            "description": description,
            "categories": [category],
            "package": plugin_id,
            "source": "",  # TODO: fill in e.g. git+https://.../<category>/<plugin_id>
            "author": "",  # TODO: your name / handle
            "license": "MIT",
            "readme": readme,
        },
        indent=2,
        ensure_ascii=False,
    )


def _plugin_module(plugin_id: str, category: str, description: str) -> str:
    """The category-specific plugin.py stub, loadable and registrable."""
    component_id = _component_id(plugin_id)
    framework_id = component_id.removeprefix("export-")
    kind_by_category = {
        "mail_source": "MAIL_SOURCE",
        "processor": "MAIL_PROCESSOR",
        "llm_backend": "LLM_BACKEND",
        "notifier": "NOTIFIER",
        "storage": "STORAGE",
        "bot_exporter": "BOT_EXPORTER",
    }
    common = f'''"""<<name>>: {_CATEGORY_LABEL[category].lower()} for MailFlow.

Scaffolded from the MailFlow plugin template — implement the TODO markers and
open a pull request against the plugin repository.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.plugin.{component_id}")

PLUGIN_INFO = PluginInfo(
    plugin_id="{plugin_id}",
    name="<<name>>",
    version="0.1.0",
    description="{description}",
    kinds=[ComponentKind.{kind_by_category[category]}],
)

'''
    module_by_category = {
        "mail_source": _MAIL_SOURCE_BODY,
        "processor": _PROCESSOR_BODY,
        "llm_backend": _LLM_BODY,
        "notifier": _NOTIFIER_BODY,
        "storage": _STORAGE_BODY,
        "bot_exporter": _BOT_EXPORTER_BODY,
    }
    body = (
        module_by_category[category]
        .replace("{component_id}", component_id)
        .replace("{framework_id}", framework_id)
    )
    tail = f"""
class {_CATEGORY_LABEL[category].split()[0]}Plugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
{body}


plugin = {_CATEGORY_LABEL[category].split()[0]}Plugin()

__all__ = ["PLUGIN_INFO", "plugin"]
"""
    return common.replace("<<name>>", _CATEGORY_LABEL[category]) + tail


_MAIL_SOURCE_BODY = """        registrar.add_source("{component_id}", {Category}Source)


class {Category}Source:
    \"\"\"Streams normalized messages; send replies through the provider.\"\"\"

    async def run(self, emit, stop_event: asyncio.Event) -> None:
        # TODO: poll your provider and emit(MailMessage(...)) until stop_event
        # is set. Example: emit a placeholder message once.
        # from mailflow.domain import MailAddress, MailMessage
        # emit(MailMessage(mail_id="1", subject="Hello", body="...",
        #                  sender=MailAddress("a@example.com", "A"),
        #                  recipients=[MailAddress("me@example.com", "Me")]))
        await asyncio.sleep(1)

    async def send_reply(self, mail_id: str, draft: Any) -> None:
        # TODO: send the confirmed reply (draft.subject / draft.body / draft.to)
        raise NotImplementedError

    async def close(self) -> None:
        pass""".replace("{Category}", "Mail")

_PROCESSOR_BODY = """        registrar.add_processor("{component_id}", {Category}Processor)


class {Category}Processor:
    \"\"\"One step of the ordered classification chain.\"\"\"

    processor_id = "{component_id}"

    async def process(self, mail: Any, context: Any) -> Any:
        # TODO: inspect mail.subject / mail.body and context, then return a
        # ProcessorResult (decision, analysis overlay, notes).
        from mailflow.contracts import ProcessorResult

        return ProcessorResult(notes=[f"processed by {self.processor_id}"])""".replace(
    "{Category}", "Mail"
)

_LLM_BODY = """        registrar.add_llm("{component_id}", {Category}Backend)


class {Category}Backend:
    \"\"\"Chat-completions transport for one model provider.\"\"\"

    backend_id = "{component_id}"

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> Any:
        # TODO: call your LLM provider with messages (list of {"role",
        # "content"}) and return an LLMCompletion(text=..., model=...,
        # backend_id=self.backend_id).
        from mailflow.contracts import LLMCompletion

        raise NotImplementedError""".replace("{Category}", "LLM")

_NOTIFIER_BODY = """        registrar.add_notifier("{component_id}", {Category}Notifier)


class {Category}Notifier:
    \"\"\"Delivers a computed mail analysis to a channel.\"\"\"

    def __init__(self, config: Any) -> None:
        self._config = config

    async def notify(self, record: Any) -> None:
        # TODO: deliver record.summary / record.effective_urgency to your
        # channel; skip gracefully when required options are missing.
        logger.info("notify via {component_id}: %s", record.summary)""".replace(
    "{Category}", "Channel"
)

_STORAGE_BODY = """        registrar.add_storage("{component_id}", {Category}Storage)


class {Category}Storage:
    \"\"\"Durable persistence. In-memory placeholder; replace with your backend.\"\"\"

    def __init__(self) -> None:
        self._mails: dict[str, Any] = {}
        self._trash: dict[str, Any] = {}
        self._drafts: dict[str, Any] = {}
        self._preferences: dict[str, str] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def save_mail(self, record: Any) -> None:
        self._mails[record.record_id] = record

    async def get_mail(self, record_id: str) -> Any:
        return self._mails.get(record_id)

    async def list_mails(self, limit: int | None = None) -> list[Any]:
        return list(self._mails.values())[:limit]

    async def count_mails(self) -> int:
        return len(self._mails)

    async def set_manual_urgency(self, record_id: str, urgency: Any) -> Any:
        record = self._mails.get(record_id)
        if record is None:
            return None
        record.manual_urgency = urgency
        return record

    async def delete_mail(self, record_id: str) -> None:
        record = self._mails.pop(record_id, None)
        if record is not None:
            self._trash[record_id] = record

    async def list_trash(self) -> list[Any]:
        return list(self._trash.values())

    async def restore_from_trash(self, record_id: str) -> Any:
        record = self._trash.pop(record_id, None)
        if record is not None:
            self._mails[record_id] = record
        return record

    async def purge_trash(self, before: Any) -> int:
        count = 0
        for record_id in [r for r, t in self._trash.items() if t.received_at < before]:
            del self._trash[record_id]
            count += 1
        return count

    async def cleanup_mail(self, before: Any) -> int:
        count = 0
        for record_id in [r for r, m in self._mails.items() if m.received_at < before]:
            record = self._mails.pop(record_id)
            self._trash[record_id] = record
            count += 1
        return count

    async def save_draft(self, draft: Any) -> None:
        self._drafts[draft.draft_id] = draft

    async def get_draft(self, draft_id: str) -> Any:
        return self._drafts.get(draft_id)

    async def delete_draft(self, draft_id: str) -> None:
        self._drafts.pop(draft_id, None)

    async def get_preference(self, key: str) -> str | None:
        return self._preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self._preferences[key] = value""".replace("{Category}", "Memory")

_BOT_EXPORTER_BODY = """        def export(context: Any) -> Any:
            # TODO: generate the chatbot-framework plugin package under
            # context.output_dir. context carries the resolved config
            # (context.config), the enabled plugin ids (context.plugin_ids)
            # and the active language (context.language). Return a
            # BotExportResult listing the files you wrote.
            from mailflow.bot_export import BotExportResult

            target = context.output_dir / "{component_id}"
            target.mkdir(parents=True, exist_ok=True)
            readme = target / "README.md"
            readme.write_text(
                "# MailFlow for " + "{framework_id}" + "\\n", encoding="utf-8"
            )
            return BotExportResult(
                framework="{framework_id}",
                plugin_name="{component_id}",
                created=["{component_id}/README.md"],
                notes="TODO: replace the placeholder with a real framework plugin",
            )

        registrar.add_bot_exporter("{framework_id}", export)""".replace("{Category}", "Bot")


def template_files(plugin_id: str, category: str, *, name: str = "") -> dict[str, str]:
    """Return the relative-path -> content map for a scaffolded plugin."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown template category {category!r}; choose one of {CATEGORIES}")
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", plugin_id):
        raise ValueError("plugin id must be lowercase letters/digits with dashes")
    display_name = name or _CATEGORY_LABEL[category]
    description = _DESCRIPTION[category]
    package = _package_name(plugin_id)
    return {
        "plugin.json": _plugin_json(plugin_id, category, description, display_name),
        "pyproject.toml": _pyproject(plugin_id, description),
        f"src/{package}/__init__.py": "",
        f"src/{package}/plugin.py": _plugin_module(plugin_id, category, description),
    }


def scaffold_plugin(
    target_dir: str | Path,
    plugin_id: str,
    category: str,
    *,
    name: str = "",
) -> Path:
    """Write a complete plugin template into ``target_dir`` (created if
    missing, subfolders allowed) and return the plugin directory."""
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    for relative, content in template_files(plugin_id, category, name=name).items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return target


__all__ = ["CATEGORIES", "scaffold_plugin", "template_files"]
