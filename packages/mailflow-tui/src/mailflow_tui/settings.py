"""VS Code-style settings surface for the TUI.

Every pane here is a thin client of ``mailflow.settings``:

- :class:`SettingsPane` — sidebar of sections (MailFlow's own plus one per
  plugin that owns options), a search box, and one card per option showing
  its name, description and an inline editor with Save / Restore-default.
- :class:`LLMPane` — the ordered LLM chain: the first entry is the default
  and each entry falls back to the ones below it; add / edit / delete / move.
- :class:`AccountsPane` — mailboxes plus the history browser: load mail that
  already arrived and push a selected subset through the pipeline.
- :class:`EntryFormScreen` — form window for one structured entry.
- :class:`ListEditScreen` — window for list and mapping values.

Invalid input surfaces as a message naming the offending option; nothing is
persisted unless the whole config re-validates.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections import deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, ClassVar, cast
from zoneinfo import ZoneInfo

from mailflow.config import LLMConfig
from mailflow.domain import ComponentKind, MailMessage
from mailflow.service import ConfigReloadError, MailFlowService
from mailflow.settings import (
    LIVE_CONFIG_GROUPS,
    EditorKind,
    OptionSpec,
    SettingsError,
    SettingsSection,
    coerce_value,
    entry_field_specs,
    entry_model,
)
from rich.text import Text
from textual import getters
from textual.app import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Label,
    ListItem,
    ListView,
    RichLog,
    Select,
    Static,
    Switch,
)
from textual.worker import WorkerCancelled

from mailflow_tui.labels import (
    error_detail,
    error_message,
    redact_operation_logs,
    relabel_buttons,
    secret_values,
    urgency_label,
)
from mailflow_tui.search import SearchMatcher, build_search_matcher
from mailflow_tui.view_state import TableView
from mailflow_tui.widgets import (
    Input,
    SafeSelect,
    SearchToggle,
    SecretRow,
    SelectionTable,
    TextArea,
)

_SECTION_LABELS = {
    "general": "tui.settings_section_general",
    "logging": "tui.settings_section_logging",
    "plugins": "tui.settings_section_plugins",
    "storage": "tui.settings_section_storage",
    "i18n": "tui.settings_section_i18n",
}

_MULTILINE_EDITORS = (EditorKind.STRING_LIST, EditorKind.MAPPING)

_INDEXED = re.compile(r"\[\d+\]")


def _local_time(service: MailFlowService, value: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Project persisted UTC values into the user's configured timezone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(service.config.general.timezone)).strftime(fmt)


def _slug(key: str) -> str:
    """Config key -> widget-id-safe token."""
    return re.sub(r"[^0-9a-zA-Z]+", "-", key).strip("-").lower()


def generic_key(key: str) -> str:
    """``llms[2].model`` -> ``llms[].model`` (description lookup)."""
    return _INDEXED.sub("[]", key)


def as_editor_text(editor: EditorKind, value: Any) -> str:
    """Render a list/mapping value as the one-entry-per-line editor text."""
    if editor is EditorKind.MAPPING and isinstance(value, dict):
        mapping: dict[Any, Any] = {**value}
        return "\n".join(f"{name} = {item}" for name, item in mapping.items())
    if isinstance(value, list):
        items: list[Any] = [*value]
        return "\n".join(str(item) for item in items)
    return "" if value is None else str(value)


def _table_id(event: Any) -> str:
    """Id of the DataTable that raised ``event`` ("" when unknown)."""
    table: Any = event.data_table
    return str(table.id or "")


def _select_text(select: Any) -> str:
    """Selected value of a Select as text ("" when nothing is selected)."""
    value: Any = select.value
    # textual 8 removed Select.BLANK (it now resolves to Widget.BLANK ==
    # False); the blank sentinel is Select.NULL
    if value is None or value is False or value is Select.NULL:
        return ""
    return str(value)


def default_text(service: MailFlowService, spec: OptionSpec) -> str:
    """Human-readable, localized rendering of an option's schema default."""
    default: Any = spec.default
    if isinstance(default, bool):
        return service.t("common.yes" if default else "common.no")
    if default is None or default == "" or default == [] or default == {}:
        return "-"
    if isinstance(default, list):
        entries = cast("list[Any]", default)
        return service.t("tui.settings_default_entries", count=len(entries))
    if isinstance(default, dict):
        keys = cast("dict[str, Any]", default)
        return service.t("tui.settings_default_keys", count=len(keys))
    text = str(default)
    # model defaults repr as ClassName(...): summarize instead of dumping,
    # the brackets would otherwise be parsed as markup and crash rendering
    if text.endswith(")") and "(" in text and not text.startswith(("'", '"')):
        name = text.split("(", 1)[0]
        return f"{name}(…)" if name[:1].isupper() else escape(text)
    return escape(text)


def _unwrap_listish(text: str) -> str:
    """Undo the damage of the old list-editor bug: values saved as
    "['404291187']" (a stringified list rendered as one item) come back
    as the bare id so the editor never nests another layer."""
    stripped = text.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        import ast as _ast

        try:
            parsed = _ast.literal_eval(stripped)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, list):
            items: list[Any] = [v for v in parsed]  # pyright: ignore[reportUnknownVariableType]
            inner = [str(v).strip() for v in items if str(v).strip()]
            return "\n".join(inner)
    return stripped


class ListEditScreen(ModalScreen[str | None]):
    """Edit a list or mapping value as text: one entry per line."""

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(
        self,
        service: MailFlowService,
        spec: OptionSpec,
        description: str = "",
        *,
        on_save: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._spec = spec
        self._description = description
        self._on_save = on_save
        self._saving = False
        self._bindings.bind("escape", "dismiss_modal", self._t("tui.btn_back"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        hint = (
            "tui.settings_mapping_hint"
            if self._spec.editor is EditorKind.MAPPING
            else "tui.settings_list_hint"
        )
        with Vertical(id="list-edit-dialog"):
            yield Static(self._t("tui.settings_edit_list"), classes="dialog-title")
            yield Static(f"[bold]{self._spec.key}[/bold]", id="list-edit-key")
            if self._description:
                yield Static(self._description, classes="dialog-hint")
            yield Static(self._t(hint), classes="dialog-hint")
            yield TextArea(as_editor_text(self._spec.editor, self._spec.value), id="list-edit-text")
            yield Static("", id="list-edit-status")
            with Horizontal(classes="dialog-actions"):
                yield Button(self._t("tui.btn_save"), id="list-edit-save", variant="success")
                yield Button(self._t("tui.btn_back"), id="list-edit-back", variant="primary")

    def action_dismiss_modal(self) -> None:
        if not self._saving:
            self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "list-edit-save":
            if not self._saving:
                self._saving = True
                self.run_worker(self._save(), group="list-save", exit_on_error=False)
        elif event.button.id == "list-edit-back":
            self.action_dismiss_modal()

    async def _save(self) -> None:
        status = self.query_one("#list-edit-status", Static)
        text = self.query_one("#list-edit-text", TextArea).text
        secrets = {text}
        with contextlib.suppress(SettingsError):
            secrets.update(secret_values({self._spec.label: coerce_value(self._spec, text)}))
        status.update(self._t("tui.form_saving"))
        for node in self.query("Button, TextArea"):
            node.disabled = True
        try:
            if self._on_save is not None:
                with redact_operation_logs(self._service, extra_secrets=secrets):
                    await self._on_save(text)
            self.dismiss(text)
        except Exception as exc:
            status.update(escape(error_message(self._service, exc, extra_secrets=secrets)))
        finally:
            self._saving = False
            for node in self.query("Button, TextArea"):
                node.disabled = False


class _Extra:
    """One provider-specific form field: id, label key suffix, widget kind,
    default and whether it lands in ``options`` (vs a top-level column)."""

    __slots__ = (
        "choices",
        "default",
        "description_key",
        "field_id",
        "into_options",
        "kind",
        "label",
        "label_key",
        "required",
        "secret",
    )

    def __init__(
        self,
        field_id: str,
        *,
        kind: str = "text",
        default: str = "",
        into_options: bool = True,
        secret: bool = False,
        choices: tuple[str, ...] = (),
        required: bool = False,
        label_key: str = "",
        description_key: str = "",
    ) -> None:
        self.field_id = field_id
        self.label = field_id.replace("_", " ")
        self.kind = kind  # text | password | choice | int | float | lines
        self.default = default
        self.into_options = into_options
        self.secret = secret
        self.choices = choices
        self.required = required
        self.label_key = label_key
        self.description_key = description_key


# config group -> ComponentKind, for looking up plugin-declared form fields
_GROUP_REGISTRY_KIND = {
    "accounts": ComponentKind.MAIL_SOURCE,
    "llms": ComponentKind.LLM_BACKEND,
    "processors": ComponentKind.MAIL_PROCESSOR,
    "notifiers": ComponentKind.NOTIFIER,
}

_LLM_PROVIDER_FIELDS: dict[str, tuple[_Extra, ...]] = {
    "openai-completions": (
        _Extra("base_url", default="https://api.openai.com/v1", into_options=False, required=True),
        _Extra("api_key", kind="password", into_options=False, secret=True),
        _Extra("max_tokens", kind="int"),
        _Extra("temperature", kind="float"),
    ),
    "openai-responses": (
        _Extra("base_url", default="https://api.openai.com/v1", into_options=False, required=True),
        _Extra("api_key", kind="password", into_options=False, secret=True),
        _Extra("max_tokens", kind="int"),
        _Extra("temperature", kind="float"),
    ),
    "openai-codex-responses": (
        _Extra(
            "base_url",
            default="https://chatgpt.com/backend-api",
            into_options=False,
            required=True,
        ),
        _Extra("api_key", kind="password", into_options=False, secret=True),
        _Extra("max_tokens", kind="int"),
        _Extra("temperature", kind="float"),
    ),
    "azure-openai-responses": (
        _Extra(
            "base_url",
            default="https://YOUR-RESOURCE.openai.azure.com",
            into_options=False,
            required=True,
        ),
        _Extra("api_key", kind="password", into_options=False, secret=True),
        _Extra("max_tokens", kind="int"),
        _Extra("temperature", kind="float"),
        _Extra("api_version", default="preview"),
    ),
    "anthropic-messages": (
        _Extra("base_url", default="https://api.anthropic.com", into_options=False, required=True),
        _Extra("api_key", kind="password", into_options=False, secret=True),
        _Extra("max_tokens", kind="int"),
        _Extra("temperature", kind="float"),
        _Extra("thinking_budget", kind="int"),
    ),
    "google-generative-ai": (
        _Extra(
            "base_url",
            default="https://generativelanguage.googleapis.com",
            into_options=False,
            required=True,
        ),
        _Extra("api_key", kind="password", into_options=False, secret=True),
        _Extra("max_tokens", kind="int"),
        _Extra("temperature", kind="float"),
        _Extra("thinking_budget", kind="int"),
    ),
    "google-vertex": (
        _Extra("project", required=True),
        _Extra("location", default="us-central1"),
        _Extra("service_account_file"),
        _Extra("api_key", kind="password", into_options=False, secret=True),
        _Extra("max_tokens", kind="int"),
        _Extra("temperature", kind="float"),
        _Extra("thinking_budget", kind="int"),
    ),
    "onebot": (
        _Extra("http_url", required=True),
        _Extra("access_token", kind="password", secret=True),
        _Extra("targets", kind="lines", required=True),
    ),
}
# the legacy alias behaves like plain completions
_LLM_PROVIDER_FIELDS["openai-compatible"] = _LLM_PROVIDER_FIELDS["openai-completions"]

_MINIMAL_CORE_FIELDS: dict[str, frozenset[str]] = {
    # Everything else (retries, fallback chains, raw mappings) stays editable in
    # config.toml or via `config set`; forms stay minimal. The timeout is part
    # of the form on purpose: a slow or cold model that cannot answer within the
    # default needs it raised, and hunting for it in TOML is not acceptable.
    "llms": frozenset({"llm_id", "model", "provider", "timeout_seconds", "max_retries"}),
    "accounts": frozenset({"account_id", "provider", "email", "enabled"}),
}

_IMAP_PRESET_HOSTS: dict[str, tuple[str, int, bool]] = {
    "qq": ("imap.qq.com", 993, True),
    "163": ("imap.163.com", 993, True),
    "outlook": ("outlook.office365.com", 993, True),
    "gmail": ("imap.gmail.com", 993, True),
}


def _imap_preset(options: dict[str, Any]) -> str:
    if options.get("preset"):
        return str(options["preset"])
    host = options.get("imap_host")
    if not host:
        return "qq"
    return next((key for key, values in _IMAP_PRESET_HOSTS.items() if values[0] == host), "custom")


class EntryFormScreen(ModalScreen[dict[str, Any] | None]):
    """Provider-aware form for one structured entry (mailbox, LLM, ...).

    The ``provider`` dropdown re-renders the provider-specific section, so
    users edit the selected provider's declared fields directly.
    """

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(
        self,
        service: MailFlowService,
        group: str,
        *,
        values: dict[str, Any] | None = None,
        hidden: tuple[str, ...] = (),
        on_save: Callable[[dict[str, Any]], Awaitable[bool | None]] | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._bindings.bind("escape", "dismiss_modal", self._t("tui.btn_back"))
        self._group = group
        self._values = deepcopy(values or {})
        self._on_save = on_save
        self._busy = ""
        self._operation: Any = None
        self._tested_inputs: dict[str, str] = {}
        self._provider_drafts: dict[str, dict[str, Any]] = {}
        self._editing = values is not None
        minimal = _MINIMAL_CORE_FIELDS.get(group)
        self._specs = [
            spec
            for spec in entry_field_specs(entry_model(group))
            if spec.label not in hidden
            and spec.editor is not EditorKind.STRUCT_LIST
            and (minimal is None or spec.label in minimal)
            and spec.editor not in _MULTILINE_EDITORS  # no hand-written TOML
        ]
        if group == "llms":
            self._default_provider = "openai-completions"
        elif group == "accounts":
            self._default_provider = "imap"
        elif group == "notifiers":
            # OneBot remains the default manual transport; registered
            # delivery channels and gateways are also selectable below.
            self._default_provider = "onebot"
        else:
            self._default_provider = ""
        if group == "llms":
            self._provider_choices = tuple(
                sorted(service.registry.component_ids(ComponentKind.LLM_BACKEND))
            )
        elif group == "accounts":
            self._provider_choices = tuple(
                sorted(service.registry.component_ids(ComponentKind.MAIL_SOURCE))
            )
        elif group == "notifiers":
            from mailflow_tui.notifications import NotificationsPane

            # gateway-auto-deploy platforms first, then manual ones; the
            # gateway id (napcat) is a separate choice from the manual
            # notifier id (onebot) so self-hosted users keep the form
            choices = set(NotificationsPane.IM_PROVIDERS)
            choices.update(service.registry.component_ids(ComponentKind.NOTIFIER))
            choices.update(service.gateway_providers())
            # Order: console → QQ (onebot, napcat) → WeChat → Telegram →
            # WhatsApp; ids outside this list still appear, sorted last
            _ORDER = (
                "console",
                "onebot",
                "napcat",
                "openwechat",
                "wechatpadpro",
                "telegram",
                "whatsapp",
            )
            self._provider_choices = tuple(p for p in _ORDER if p in choices) + tuple(
                sorted(choices - set(_ORDER))
            )
        else:
            self._provider_choices = ()
        if group == "notifiers":
            gateway = str(self._values.get("options", {}).get("gateway") or "")
            if gateway in self._provider_choices:
                self._values["provider"] = gateway
        self._initial_provider = str(self._values.get("provider") or self._default_provider)
        self._active_provider = self._initial_provider
        self._preset_values: dict[str, str] = {}

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def _field_label(self, field_id: str, label_key: str = "") -> str:
        key = label_key or f"tui.form_fields.{field_id}"
        label = self._t(key)
        return field_id.replace("_", " ") if label == key else label

    def _describe(self, spec: OptionSpec) -> str:
        key = f"config.desc.{self._group}[].{spec.label}"
        translated = self._service.t(key)
        return spec.description if translated == key else translated

    # -- helpers -------------------------------------------------------------

    def _current_provider(self) -> str:
        if self._group not in ("llms", "accounts", "notifiers"):
            return ""
        try:
            node = self.query_one("#field-provider")
            if isinstance(node, Select):
                selected = _select_text(node)
            elif isinstance(node, Input):
                selected = node.value
            else:
                selected = ""
        except Exception:
            selected = ""
        return selected or str(self._values.get("provider", "")) or self._default_provider

    def _choice_label(self, choice: Any) -> str:
        """Localized provider label (e.g. 'QQ (OneBot)' vs
        'QQ (NapCat auto-deploy)'); unknown ids stay raw."""
        value = str(choice)
        key = f"tui.bots_provider_{value}"
        translated = self._service.t(key)
        return translated if translated != key else value

    def _extras_for(self, provider: str) -> tuple[_Extra, ...]:
        extras = self._provider_extras_for(provider)
        if self._group != "notifiers":
            return extras
        existing = {field.field_id for field in extras}
        capabilities = self._service.notifier_capabilities(provider)
        return (
            extras
            + tuple(
                _Extra(name, kind="boolean", default=default, label_key="ux." + name)
                for name, default in (
                    ("mail_notifications", "true"),
                    ("task_reminders", "true"),
                    ("daily_digest", "true"),
                    ("hourly_summary", "false"),
                    ("notification_images", "false"),
                    ("summary_cards", "false"),
                )
                if name not in existing and name in capabilities
            )
            + (
                (
                    _Extra(
                        "send_interval_seconds",
                        kind="number",
                        default="2",
                        label_key="delivery.send_interval",
                    ),
                )
                if "send_interval_seconds" not in existing
                else ()
            )
        )

    def _provider_extras_for(self, provider: str) -> tuple[_Extra, ...]:
        plugin_extras = self._plugin_extras(provider)
        if plugin_extras is not None:
            return plugin_extras
        if self._group == "llms":
            return _LLM_PROVIDER_FIELDS.get(provider, ())
        if self._group == "accounts" and provider == "imap":
            return (
                _Extra("preset", default="qq"),
                _Extra("username"),
                _Extra("password", kind="password", secret=True, required=True),
                _Extra("imap_folder", default="INBOX"),
                _Extra("interval_seconds", default="300"),
                _Extra("limit", default="20"),
            )
        if self._group == "notifiers":
            if self._gateway_for(provider) is not None:
                # gateway-backed platform (napcat auto-deploy): admins are
                # the platform user ids allowed to run chat commands (QQ
                # number / wxid), one per line; everything else
                # (subscriptions, mail queries) happens via chat commands
                # like <prefix>mailflow subscribe
                return (
                    _Extra("admins", kind="lines", description_key="tui.extras_admins"),
                    _Extra("targets", kind="lines"),
                )
            if provider == "onebot":
                return (
                    _Extra("http_url", required=True),
                    _Extra("access_token", kind="password", secret=True),
                    _Extra("targets", required=True),
                    _Extra("admins", kind="lines", description_key="tui.extras_admins"),
                )
        return ()

    def _plugin_extras(self, provider: str) -> tuple[_Extra, ...] | None:
        """Plugin-declared form fields, converted to _Extra; None when the
        provider declares none (fall back to the hardcoded extras)."""
        kind = _GROUP_REGISTRY_KIND.get(self._group)
        if kind is None:
            return None
        fields = self._service.registry.form_fields(kind, provider)
        if not fields:
            return None
        extras: list[_Extra] = []
        for field in fields:
            kind_map = {
                "string": "text",
                "password": "password",
                "number": "number",
                "boolean": "boolean",
                "list": "lines",
                "select": "choice",
                "textarea": "textarea",
            }
            extras.append(
                _Extra(
                    field.field_id,
                    kind=kind_map.get(field.kind, "text"),
                    default="" if field.default is None else str(field.default),
                    into_options=field.into_options,
                    secret=field.secret or field.kind == "password",
                    required=field.required
                    and not (self._group == "llms" and field.field_id == "api_key")
                    and not (
                        self._group == "notifiers"
                        and not self._editing
                        and self._gateway_for(provider) is not None
                        and field.field_id in {"gateway_url", "base_url", "http_url"}
                    ),
                    choices=tuple(field.choices),
                    label_key=field.label_key,
                    description_key=field.description_key,
                )
            )
        return tuple(extras)

    def _gateway_for(self, provider: str) -> str | None:
        """The gateway provisioner id backing a notifier provider.

        ``napcat`` (auto-deploy) maps 1:1 to its gateway provisioner;
        ``onebot`` is the manual notifier id and has no gateway. Existing
        gateway entries select their gateway before the form is built."""
        if provider in self._service.gateway_providers():
            return provider
        return None

    def _sync_actions(self) -> None:
        """Match the visible actions to the provider and current operation."""
        save_btn = self.query_one_optional("#entry-form-save", Button)
        provider = self._current_provider()
        gateway = (
            self._group == "notifiers"
            and self._gateway_for(provider) is not None
            and not self._editing
        )
        next_btn = self.query_one_optional("#entry-form-next", Button)
        test_btn = self.query_one_optional("#entry-form-test", Button)
        if next_btn is not None:
            next_btn.display = gateway
            next_btn.disabled = bool(self._busy)
        if test_btn is not None:
            test_btn.display = not gateway
            test_btn.disabled = self._busy == "saving" or (
                self._group == "notifiers"
                and provider
                not in {"onebot", "telegram", "openwechat", "wechatpadpro", "whatsapp", "console"}
                and self._service.registry.probe(ComponentKind.NOTIFIER, provider) is None
            )
            test_btn.label = self._t(
                "tui.btn_cancel" if self._busy == "testing" else "tui.btn_test"
            )
        if save_btn is not None:
            save_btn.display = not gateway
            save_btn.disabled = bool(self._busy)
        back = self.query_one_optional("#entry-form-back", Button)
        if back is not None:
            back.disabled = self._busy == "saving"
        for node in self.query(
            "#entry-form-fields Input, #entry-form-fields Select, #entry-form-fields Switch, #entry-form-fields TextArea, #entry-form-fields Button"
        ):
            node.disabled = self._busy == "saving"

    async def on_mount(self) -> None:
        # initial button states for the default provider
        self._sync_actions()

    def _core_value(self, label: str) -> Any:
        spec = next((s for s in self._specs if s.label == label), None)
        value = self._values.get(label, spec.default if spec else None)
        return "" if value is None else value

    def _extra_value(self, extra: _Extra) -> str:
        provider = self._current_provider()
        draft = self._provider_drafts.get(provider, {})
        if extra.field_id in draft:
            raw = draft[extra.field_id]
        else:
            original = self._values if provider == self._initial_provider else {}
            pool: dict[str, Any] = dict(original.get("options") or {})
            fallback = original.get(extra.field_id)
            raw = fallback if not extra.into_options else pool.get(extra.field_id, fallback)
            if (
                raw is None
                and self._group == "accounts"
                and extra.field_id.startswith(("imap_", "smtp_"))
            ):
                from mailflow_mail_imap.plugin import PRESETS

                preset = str(pool.get("preset") or (provider if provider in PRESETS else "qq"))
                raw = PRESETS.get(preset, {}).get(extra.field_id)
            if extra.field_id == "preset":
                raw = _imap_preset(pool)
            if raw is None or raw == "":
                raw = extra.default
        if extra.kind == "lines" and isinstance(raw, (list, tuple)):
            # list fields flow back as a real list (admins/targets); the
            # lines editor consumes one string — join without str()-wrapping
            # (str(['a']) rendered the whole list as ONE line item)
            raw_items: list[Any] = [x for x in raw]  # pyright: ignore[reportUnknownVariableType]
            cleaned = [str(item).strip() for item in raw_items if str(item).strip()]
            return "\n".join(cleaned)
        return str(raw)

    # -- composition -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        title_key = "tui.entry_form_edit" if self._editing else "tui.entry_form_new"
        group_label = self._t(f"tui.group_{self._group}")
        if group_label == f"tui.group_{self._group}":
            group_label = self._group
        with Vertical(id="entry-form-dialog"):
            yield Static(self._t(title_key, group=group_label), classes="dialog-title")
            with ScrollableContainer(id="entry-form-fields"):
                for spec in self._specs:
                    yield from self._field_widgets(spec)
                with Vertical(id="entry-form-extras"):
                    yield from self._render_extras()
            yield Static("", id="entry-form-status")
            with Horizontal(classes="dialog-actions"):
                if self._group == "llms" or self._group == "accounts":
                    yield Button(self._t("tui.btn_test"), id="entry-form-test", variant="primary")
                if self._group == "notifiers":
                    # gateway-backed platforms continue into the guided
                    # setup (Next); manual platforms test the connection
                    yield Button(self._t("tui.btn_next"), id="entry-form-next", variant="primary")
                    yield Button(self._t("tui.btn_test"), id="entry-form-test", variant="primary")
                yield Button(
                    self._t("tui.btn_save"), id="entry-form-save", variant="success", disabled=True
                )
                yield Button(self._t("tui.btn_back"), id="entry-form-back", variant="primary")

    def _field_widgets(self, spec: OptionSpec) -> ComposeResult:
        widget_id = f"field-{_slug(spec.label)}"
        current = self._values.get(spec.label, spec.default)
        choices = spec.choices
        if spec.label == "provider" and self._provider_choices:
            choices = self._provider_choices
        yield Label(
            f"{self._field_label(spec.label)}{' *' if spec.required else ''}", classes="field-label"
        )
        description = self._describe(spec)
        if description:
            yield Static(escape(description), classes="field-desc")
        if spec.editor is EditorKind.BOOLEAN:
            yield Switch(value=bool(current), id=widget_id)
        elif spec.editor is EditorKind.CHOICE or (spec.label == "provider" and choices):
            choice_values = [str(c) for c in choices]
            initial = str(current) if str(current) in choice_values else None
            if spec.label == "provider" and not self._editing:
                # a brand-new entry defaults to the most common transport
                initial = self._default_provider or initial
            # the default must be one of the options: Textual Select validates
            # the value at mount and raises InvalidSelectValueError otherwise
            # (e.g. a notifier form whose default "console" is not a listed
            # IM/gateway provider). Fall back to NULL so Textual picks the
            # first option instead of crashing.
            if initial is not None and str(initial) not in choice_values:
                initial = None
            # literal None is an illegal Select value: NULL makes Textual pick
            # the first option when blank is not allowed, instead of crashing
            initial_value: Any = initial if initial is not None else Select.NULL
            yield SafeSelect(
                [(self._choice_label(choice), str(choice)) for choice in choices],
                value=initial_value,
                id=widget_id,
                allow_blank=False,
            )
        elif spec.editor in _MULTILINE_EDITORS:
            yield TextArea(
                as_editor_text(spec.editor, current), id=widget_id, classes="field-multiline"
            )
        elif spec.secret:
            with SecretRow(classes="secret-row"):
                yield Input(
                    value="" if current is None else str(current),
                    id=widget_id,
                    password=True,
                )
                yield Button(self._t("tui.eye_hide"), id=f"{widget_id}-eye", classes="eye-btn")
        else:
            yield Input(value="" if current is None else str(current), id=widget_id)

    def _render_extras(self) -> ComposeResult:
        provider = self._current_provider()
        extras = self._extras_for(provider)
        if extras:
            yield Label(self._t("tui.provider_section", provider=provider), classes="field-label")
        for extra in extras:
            widget_id = f"extra-{_slug(extra.field_id)}"
            marker = " *" if extra.required else ""
            yield Label(
                self._field_label(extra.field_id, extra.label_key) + marker, classes="field-label"
            )
            desc_key = (
                extra.description_key or f"tui.extras_{_slug(extra.field_id).replace('-', '_')}"
            )
            translated_desc = self._t(desc_key)
            if translated_desc != desc_key:
                yield Static(escape(translated_desc), classes="field-desc")
            if extra.kind == "choice":
                _choice_names = tuple(extra.choices) or tuple(_IMAP_PRESET_HOSTS)
                value = self._extra_value(extra) or (_choice_names[0] if _choice_names else "")
                if extra.field_id == "preset":
                    self._preset_values[provider] = value
                yield SafeSelect(
                    [
                        (
                            self._t("ux.initial_" + name)
                            if extra.field_id == "initial_sync"
                            else name,
                            name,
                        )
                        for name in _choice_names
                    ],
                    value=value,
                    id=widget_id,
                    allow_blank=False,
                )
            elif extra.kind == "lines":
                from mailflow_tui.list_editor import ListEditor

                raw_lines = self._extra_value(extra)
                current_lines = [
                    _unwrap_listish(ln.strip())
                    for ln in str(raw_lines or "").splitlines()
                    if ln.strip()
                ]
                yield ListEditor(
                    current_lines,
                    placeholder=str(extra.default or self._t("tui.list_editor_placeholder")),
                    add_label=self._t("tui.list_add"),
                    remove_label=self._t("tui.list_remove"),
                    id=widget_id,
                )
            elif extra.kind == "boolean":
                yield Switch(
                    value=str(self._extra_value(extra)).strip().lower()
                    in ("1", "true", "yes", "on"),
                    id=widget_id,
                )
            elif extra.kind == "textarea" and not extra.secret:
                yield TextArea(self._extra_value(extra), id=widget_id, classes="field-multiline")
            elif extra.secret:
                # mounted via mount_all (outside compose), so children are
                # attached explicitly instead of with-block composition
                row = SecretRow(classes="secret-row")
                row.compose_add_child(
                    Input(
                        value=self._extra_value(extra),
                        id=widget_id,
                        password=True,
                        placeholder=extra.default,
                    )
                )
                row.compose_add_child(
                    Button(self._t("tui.eye_hide"), id=f"{widget_id}-eye", classes="eye-btn")
                )
                yield row
            else:
                yield Input(value=self._extra_value(extra), id=widget_id, placeholder=extra.default)

    async def _rebuild_extras(self) -> None:
        container = self.query_one_optional("#entry-form-extras", Vertical)
        if container is None:
            return
        # the removal must complete BEFORE new widgets mount, otherwise a
        # same-id child is still present and Textual raises DuplicateIds
        await container.remove_children()
        await container.mount_all(list(self._render_extras()))

    def _remember_extras(self) -> None:
        from mailflow_tui.list_editor import ListEditor

        draft: dict[str, Any] = {}
        for extra in self._extras_for(self._active_provider):
            node = self.query_one_optional(f"#extra-{_slug(extra.field_id)}")
            if isinstance(node, ListEditor):
                draft[extra.field_id] = node.value()
            elif isinstance(node, TextArea):
                draft[extra.field_id] = node.text
            elif isinstance(node, Select):
                select = cast("Select[Any]", node)
                draft[extra.field_id] = select.value
            elif isinstance(node, (Input, Switch)):
                draft[extra.field_id] = node.value
        self._provider_drafts[self._active_provider] = draft

    # -- collection ---------------------------------------------------------------

    def _collect_core(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for spec in self._specs:
            selector = f"#field-{_slug(spec.label)}"
            node: Any | None = self.query_one_optional(selector)
            if node is None:
                continue
            if isinstance(node, Switch):
                values[spec.label] = node.value
            elif isinstance(node, Select):
                selected = _select_text(node)
                if selected:
                    values[spec.label] = coerce_value(spec, selected)
            elif isinstance(node, TextArea):
                values[spec.label] = coerce_value(spec, node.text)
            else:
                text = node.value
                if spec.secret and text == "":
                    continue  # leave existing/blank secret untouched
                if spec.required and not text.strip():
                    raise SettingsError(
                        spec.label,
                        self._t("tui.field_required", field=self._field_label(spec.label)),
                        code="tui.field_required",
                        params={"field": self._field_label(spec.label)},
                    )
                values[spec.label] = coerce_value(spec, text)
        return values

    def _collect_extras(self, *, validate: bool = True) -> dict[str, Any]:
        provider = self._current_provider()
        extras = self._extras_for(provider)
        original = self._values if provider == self._initial_provider else {}
        collected: dict[str, Any] = {"options": dict(original.get("options") or {})}
        for extra in extras:
            target = collected["options"] if extra.into_options else collected
            previous = target.pop(extra.field_id, None)
            node: Any | None = self.query_one_optional(f"#extra-{_slug(extra.field_id)}")
            raw = ""
            if isinstance(node, Select):
                raw = _select_text(node)
            elif isinstance(node, TextArea):
                raw = node.text
            elif isinstance(node, Input):
                raw = node.value
            if extra.field_id == "preset":
                preset = raw.strip() or "qq"
                if preset in _IMAP_PRESET_HOSTS:
                    if preset != _imap_preset(dict(original.get("options") or {})):
                        for key in (
                            "imap_host",
                            "imap_port",
                            "imap_ssl",
                            "smtp_host",
                            "smtp_port",
                            "smtp_ssl",
                        ):
                            target.pop(key, None)
                    target["preset"] = preset
                    host, port, ssl_flag = _IMAP_PRESET_HOSTS[preset]
                    for key, default in (
                        ("imap_host", host),
                        ("imap_port", port),
                        ("imap_ssl", ssl_flag),
                    ):
                        target.setdefault(key, default)
                elif preset != "custom":
                    target["preset"] = preset
                continue
            from mailflow_tui.list_editor import ListEditor

            if isinstance(node, ListEditor):
                items = node.value()
                if items:
                    target[extra.field_id] = items
                elif extra.required and validate:
                    raise SettingsError(
                        extra.field_id,
                        self._t(
                            "tui.field_required",
                            field=self._field_label(extra.field_id, extra.label_key),
                        ),
                        code="tui.field_required",
                        params={"field": self._field_label(extra.field_id, extra.label_key)},
                    )
                continue
            if isinstance(node, Switch):
                target = collected["options"] if extra.into_options else collected
                target[extra.field_id] = bool(node.value)
                continue
            text = raw.strip()
            if not text:
                if extra.secret:
                    previous = previous or original.get(extra.field_id)
                    if previous:
                        target[extra.field_id] = previous
                        continue
                if extra.required and validate:
                    raise SettingsError(
                        extra.field_id,
                        self._t(
                            "tui.field_required",
                            field=self._field_label(extra.field_id, extra.label_key),
                        ),
                        code="tui.field_required",
                        params={"field": self._field_label(extra.field_id, extra.label_key)},
                    )
                continue
            numeric_fields = ("interval_seconds", "limit", "max_tokens", "thinking_budget")
            target = collected["options"] if extra.into_options else collected
            try:
                value_num: Any
                if extra.kind == "float" or extra.field_id == "temperature":
                    value_num = float(text)
                elif extra.kind == "int" or extra.field_id in numeric_fields:
                    value_num = int(text)
                elif extra.kind == "number":
                    number = float(text)
                    value_num = int(number) if number.is_integer() else number
                else:
                    value_num = text
                target[extra.field_id] = value_num
            except ValueError as exc:
                raise SettingsError(
                    extra.field_id,
                    self._t(
                        "tui.field_must_be_number",
                        field=self._field_label(extra.field_id, extra.label_key),
                    ),
                    code="tui.field_must_be_number",
                    params={"field": self._field_label(extra.field_id, extra.label_key)},
                ) from exc
        if (
            validate
            and provider == "google-vertex"
            and not collected["options"].get("service_account_file")
            and not collected.get("api_key")
        ):
            raise SettingsError(
                "credential",
                self._t("tui.google_vertex_credentials"),
                code="tui.google_vertex_credentials",
                params={},
            )
        return collected

    def _collect(self) -> dict[str, Any]:
        values = dict(self._values)
        if self._current_provider() != self._initial_provider:
            for name in (
                "options",
                "base_url",
                "api_key",
                "api_key_env",
                "headers",
                "query",
                "extra_body",
            ):
                values.pop(name, None)
        values.update(self._collect_core())
        values.update(self._collect_extras())
        if values.get("api_key_env") and values.get("api_key") != self._values.get("api_key"):
            values["api_key_env"] = None
        return values

    # -- llm connectivity test ---------------------------------------------------

    async def _test_llm(self) -> None:
        status = self.query_one("#entry-form-status", Static)
        try:
            values = self._collect()
        except SettingsError as exc:
            status.update(f"[red]{escape(error_detail(self._service, exc))}[/red]")
            return
        llm_id = str(values.get("llm_id") or "test")
        config = LLMConfig(
            llm_id=llm_id,
            provider=str(values.get("provider") or ""),
            base_url=str(values.get("base_url") or ""),
            api_key=str(values.get("api_key") or ""),
            model=str(values.get("model") or ""),
            headers=dict(values.get("headers") or {}),
            query=dict(values.get("query") or {}),
            options=dict(values.get("options") or {}),
            # a cold local model can take a while before its first token, so the
            # probe gets a per-request budget well above the old 20s default
            timeout_seconds=60.0,
            max_retries=0,
        )
        provider = config.provider
        from mailflow.contracts import LLMBackend

        backend: LLMBackend
        try:
            factory = self._service.registry.llm_factory(provider)
            backend = factory(config)
        except Exception as exc:
            status.update(
                f"[red]{escape(error_message(self._service, exc, extra_secrets=self._secrets()))}[/red]"
            )
            return
        status.update(self._t("tui.llm_testing_connect", provider=config.provider))
        started = datetime.now()
        try:
            completion = await asyncio.wait_for(
                backend.chat([{"role": "user", "content": "ping"}], temperature=0.0),
                timeout=120.0,
            )
        except TimeoutError:
            status.update(f"[red]{self._t('tui.llm_test_timeout')}[/red]")
            return
        except Exception as exc:
            status.update(
                f"[red]{escape(error_message(self._service, exc, max_chars=300, extra_secrets=(config.api_key, *config.headers.values())))}[/red]"
            )
            return
        finally:
            close = getattr(backend, "aclose", None) or getattr(backend, "close", None)
            if callable(close):
                import inspect

                result = close()
                if inspect.isawaitable(result):
                    await result
        elapsed = (datetime.now() - started).total_seconds()
        # raw model output is intentionally not shown — latency, the model
        # name and the routed provider are enough to judge connectivity
        status.update(
            f"[green]{self._t('tui.llm_test_ok_model', seconds=f'{elapsed:.1f}', model=escape(completion.model or '-'))}[/green]"
        )

    async def _test_account(self) -> None:
        """Verify mailbox credentials by logging into the real backend."""
        status = self.query_one("#entry-form-status", Static)
        try:
            values = self._collect()
        except SettingsError as exc:
            status.update(f"[red]{escape(error_detail(self._service, exc))}[/red]")
            return
        provider = str(values.get("provider") or "")
        probe = self._service.registry.probe(ComponentKind.MAIL_SOURCE, provider)
        if probe is not None:
            message = await probe(dict(values.get("options") or {}), self._service.t)
            status.update(
                escape(error_detail(self._service, message, extra_secrets=self._secrets()))
            )
            return
        if provider not in {"imap", "gmail", "outlook"}:
            status.update(f"[yellow]{self._t('tui.account_test_skip')}[/yellow]")
            return
        opts = dict(values.get("options") or {})
        from mailflow_mail_imap.plugin import PRESETS

        preset = str(opts.get("preset") or (provider if provider in PRESETS else ""))
        opts = {**PRESETS.get(preset, {}), **opts}
        host = str(opts.get("imap_host") or "")
        port = int(opts.get("imap_port") or 993)
        user = str(opts.get("username") or values.get("email") or "")
        password = str(opts.get("password") or "")
        use_ssl = bool(opts.get("imap_ssl", True))
        if not host or not user or not password:
            raise SettingsError(
                "credentials",
                self._t("tui.account_credentials_required"),
                code="tui.account_credentials_required",
                params={},
            )
        status.update(self._t("tui.account_testing", host=host))
        import imaplib
        import smtplib
        import threading

        clients: list[Any] = []
        cancelled = threading.Event()

        def _probe() -> str:
            client_type = imaplib.IMAP4_SSL if use_ssl else imaplib.IMAP4
            client = client_type(host, port, timeout=10.0)
            clients.append(client)
            try:
                if cancelled.is_set():
                    return "cancelled"
                client.login(user, password)
            finally:
                with contextlib.suppress(Exception):
                    client.logout()
            if cancelled.is_set():
                return "cancelled"
            smtp_host = str(opts.get("smtp_host") or "")
            if not smtp_host:
                return self._t("ux.account_send_missing")
            sender: Any = None
            try:
                smtp_ssl = bool(opts.get("smtp_ssl", True))
                sender = (smtplib.SMTP_SSL if smtp_ssl else smtplib.SMTP)(
                    smtp_host,
                    int(opts.get("smtp_port") or (465 if smtp_ssl else 587)),
                    timeout=10,
                )
                clients.append(sender)
                if cancelled.is_set():
                    return "cancelled"
                if not smtp_ssl:
                    sender.starttls()
                sender.login(user, password)
                return self._t("ux.account_send_ready")
            except Exception as exc:
                return self._t(
                    "ux.account_send_failed",
                    error=error_detail(self._service, exc, extra_secrets=(password,)),
                )
            finally:
                if sender is not None:
                    with contextlib.suppress(Exception):
                        sender.quit()

        try:
            send_status = await asyncio.wait_for(asyncio.to_thread(_probe), timeout=25.0)
        except TimeoutError:
            status.update(f"[red]{self._t('tui.account_test_timeout')}[/red]")
            return
        except Exception as exc:
            detail = escape(
                error_detail(self._service, exc, max_chars=200, extra_secrets=(password,))
            )
            status.update(f"[red]{self._t('tui.account_test_failed', reason=detail)}[/red]")
            return
        finally:
            cancelled.set()
            for client in clients:
                with contextlib.suppress(Exception):
                    close = getattr(client, "shutdown", None) or getattr(client, "close", None)
                    if callable(close):
                        close()
        status.update(escape(self._t("ux.account_receive_ready") + " · " + send_status))

    async def _test_notifier(self) -> None:
        """Use the current provider's credentials and declared probe."""
        from mailflow_tui.notifications import NotifierProbe

        provider = self._current_provider()
        options = dict(self._collect_extras(validate=False).get("options") or {})
        probe = self._service.registry.probe(ComponentKind.NOTIFIER, provider)
        if probe is not None:
            message = await probe(options, self._service.t)
        else:
            message = await NotifierProbe.probe(provider, options, self._service.t)
        self.query_one("#entry-form-status", Static).update(
            escape(error_detail(self._service, message, extra_secrets=self._secrets()))
        )

    # -- events -----------------------------------------------------------------

    def _secrets(self) -> tuple[str, ...]:
        ids = {f"field-{_slug(spec.label)}" for spec in self._specs if spec.secret}
        ids.update(
            f"extra-{_slug(extra.field_id)}"
            for extra in self._extras_for(self._current_provider())
            if extra.secret
        )
        return tuple(node.value for node in self.query(Input) if node.id in ids and node.value)

    async def _run_operation(self, *, save: bool = False, guided: bool = False) -> None:
        with redact_operation_logs(self._service, extra_secrets=self._secrets()):
            await self._run_form_operation(save=save, guided=guided)

    async def _run_form_operation(self, *, save: bool = False, guided: bool = False) -> None:
        self._busy = "saving" if save else "testing"
        self._sync_actions()
        status = self.query_one("#entry-form-status", Static)
        status.update(self._t("tui.form_saving" if save else "tui.form_testing"))
        try:
            if save:
                values = self._collect()
                if guided:
                    values["_guided"] = True
                if self._on_save is not None and await self._on_save(values) is False:
                    status.update(self._t("tui.form_cancelled"))
                    return
                self.dismiss(values)
            elif self._group == "notifiers":
                await self._test_notifier()
            elif self._group == "accounts":
                await self._test_account()
            else:
                await self._test_llm()
        except asyncio.CancelledError:
            status.update(self._t("tui.form_cancelled"))
            raise
        except Exception as exc:
            status.update(
                f"[red]{escape(error_message(self._service, exc, extra_secrets=self._secrets()))}[/red]"
            )
        finally:
            self._busy = ""
            self._sync_actions()

    async def _cancel_test(self) -> None:
        if self._busy == "testing" and self._operation is not None:
            self._operation.cancel()
            with contextlib.suppress(WorkerCancelled):
                await self._operation.wait()
            self._busy = ""
            self._sync_actions()
            self.query_one("#entry-form-status", Static).update(self._t("tui.form_cancelled"))

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "field-provider":
            await self._cancel_test()
            self._remember_extras()
            self._active_provider = self._current_provider()
            await self._rebuild_extras()
            self.query_one("#entry-form-status", Static).update("")
            self._sync_actions()
        elif event.select.id == "extra-preset":
            provider, selected = self._current_provider(), _select_text(event.select)
            if self._preset_values.get(provider) == selected:
                return
            self._preset_values[provider] = selected
            await self._cancel_test()
            from mailflow_mail_imap.plugin import PRESETS

            preset = PRESETS.get(selected)
            if preset:
                for field_id, value in preset.items():
                    node = self.query_one_optional(f"#extra-{_slug(field_id)}")
                    if isinstance(node, Input):
                        node.value = str(value)
                    elif isinstance(node, Switch):
                        node.value = bool(value)

    async def on_input_changed(self, event: Input.Changed) -> None:
        if (
            event.input.id
            and event.input.id.startswith(("field-", "extra-"))
            and self._tested_inputs.get(event.input.id) != event.input.value
        ):
            await self._cancel_test()

    async def action_dismiss_modal(self) -> None:
        if self._busy == "saving":
            return
        await self._cancel_test()
        self.dismiss(None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.endswith("-eye"):
            input_id = button_id[: -len("-eye")]
            try:
                secret_input = self.query_one(f"#{input_id}", Input)
            except Exception:
                return
            secret_input.password = not secret_input.password
            event.button.label = self._t(
                "tui.eye_hide" if secret_input.password else "tui.eye_show"
            )
            event.stop()
            return
        if button_id == "entry-form-back":
            await self.action_dismiss_modal()
            return
        if button_id == "entry-form-test":
            if self._busy == "testing":
                await self._cancel_test()
                return
            if self._busy:
                return
            self._tested_inputs = {node.id: node.value for node in self.query(Input) if node.id}
            self._busy = "testing"
            self._operation = self.run_worker(
                self._run_operation(), group="entry-operation", exit_on_error=False
            )
            return
        if button_id not in ("entry-form-save", "entry-form-next") or self._busy:
            return
        self._busy = "saving"
        self._operation = self.run_worker(
            self._run_operation(save=True, guided=button_id == "entry-form-next"),
            group="entry-operation",
            exit_on_error=False,
        )


class OptionCard(Vertical):
    """One setting: name, description, inline editor, Save / Restore-default."""

    def __init__(self, service: MailFlowService, spec: OptionSpec, description: str) -> None:
        super().__init__(classes="option-card", id=f"card-{_slug(spec.key)}")
        self._service = service
        self.spec = spec
        self._description = description

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        spec = self.spec
        slug = _slug(spec.key)
        title = f"[bold]{spec.key}[/bold]"
        if not spec.is_default():
            title += f"  [dim]({self._t('tui.settings_modified')})[/dim]"
        yield Static(title, classes="option-key")
        if self._description:
            yield Static(self._description, classes="option-desc")
        yield Static(
            self._t("tui.settings_default_hint", value=default_text(self._service, spec)),
            classes="option-default",
        )
        with Horizontal(classes="option-row"):
            yield from self._editor_widgets(slug)
            yield Button(
                self._t("tui.btn_save"),
                id=f"save-{slug}",
                variant="success",
                classes="option-button",
            )
            yield Button(
                self._t("tui.btn_reset_default"),
                id=f"reset-{slug}",
                variant="warning",
                classes="option-button",
            )

    def _editor_widgets(self, slug: str) -> ComposeResult:
        spec = self.spec
        widget_id = f"value-{slug}"
        if spec.editor is EditorKind.BOOLEAN:
            yield Switch(value=bool(spec.value), id=widget_id, classes="option-input")
        elif spec.editor is EditorKind.CHOICE:
            yield SafeSelect(
                [(choice, choice) for choice in spec.choices],
                value=str(spec.value) if spec.value in spec.choices else Select.NULL,
                id=widget_id,
                classes="option-input",
            )
        elif spec.editor in _MULTILINE_EDITORS:
            yield Static(spec.display_value(), id=widget_id, classes="option-summary")
            yield Button(
                self._t("tui.btn_edit"),
                id=f"list-{slug}",
                variant="primary",
                classes="option-button",
            )
        else:
            yield Input(
                value="" if spec.value is None else str(spec.value),
                id=widget_id,
                password=spec.secret,
                classes="option-input",
            )

    def entered_value(self) -> Any:
        """Current editor content, ready for ``service.set_setting``."""
        selector = f"#value-{_slug(self.spec.key)}"
        if self.spec.editor is EditorKind.BOOLEAN:
            return self.query_one(selector, Switch).value
        if self.spec.editor is EditorKind.CHOICE:
            return _select_text(self.query_one(selector, Select))
        return self.query_one(selector, Input).value


class SettingsPane(Vertical):
    """Sidebar of sections, a search box, and one card per option."""

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._sections: list[SettingsSection] = []
        self._active = ""
        self._query = ""
        self._settings_filter_generation = 0
        self._settings_matcher = SearchMatcher("")
        self._render_lock = asyncio.Lock()
        self._reload_lock = asyncio.Lock()
        self._drafts: dict[str, Any] = {}
        self._saved_keys: set[str] = set()
        self._saving = False

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        with Horizontal(id="settings-search-row"):
            yield Input(
                placeholder=self._t("tui.settings_search_placeholder"), id="settings-search"
            )
            yield SearchToggle("regex", ident="settings-search-regex")
            yield SearchToggle("case", ident="settings-search-case")
            if not getattr(self._service, "remote", False):
                yield Button(self._t("ux.backup_title"), id="settings-backup")
                yield Button(self._t("ux.remote_connection"), id="settings-connection")
        with Horizontal(id="settings-body"):
            with Vertical(id="settings-sidebar"):
                yield Static(self._t("tui.settings_sections"), id="settings-sidebar-title")
                yield ListView(id="settings-sections")
            with Vertical(id="settings-main"):
                # language switching lives in the general.language option
                # card (dropdown of loaded packs) — no separate row
                yield Static("", id="settings-status")
                yield ScrollableContainer(id="settings-options")

    async def on_mount(self) -> None:
        # settings_sections + card construction are heavy: paint the pane
        # first and let the exclusive worker fill it in
        self.run_worker(self.reload(), exclusive=True, group="settings-reload", exit_on_error=False)

    async def relabel(self) -> None:
        self.query_one("#settings-search", Input).placeholder = self._t(
            "tui.settings_search_placeholder"
        )
        self.query_one("#settings-sidebar-title", Static).update(self._t("tui.settings_sections"))
        await self.reload()

    # -- language ----------------------------------------------------------

    async def refresh_languages(self) -> None:
        select = self.query_one_optional("#language-select", Select)  # pyright: ignore[reportUnknownVariableType]
        if select is None:
            return
        # a concurrent pane remount can detach the Select between the lookup
        # and the poke; its watcher's internal '#label' query would raise
        # NoMatches and kill the app — swallow exactly that race
        from textual.css.query import QueryError

        try:
            select.set_options(  # pyright: ignore[reportUnknownMemberType]
                [
                    (f"{info.name} ({info.code})", info.code)
                    for info in self._service.i18n.available_languages()
                ]
            )
            if select.value != self._service.i18n.language:  # pyright: ignore[reportUnknownMemberType]
                select.value = self._service.i18n.language
        except QueryError:
            pass

    # -- sections ----------------------------------------------------------

    def _section_title(self, section: SettingsSection) -> str:
        label_key = _SECTION_LABELS.get(section.section_id)
        if label_key is not None:
            return self._t(label_key)
        if section.plugin_id or not section.title:
            # uniform naming: plugin sections read as their lowercase-dash id
            return section.section_id
        return section.title

    def _describe(self, spec: OptionSpec) -> str:
        key = f"config.desc.{generic_key(spec.key)}"
        translated = self._service.t(key)
        return spec.description if translated == key else translated

    async def reload(self) -> None:
        """Rebuild sidebar and cards from the current config.

        Serialized: a save and a language-changed relabel can fire two
        reloads at once, and interleaved clear/append passes duplicated
        every sidebar entry."""
        async with self._reload_lock:
            await self._reload_unlocked()

    async def _reload_unlocked(self) -> None:
        if getattr(self._service, "remote", False):
            try:
                await cast(Any, self._service).refresh_settings()
            except Exception as exc:
                self._set_status(f"[red]{escape(error_message(self._service, exc))}[/red]")
                return
        self._sections = self._service.settings_sections()
        # the sidebar ListView may not exist yet when reload is triggered by
        # on_mount or an event handler racing compose — wait briefly instead
        # of raising MountError
        listing = self.query_one_optional("#settings-sections", ListView)
        for _ in range(100):
            if listing is not None or not self.is_mounted:
                break
            await asyncio.sleep(0.02)
            listing = self.query_one_optional("#settings-sections", ListView)
        if listing is None or not self._sections:
            return
        ids = {section.section_id for section in self._sections}
        if self._active not in ids:
            self._active = self._sections[0].section_id
        await listing.clear()
        for index, section in enumerate(self._sections):
            await listing.append(ListItem(Static(self._section_title(section))))
            if section.section_id == self._active:
                listing.index = index
        await self._render_options()

    def _current_matcher(self) -> SearchMatcher:
        regex = self.query_one_optional("#settings-search-regex", SearchToggle)
        case = self.query_one_optional("#settings-search-case", SearchToggle)
        return build_search_matcher(
            self._query,
            regex=bool(regex.value) if regex is not None else False,
            case_sensitive=bool(case.value) if case is not None else False,
        )

    def _visible_options(self, matcher: SearchMatcher) -> list[OptionSpec]:
        if matcher.query:
            return [
                spec
                for section in self._sections
                for spec in section.options
                if matcher.matches(f"{spec.key} {self._describe(spec)}")
            ]
        section = next((s for s in self._sections if s.section_id == self._active), None)
        return list(section.options) if section is not None else []

    async def _render_options(self) -> None:
        matcher = self._current_matcher()
        if matcher.error:
            self._set_status(f"[red]{self._t('tui.invalid_regex')}: {escape(matcher.error)}[/red]")
            return
        self._settings_matcher = matcher
        async with self._render_lock:
            container = self.query_one_optional("#settings-options", ScrollableContainer)
            if container is None:
                return
            scroll_offset = (container.scroll_x, container.scroll_y)
            for card in container.query(OptionCard):
                if card.spec.editor in _MULTILINE_EDITORS:
                    continue
                key = card.spec.key
                value = card.entered_value()
                expected = (
                    card.spec.value
                    if card.spec.editor is EditorKind.BOOLEAN
                    else str(card.spec.value if card.spec.value is not None else "")
                )
                if key in self._saved_keys:
                    self._drafts.pop(key, None)
                elif value != expected:
                    self._drafts[key] = value
            self._saved_keys.clear()
            options = self._visible_options(matcher)
            await container.remove_children()
            if not options:
                await container.mount(
                    Static(self._t("tui.settings_no_matches"), classes="settings-empty")
                )
                return
            for offset in range(0, len(options), 50):
                batch = [
                    OptionCard(self._service, spec, self._describe(spec))
                    for spec in options[offset : offset + 50]
                ]
                await container.mount_all(batch)
                for card in batch:
                    for node in card.query(".option-button, .option-input"):
                        node.disabled = self._saving
                    if card.spec.key in self._drafts:
                        node = card.query_one(f"#value-{_slug(card.spec.key)}")
                        if isinstance(node, (Input, Switch, Select)):
                            with node.prevent(Select.Changed, Switch.Changed, Input.Changed):
                                node.value = self._drafts[card.spec.key]
                if offset + 50 < len(options):
                    await asyncio.sleep(0)
            container.call_after_refresh(
                lambda: container.scroll_to(
                    x=scroll_offset[0], y=scroll_offset[1], animate=False, force=True
                )
            )

    def _schedule_settings_filter(self) -> None:
        self._settings_filter_generation += 1
        generation = self._settings_filter_generation

        async def _filter() -> None:
            await asyncio.sleep(0.3)
            if generation == self._settings_filter_generation:
                await self._render_options()

        self.run_worker(
            cast(Any, _filter), exclusive=True, group="settings-filter", exit_on_error=False
        )

    def _set_status(self, text: str) -> None:
        node = self.query_one_optional("#settings-status", Static)
        if node is not None:
            node.update(text)

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "settings-search":
            self._query = event.value
            self._schedule_settings_filter()

    async def on_search_toggle_toggled(self, event: SearchToggle.Toggled) -> None:
        if event.toggle.id in {"settings-search-regex", "settings-search-case"}:
            self._schedule_settings_filter()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "settings-sections":
            return
        index = event.list_view.index or 0
        if 0 <= index < len(self._sections):
            self._active = self._sections[index].section_id
            await self._render_options()

    def _card_of(self, button: Button) -> OptionCard | None:
        node: Any = button.parent
        while node is not None and not isinstance(node, OptionCard):
            node = node.parent
        return node

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in {"settings-backup", "settings-connection"}:
            from mailflow_tui.mail_workflows import DataManagementScreen, RemoteConnectionScreen

            screen = (
                DataManagementScreen
                if event.button.id == "settings-backup"
                else RemoteConnectionScreen
            )
            cast("Any", self).app.push_screen(screen(self._service))
            return
        button_id = event.button.id or ""
        card = self._card_of(event.button)
        if card is None:
            return
        event.stop()
        if button_id.startswith("list-"):
            await self._edit_list(card)
        elif button_id.startswith("save-"):
            self.run_worker(
                self._save(card.spec.key, card.entered_value()),
                group="settings-save",
                exit_on_error=False,
            )
        elif button_id.startswith("reset-"):
            self.run_worker(self._reset(card.spec.key), group="settings-save", exit_on_error=False)

    async def _edit_list(self, card: OptionCard) -> None:
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ListEditScreen(
                self._service,
                card.spec,
                self._describe(card.spec),
                on_save=lambda value: self._change_setting(card.spec.key, value, propagate=True),
            ),
        )

    async def _save(self, key: str, value: Any) -> None:
        await self._change_setting(key, value)

    async def _reset(self, key: str) -> None:
        await self._change_setting(key, None, reset=True)

    async def _change_setting(
        self, key: str, value: Any, *, reset: bool = False, propagate: bool = False
    ) -> None:
        if self._saving:
            if propagate:
                raise RuntimeError(self._t("tui.form_saving"))
            return
        self._saving = True
        secrets = secret_values({key: value})
        spec = self._service.settings_option(key)
        if spec is not None:
            with contextlib.suppress(SettingsError):
                secrets.update(secret_values({spec.label: coerce_value(spec, value)}))
        self._set_status(self._t("tui.form_saving"))
        for node in self.query(".option-button, .option-input"):
            node.disabled = True
        try:
            warning = ""
            try:
                with redact_operation_logs(self._service, extra_secrets=secrets):
                    if reset:
                        await self._service.reset_setting(key)
                    else:
                        await self._service.set_setting(key, value)
            except ConfigReloadError as exc:
                warning = error_detail(self._service, exc)
            self._saved_keys.add(key)
            self._drafts.pop(key, None)
            await self.reload()
            message = warning or self._t(
                "tui.settings_reset_done" if reset else "tui.settings_saved", option=key
            )
            if not warning:
                message += " · " + self._t(
                    "ux.config_active"
                    if key.split(".")[0] in LIVE_CONFIG_GROUPS
                    else "ux.config_restart"
                )
            self._set_status(escape(message))
        except Exception as exc:
            message = error_message(self._service, exc, extra_secrets=secrets)
            self._set_status(f"[red]{escape(message)}[/red]")
            if propagate:
                raise
        finally:
            self._saving = False
            for node in self.query(".option-button, .option-input"):
                node.disabled = False


class _NotifyFeed:
    """Rich notification feed shown under the LLM chain table.

    Subscribes to ``mailflow.mail.processed`` and renders one colored entry
    per processed mail (all urgency levels, strongest last). Replaces the
    former raw request log — LLM activity now lives in the main Logs tab."""

    MAX_ENTRIES = 100

    _STYLES: ClassVar[dict[str, str]] = {
        "urgent": "bold #F56C6C",
        "important": "#E6A23C",
        "info": "#67C23A",
        "ad": "#909399",
    }

    def __init__(self, service: MailFlowService, pane: Any) -> None:
        self._service = service
        self._pane = pane
        self._entries: deque[Text] = deque(maxlen=self.MAX_ENTRIES)

    def start(self) -> None:
        self._unsubscribe = self._service.events.subscribe(
            "mailflow.mail.processed", self._on_processed
        )

    def stop(self) -> None:
        stop = getattr(self, "_unsubscribe", None)
        if stop is not None:
            with contextlib.suppress(Exception):
                stop()

    async def _on_processed(self, **payload: Any) -> None:
        record = payload.get("record")
        if record is None:
            return
        urgency = str(getattr(record, "effective_urgency", "info"))
        style = self._STYLES.get(urgency, "white")
        mail = getattr(record, "mail", None)
        stamp = getattr(mail, "received_at", None)
        when = _local_time(self._service, stamp, "%m-%d %H:%M") if stamp is not None else ""
        subject = str(getattr(mail, "subject", "") or "")
        line = Text.assemble(
            (when + "  ", "dim"),
            (urgency_label(self._service, urgency), style),
            ("  ", ""),
            (subject[:60], "bold"),
        )
        summary = str(getattr(getattr(record, "analysis", None), "summary", ""))
        if summary:
            line.append("\n")
            line.append(summary[:160], "dim")
        self._entries.append(line)
        with contextlib.suppress(Exception):
            self._pane.app.call_later(self._pane._append_notify_entry, line)

    def snapshot(self) -> list[Text]:
        return list(self._entries)


_CLICK_WINDOW_SECONDS = 0.5
"""Double-click window for list rows (this textual has no RowActivated)."""


_double_click_state: tuple[str, str, float] | None = None
"""Last (table, row, monotonic time) click for _double_clicked."""


def _double_clicked(stamp_key: str, row_key: str) -> bool:
    """True on the SECOND selection of the same row within the window."""
    global _double_click_state
    import time as _time

    now = _time.monotonic()
    stamp = _double_click_state
    if stamp is not None:
        last_table, last_key, last_time = stamp
        if (
            last_table == stamp_key
            and last_key == row_key
            and now - last_time <= _CLICK_WINDOW_SECONDS
        ):
            _double_click_state = None
            return True
    _double_click_state = (stamp_key, row_key, now)
    return False


class LLMPane(Vertical):
    """The ordered LLM chain: first entry is default, the rest are fallbacks."""

    app = getters.app(cast("type[App[Any]]", App))

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._selected: int | None = None
        # a programmatic cursor restore must not be mistaken for the user
        # highlighting another row
        self._suppress_highlight = False
        self._mutating = False

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Static(self._t("tui.llms_title"), id="llms-title")
        yield Static(self._t("tui.llms_help"), id="llms-help")
        yield DataTable(id="llms-table")
        with Horizontal(id="llms-actions"):
            yield Button(self._t("tui.btn_new"), id="llm-add", variant="success")
            yield Button(self._t("tui.btn_edit"), id="llm-edit", variant="primary", disabled=True)
            yield Button(self._t("tui.btn_delete"), id="llm-delete", variant="error", disabled=True)
            yield Button(self._t("tui.btn_move_up"), id="llm-up", variant="primary", disabled=True)
            yield Button(
                self._t("tui.btn_move_down"), id="llm-down", variant="primary", disabled=True
            )
        with Vertical(id="notify-wrap"):
            yield Static(self._t("tui.notify_title"), id="notify-title")
            yield RichLog(id="notify-feed", wrap=True, markup=False, highlight=False)
        yield Static("", id="llms-status")

    async def on_mount(self) -> None:
        await self.reload()
        self._notify_feed = _NotifyFeed(self._service, self)
        self._notify_feed.start()
        for entry in self._notify_feed.snapshot():
            with contextlib.suppress(Exception):
                self.query_one("#notify-feed", RichLog).write(entry)

    def on_unmount(self) -> None:
        feed = getattr(self, "_notify_feed", None)
        if feed is not None:
            feed.stop()

    def _append_notify_entry(self, entry: Text) -> None:
        """Write one entry incrementally — clear()+rewrite on every mail
        churned the UI loop hard enough to starve Textual pilots in e2e."""
        with contextlib.suppress(Exception):
            self.query_one("#notify-feed", RichLog).write(entry)

    async def relabel(self) -> None:
        self._columns_done = False
        relabel_buttons(
            self,
            {
                "llm-add": "tui.btn_new",
                "llm-edit": "tui.btn_edit",
                "llm-delete": "tui.btn_delete",
                "llm-up": "tui.btn_move_up",
                "llm-down": "tui.btn_move_down",
            },
        )
        self.query_one("#notify-title", Static).update(self._t("tui.notify_title"))
        await self.reload()

    def _table(self) -> DataTable[Any] | None:
        return self.query_one_optional("#llms-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    def _sync_controls(self) -> None:
        index = self._selected
        total = len(self._service.config.llms)
        selected = index is not None and 0 <= index < total
        enabled = {
            "llm-add": True,
            "llm-edit": selected,
            "llm-delete": selected,
            "llm-up": selected and index is not None and index > 0,
            "llm-down": selected and index is not None and index < total - 1,
        }
        for ident, available in enabled.items():
            button = self.query_one_optional(f"#{ident}", Button)
            if button is not None:
                button.disabled = self._mutating or not available

    def _set_status(self, text: str) -> None:
        node = self.query_one_optional("#llms-status", Static)
        if node is not None:
            node.update(text)

    def _ensure_columns(self) -> None:
        if getattr(self, "_columns_done", False):
            return
        table = self._table()
        if table is None:
            return
        for key in ("order", "llm", "model", "provider", "role", "default_col"):
            with contextlib.suppress(Exception):
                table.remove_column(key)
        table.add_column(self._t("tui.llms_column_order"), key="order")
        table.add_column(self._t("plugin.header_id"), key="llm")
        table.add_column(self._t("llm.header_backend"), key="provider")
        table.add_column(self._t("llm.header_model"), key="model")
        table.add_column(self._t("tui.llms_column_default"), key="default_col")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        self._columns_done = True

    async def reload(self) -> None:
        table = self._table()
        if table is None:
            return
        view = TableView.capture(table, str(self._selected) if self._selected is not None else None)
        table.clear()
        self._ensure_columns()
        llms = self._service.config.llms
        for position, llm in enumerate(llms):
            default_marker = (
                self._t("tui.llms_default_marker") if llm.default or position == 0 else ""
            )
            table.add_row(
                str(position + 1),
                escape(llm.llm_id),
                escape(llm.provider),
                escape(llm.model),
                default_marker,
                key=str(position),
            )
        if not llms:
            self._set_status(self._t("tui.llms_empty"))
            self._selected = None
            self._sync_controls()
            return
        # clear() parks the cursor on row 0, and the highlight event would then
        # overwrite the selection: put the cursor back where the user was so a
        # move or delete can be repeated without re-selecting the row
        if self._selected is not None and 0 <= self._selected < len(llms):
            self._suppress_highlight = True
            try:
                view.restore(table)
            finally:
                self._suppress_highlight = False
        else:
            selected = view.restore(table)
            self._selected = int(selected) if selected is not None else None
        self._sync_controls()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if _table_id(event) == "llms-table" and event.row_key.value is not None:
            if self._suppress_highlight:
                return
            self._selected = int(str(event.row_key.value))
            self._sync_controls()
            if _double_clicked("llms", str(event.row_key.value)):
                self._open_form(self._selected)

    async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if (
            _table_id(event) == "llms-table"
            and event.row_key.value is not None
            and not self._suppress_highlight
        ):
            self._selected = int(str(event.row_key.value))
            self._sync_controls()

    async def on_data_table_row_activated(self, event: Any) -> None:
        """Enter on an LLM row opens the edit form (double-click handled
        by _register_row_click in row_selected)."""
        if _table_id(event) == "llms-table" and event.row_key.value is not None:
            self._selected = int(str(event.row_key.value))
            self._open_form(self._selected)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.disabled:
            return
        button_id = event.button.id or ""
        if button_id == "llm-add":
            self._open_form(None)
            return
        if button_id not in ("llm-edit", "llm-delete", "llm-up", "llm-down"):
            return
        index = self._selected
        if index is None or index >= len(self._service.config.llms):
            self._set_status(f"[yellow]{self._t('tui.entry_pick_first')}[/yellow]")
            return
        if button_id == "llm-edit":
            self._open_form(index)
            return
        if button_id == "llm-delete":
            await self._mutate(self._service.remove_config_entry("llms", index), "entry_removed")
            # keep the selection on the neighbouring row so several entries can
            # be deleted in a row without re-selecting
            remaining = len(self._service.config.llms)
            self._selected = min(index, remaining - 1) if remaining else None
            await self.reload()
            return
        offset = -1 if button_id == "llm-up" else 1
        target = index + offset
        total = len(self._service.config.llms)
        if target < 0 or target >= total:
            self._set_status(
                f"[yellow]{self._t('tui.llms_already_first' if offset < 0 else 'tui.llms_already_last')}[/yellow]"
            )
            return
        # select the entry's new position *before* the reload so the cursor
        # follows it, letting the user press move-up repeatedly; a rejected move
        # puts the cursor back on the entry, which did not move
        self._selected = target
        if not await self._mutate(self._service.move_config_entry("llms", index, offset), ""):
            self._selected = index
            await self.reload()

    def _open_form(self, index: int | None) -> None:
        values = self._service.config.llms[index].model_dump() if index is not None else None

        async def save(result: dict[str, Any]) -> None:
            warning = ""
            try:
                if index is None:
                    await self._service.add_config_entry("llms", result)
                else:
                    await self._service.update_config_entry("llms", index, result)
            except ConfigReloadError as exc:
                warning = error_detail(self._service, exc)
            await self.reload()
            message = warning or self._t(
                "tui.entry_added" if index is None else "tui.entry_updated",
                group=self._t("tui.tab_llms"),
            )
            self._set_status(escape(message))

        def after(result: dict[str, Any] | None) -> None:
            if result is None:
                self._set_status(self._t("tui.form_cancelled"))

        app = self.app
        app.push_screen(
            EntryFormScreen(
                self._service, "llms", values=values, hidden=("default", "fallback"), on_save=save
            ),
            callback=after,
        )  # pyright: ignore[reportUnknownMemberType]

    async def _add(self, values: dict[str, Any]) -> None:
        await self._mutate(self._service.add_config_entry("llms", values), "entry_added")

    async def _update(self, index: int, values: dict[str, Any]) -> None:
        await self._mutate(
            self._service.update_config_entry("llms", index, values), "entry_updated"
        )

    async def _mutate(self, action: Any, message_key: str) -> bool:
        """Apply one settings mutation; False when it was rejected."""
        self._mutating = True
        self._sync_controls()
        try:
            await action
        except SettingsError as exc:
            message = self._t(
                "tui.settings_invalid", option=exc.option, reason=error_detail(self._service, exc)
            )
            self._set_status(f"[red]{escape(message)}[/red]")
            self.notify(message, severity="error", timeout=8)
            return False
        except Exception as exc:
            message = error_message(self._service, exc)
            self._set_status(f"[red]{escape(message)}[/red]")
            self.notify(message, severity="error", timeout=8)
            return False
        finally:
            self._mutating = False
            self._sync_controls()
        if message_key:
            self._set_status(
                f"[green]{self._t(f'tui.{message_key}', group=self._t('tui.tab_llms'))}[/green]"
            )
        await self.reload()
        return True


class AccountsPane(Vertical):
    """Mailboxes plus the history browser (select mail, run the pipeline)."""

    _DEFAULT_HISTORY_BATCH_SIZE = 25

    app = getters.app(cast("type[App[Any]]", App))

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._selected: int | None = None
        self._history: list[MailMessage] = []
        self._picked: set[str] = set()
        self._known: set[str] = set()
        self._history_account = ""
        self._analysis_worker: Any = None
        self._analysis_running = False
        self._history_load_worker: Any = None
        self._history_loading = False
        self._history_loaded = False
        self._history_has_more = True
        self._history_offset = 0
        self._history_load_generation = 0

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Static(self._t("tui.accounts_title"), id="accounts-title")
        yield Static(self._t("tui.accounts_help"), id="accounts-help")
        yield DataTable(id="accounts-table")
        with Horizontal(id="accounts-actions"):
            yield Button(self._t("tui.btn_new"), id="account-add", variant="success")
            yield Button(
                self._t("tui.btn_edit"), id="account-edit", variant="primary", disabled=True
            )
            yield Button(
                self._t("tui.btn_delete"), id="account-delete", variant="error", disabled=True
            )
            yield Button(
                self._t("tui.history_load"), id="account-history", variant="primary", disabled=True
            )
            yield Button(self._t("ux.sync_mail"), id="account-sync", disabled=True)
        yield Static(self._t("tui.history_title"), id="history-title")
        yield Static(self._t("tui.history_select_hint"), id="history-help")
        yield SelectionTable(t=self._t, id="history-table")
        with Horizontal(id="history-actions"):
            yield Button(
                self._t("tui.history_analyze"),
                id="history-analyze",
                variant="success",
                disabled=True,
            )
            yield Button(
                self._t("tui.history_select_all"),
                id="history-select-all",
                variant="primary",
                disabled=True,
            )
            yield Button(
                self._t("tui.history_more"), id="history-more", variant="primary", disabled=True
            )
            yield Static(self._t("tui.history_count_label"), id="history-count-label")
            yield Input(
                value=str(self._DEFAULT_HISTORY_BATCH_SIZE), type="integer", id="history-count"
            )
        yield Static("", id="accounts-status")

    async def on_mount(self) -> None:
        await self.reload()

    async def relabel(self) -> None:
        self._columns_done = False
        self._history_columns_done = False
        relabel_buttons(
            self,
            {
                "account-add": "tui.btn_new",
                "account-edit": "tui.btn_edit",
                "account-delete": "tui.btn_delete",
                "account-history": "tui.history_load",
                "account-sync": "ux.sync_mail",
                "history-analyze": "tui.history_analyze",
                "history-select-all": "tui.history_select_all",
                "history-more": "tui.history_more",
            },
        )
        for ident, key in {
            "accounts-title": "tui.accounts_title",
            "accounts-help": "tui.accounts_help",
            "history-title": "tui.history_title",
            "history-help": "tui.history_select_hint",
            "history-count-label": "tui.history_count_label",
        }.items():
            self.query_one(f"#{ident}", Static).update(self._t(key))
        await self.reload()
        if self._analysis_worker is not None and not self._analysis_worker.is_finished:
            self.query_one("#history-analyze", Button).label = self._t("tui.reparse_stop")

    # -- helpers -----------------------------------------------------------

    def _accounts_table(self) -> DataTable[Any] | None:
        return self.query_one_optional("#accounts-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    def _history_table(self) -> DataTable[Any] | None:
        return self.query_one_optional("#history-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    def _set_status(self, text: str) -> None:
        node = self.query_one_optional("#accounts-status", Static)
        if node is not None:
            node.update(text)

    def _selected_account_id(self) -> str:
        accounts = self._service.config.accounts
        if self._selected is None or not 0 <= self._selected < len(accounts):
            return ""
        return accounts[self._selected].account_id

    def _sync_controls(self) -> None:
        account_id = self._selected_account_id()
        busy = self._history_loading or self._analysis_running
        count = self.query_one_optional("#history-count", Input)
        raw = count.value.strip() if count is not None else ""
        valid_count = raw.isdecimal() and int(raw) > 0
        can_load = valid_count and account_id in self._service.history_accounts()
        enabled = {
            "account-edit": bool(account_id) and not busy,
            "account-delete": bool(account_id) and not busy,
            "account-history": can_load and not busy,
            "account-sync": bool(account_id) and not busy,
            "history-more": bool(account_id)
            and valid_count
            and account_id == self._history_account
            and self._history_loaded
            and self._history_has_more
            and not busy,
            "history-select-all": bool(self._history) and not busy,
            "history-analyze": self._analysis_running or (bool(self._picked) and not busy),
        }
        for ident, available in enabled.items():
            button = self.query_one_optional(f"#{ident}", Button)
            if button is not None:
                button.disabled = not available
        table = self._accounts_table()
        if table is not None:
            table.disabled = self._analysis_running
        if count is not None:
            count.disabled = self._history_loading

    def _select_account(self, index: int | None) -> None:
        self._selected = index
        if self._history_account and self._history_account != self._selected_account_id():
            self._history_load_generation += 1
            if self._history_load_worker is not None:
                self._history_load_worker.cancel()
            self._history_account = ""
            self._history.clear()
            self._picked.clear()
            self._known.clear()
            self._history_loading = False
            self._history_loaded = False
            self._history_has_more = True
            self._history_offset = 0
            self._render_history()
        self._sync_controls()

    def _start_history_load(self, offset: int) -> None:
        self._history_load_generation += 1
        self._history_loading = True
        self._sync_controls()
        self._history_load_worker = self.run_worker(
            self._load_history(offset=offset, generation=self._history_load_generation),
            exclusive=True,
            group="history-load",
            exit_on_error=False,
        )

    def _ensure_columns(self) -> None:
        table = self._accounts_table()
        if table is None or getattr(self, "_columns_done", False):
            return
        for key in ("account", "email", "provider", "status"):
            with contextlib.suppress(Exception):
                table.remove_column(key)
        table.add_column(self._t("account.header_id"), key="account")
        table.add_column(self._t("account.header_provider"), key="provider")
        table.add_column(self._t("account.header_email"), key="email")
        table.add_column(self._t("tui.accounts_column_status"), key="status")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        self._columns_done = True

    def _ensure_history_columns(self) -> None:
        table = self._history_table()
        if table is None or getattr(self, "_history_columns_done", False):
            return
        for key in ("selected", "subject", "sender", "date", "state"):
            with contextlib.suppress(Exception):
                table.remove_column(key)
        table.add_column("[ ]", key="selected", width=3)
        table.add_column(self._t("tui.column_subject"), key="subject")
        table.add_column(self._t("tui.column_sender"), key="sender")
        table.add_column(self._t("tui.column_date"), key="date")
        table.add_column(self._t("tui.accounts_column_status"), key="state")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        self._history_columns_done = True

    async def reload(self) -> None:
        table = self._accounts_table()
        if table is None:
            return
        view = TableView.capture(table, str(self._selected) if self._selected is not None else None)
        table.clear()
        self._ensure_columns()
        accounts = self._service.config.accounts
        for position, account in enumerate(accounts):
            table.add_row(
                escape(account.account_id),
                escape(account.provider),
                escape(account.email or "-"),
                self._t("sync.state_" + self._service.runtime.account_status(account.account_id)),
                key=str(position),
            )
        if not accounts:
            self._set_status(self._t("tui.accounts_empty"))
            self._selected = None
        else:
            selected = view.restore(table)
            self._selected = int(selected) if selected is not None else None
        self._select_account(self._selected)
        self._render_history()

    def _render_history(self) -> None:
        table = self._history_table()
        if table is None:
            return
        view = TableView.capture(table)
        table.clear()
        self._ensure_history_columns()
        if isinstance(table, SelectionTable):
            table.set_checks(set(self._picked))
        # The IMAP page arrives in UID order, which is NOT the Date-header
        # order (moved/late-delivered mail breaks it); the user reads this
        # table as a timeline, so sort by the displayed date instead.
        for mail in sorted(self._history, key=lambda item: item.date, reverse=True):
            record_id = mail.normalized_message_id()
            try:
                subject = escape(mail.subject or self._t("tui.mail_no_subject"))
                sender = escape(mail.sender.address)
                date_text = _local_time(self._service, mail.date)
            except Exception:
                # one poison mail must never take the app down
                subject, sender, date_text = self._t("tui.history_parse_error"), "-", "-"
            state = (
                self._t("tui.history_marked_known")
                if record_id in self._known
                else self._t("tui.history_marked_new")
            )
            table.add_row(
                table.check_label(record_id) if isinstance(table, SelectionTable) else "",
                subject,
                sender,
                date_text,
                state,
                key=record_id,
            )
        view.restore(table)
        self._sync_controls()

    # -- events ------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "history-count":
            self._sync_controls()

    def on_selection_table_selection_changed(self, event: SelectionTable.SelectionChanged) -> None:
        if event.table.id == "history-table":
            self._picked = set(event.table.checked_keys)
            self._sync_controls()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if _table_id(event) == "accounts-table" and event.row_key.value is not None:
            index = int(str(event.row_key.value))
            self._select_account(index)
            if _double_clicked("accounts", str(event.row_key.value)) and index < len(
                self._service.config.accounts
            ):
                self._open_form(index)
            return
        if _table_id(event) == "history-table":
            table = self._history_table()
            if isinstance(table, SelectionTable):
                table.action_toggle_check()

    async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if _table_id(event) == "accounts-table" and event.row_key.value is not None:
            table = self._accounts_table()
            if table is None or event.cursor_row != table.cursor_row:
                return
            self._select_account(int(str(event.row_key.value)))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.disabled:
            return
        button_id = event.button.id or ""
        if button_id == "account-add":
            self._open_form(None)
            return
        if button_id == "history-select-all":
            await self._select_all_history()
            return
        if button_id == "history-analyze":
            if self._analysis_worker is not None and not self._analysis_worker.is_finished:
                self._analysis_worker.cancel()
                event.button.disabled = True
                self._set_status(self._t("tui.reparse_stopping"))
            else:
                self._analysis_running = True
                self._sync_controls()
                self._analysis_worker = self.run_worker(
                    self._analyze_selected(), group="history-analyze", exit_on_error=False
                )
            return
        if button_id == "history-more":
            self._start_history_load(self._history_offset)
            return
        index = self._selected
        if index is None or index >= len(self._service.config.accounts):
            self._set_status(f"[yellow]{self._t('tui.entry_pick_first')}[/yellow]")
            return
        if button_id == "account-edit":
            self._open_form(index)
        elif button_id == "account-sync":
            from mailflow_tui.mail_workflows import SyncScreen

            self.app.push_screen(SyncScreen(self._service, self._selected_account_id()))
        elif button_id == "account-delete":
            await self._mutate(
                self._service.remove_config_entry("accounts", index), "entry_removed"
            )
            self._select_account(None)
        elif button_id == "account-history":
            self._history = []
            self._picked.clear()
            self._known.clear()
            self._history_account = self._service.config.accounts[index].account_id
            self._history_loaded = False
            self._history_has_more = True
            self._history_offset = 0
            self._render_history()
            self._start_history_load(0)

    def _open_form(self, index: int | None) -> None:
        values = self._service.config.accounts[index].model_dump() if index is not None else None

        async def save(result: dict[str, Any]) -> None:
            warning = ""
            try:
                if index is None:
                    await self._service.add_config_entry("accounts", result)
                else:
                    await self._service.update_config_entry("accounts", index, result)
            except ConfigReloadError as exc:
                warning = error_detail(self._service, exc)
            await self.reload()
            message = warning or self._t(
                "tui.entry_added" if index is None else "tui.entry_updated",
                group=self._t("tui.accounts_title"),
            )
            self._set_status(escape(message))

        def after(result: dict[str, Any] | None) -> None:
            if result is None:
                self._set_status(self._t("tui.form_cancelled"))

        app = self.app
        app.push_screen(
            EntryFormScreen(self._service, "accounts", values=values, on_save=save),
            callback=after,
        )  # pyright: ignore[reportUnknownMemberType]

    async def _add(self, values: dict[str, Any]) -> None:
        await self._mutate(self._service.add_config_entry("accounts", values), "entry_added")

    async def _update(self, index: int, values: dict[str, Any]) -> None:
        await self._mutate(
            self._service.update_config_entry("accounts", index, values), "entry_updated"
        )

    async def _mutate(self, action: Any, message_key: str) -> None:
        try:
            await action
        except SettingsError as exc:
            message = self._t(
                "tui.settings_invalid", option=exc.option, reason=error_detail(self._service, exc)
            )
            self._set_status(f"[red]{escape(message)}[/red]")
            self.notify(message, severity="error", timeout=8)
            return
        except Exception as exc:
            message = error_message(self._service, exc)
            self._set_status(f"[red]{escape(message)}[/red]")
            self.notify(message, severity="error", timeout=8)
            return
        if message_key:
            self._set_status(
                f"[green]{self._t(f'tui.{message_key}', group=self._t('tui.accounts_title'))}[/green]"
            )
        await self.reload()

    # -- history -----------------------------------------------------------

    def _history_limit(self) -> int | None:
        node = self.query_one_optional("#history-count", Input)
        raw = str(node.value).strip() if node is not None else str(self._DEFAULT_HISTORY_BATCH_SIZE)
        try:
            count = int(raw)
        except ValueError:
            count = 0
        if count <= 0:
            self._set_status(f"[yellow]{self._t('tui.history_count_invalid')}[/yellow]")
            return None
        return count

    async def _load_history(self, *, offset: int, generation: int | None = None) -> None:
        generation = self._history_load_generation if generation is None else generation
        try:
            await self._load_history_page(offset=offset)
        finally:
            if generation == self._history_load_generation:
                self._history_loading = False
                self._sync_controls()

    async def _load_history_page(self, *, offset: int) -> None:
        account_id = self._history_account
        if not account_id:
            self._set_status(f"[yellow]{self._t('tui.entry_pick_first')}[/yellow]")
            return
        limit = self._history_limit()
        if limit is None:
            return
        self._set_status(self._t("tui.history_loading", account=account_id))
        try:
            page = await self._service.fetch_history(account_id, limit=limit, offset=offset)
        except NotImplementedError:
            self._set_status(f"[yellow]{self._t('tui.history_unsupported')}[/yellow]")
            return
        except KeyError as exc:
            self._set_status(f"[red]{escape(error_message(self._service, exc))}[/red]")
            return
        except Exception as exc:
            self._set_status(f"[red]{escape(error_message(self._service, exc))}[/red]")
            return
        if account_id != self._history_account:
            return
        known: set[str] = set(self._known)
        fresh: list[MailMessage] = []
        seen_ids = {m.normalized_message_id() for m in self._history}
        for mail in page:
            record_id = mail.normalized_message_id()
            if record_id in seen_ids:
                continue
            seen_ids.add(record_id)
            fresh.append(mail)
            if await self._service.is_mail_known(mail):
                known.add(record_id)
        if account_id != self._history_account:
            return
        self._history_offset = offset + len(page)
        self._history_loaded = True
        self._history_has_more = len(page) >= limit
        self._known = known
        self._history = [*self._history, *fresh] if offset else list(fresh)
        self._render_history()
        if not self._history:
            self._set_status(self._t("tui.history_empty"))
        else:
            self._set_status(
                self._t(
                    "tui.history_batch_loaded",
                    requested=limit,
                    added=len(fresh),
                    count=len(self._history),
                )
            )

    def _displayed_history_ids(self) -> list[str]:
        table = self._history_table()
        if table is None:
            return []
        return [str(k.value) for k in table.rows]

    async def _select_all_history(self) -> None:
        for record_id in self._displayed_history_ids():
            self._picked.add(record_id)
        self._render_history()
        self._set_status(
            f"[green]{self._t('tui.history_all_selected', count=len(self._picked))}[/green]"
        )

    async def _analyze_selected(self) -> None:
        chosen = [mail for mail in self._history if mail.normalized_message_id() in self._picked]
        if not chosen:
            self._analysis_running = False
            self._sync_controls()
            self._set_status(f"[yellow]{self._t('tui.history_none_selected')}[/yellow]")
            return
        button = self.query_one("#history-analyze", Button)
        self._analysis_running = True
        self._sync_controls()
        button.label = self._t("tui.reparse_stop")
        button.variant = "error"
        processed = 0
        failed: list[str] = []
        cancelled = False
        refresh = getattr(self.app, "schedule_reload", lambda: None)
        try:
            for position, mail in enumerate(chosen, start=1):
                subject = escape(mail.subject[:36])
                self._set_status(
                    self._t("tui.history_progress", position=position, total=len(chosen))
                    + " "
                    + subject
                )
                try:
                    record = await self._service.process_mail(mail, force=True)
                    if record is None or record.analysis_state == "failed":
                        failed.append(mail.subject[:40])
                        continue
                except Exception as exc:
                    failed.append(f"{mail.subject[:40]}: {error_detail(self._service, exc)}")
                    continue
                message_id = mail.normalized_message_id()
                self._known.add(message_id)
                self._picked.discard(message_id)
                processed += 1
                self._render_history()
                # Refresh the actual mail view even while the history batch
                # remains open. Completed results are usable immediately.
                refresh()
        except asyncio.CancelledError:
            cancelled = True
        finally:
            self._analysis_running = False
            button.label = self._t("tui.history_analyze")
            button.variant = "success"
            self._render_history()
            refresh()
        if cancelled:
            self._set_status(self._t("tui.reparse_stopped", count=processed, total=len(chosen)))
        elif failed:
            self._set_status(
                self._t("tui.history_failed", count=len(failed))
                + ": "
                + escape("; ".join(failed[:3]))
            )
        else:
            self._set_status(self._t("tui.history_reanalyzed", count=processed))


__all__ = [
    "AccountsPane",
    "EntryFormScreen",
    "LLMPane",
    "ListEditScreen",
    "OptionCard",
    "SettingsPane",
]
