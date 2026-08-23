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
from datetime import datetime
from typing import Any, ClassVar

from mailflow.config import LLMConfig
from mailflow.domain import ComponentKind, MailMessage
from mailflow.service import MailFlowService
from mailflow.settings import (
    EditorKind,
    OptionSpec,
    SettingsError,
    SettingsSection,
    coerce_value,
    entry_field_specs,
    entry_model,
)
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
    Switch,
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


def default_text(spec: OptionSpec) -> str:
    """Human-readable rendering of an option's schema default."""
    default: Any = spec.default
    if isinstance(default, bool):
        return "true" if default else "false"
    if default is None or default == "" or default == [] or default == {}:
        return "-"
    if isinstance(default, list):
        # pyright: ignore[reportUnknownVariableType] — element types are
        # irrelevant; only the entry count is rendered
        return escape(f"{len(default)} entries")  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(default, dict):
        return escape(f"{len(default)} keys")  # pyright: ignore[reportUnknownArgumentType]
    text = str(default)
    # model defaults repr as ClassName(...): summarize instead of dumping,
    # the brackets would otherwise be parsed as markup and crash rendering
    if text.endswith(")") and "(" in text and not text.startswith(("'", '"')):
        name = text.split("(", 1)[0]
        return f"{name}(…)" if name[:1].isupper() else escape(text)
    return escape(text)


class ListEditScreen(ModalScreen[str | None]):
    """Edit a list or mapping value as text: one entry per line."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss_modal", "Back")]

    def __init__(self, service: MailFlowService, spec: OptionSpec, description: str = "") -> None:
        super().__init__()
        self._service = service
        self._spec = spec
        self._description = description

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
            with Horizontal(classes="dialog-actions"):
                yield Button(self._t("tui.btn_save"), id="list-edit-save", variant="success")
                yield Button(self._t("tui.btn_back"), id="list-edit-back", variant="primary")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "list-edit-save":
            self.dismiss(self.query_one("#list-edit-text", TextArea).text)
        elif event.button.id == "list-edit-back":
            self.dismiss(None)


class _Extra:
    """One provider-specific form field: id, label key suffix, widget kind,
    default and whether it lands in ``options`` (vs a top-level column)."""

    __slots__ = (
        "default",
        "field_id",
        "into_options",
        "kind",
        "label",
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
        required: bool = False,
    ) -> None:
        self.field_id = field_id
        self.label = field_id.replace("_", " ")
        self.kind = kind  # text | password | choice
        self.default = default
        self.into_options = into_options
        self.secret = secret
        self.required = required


_ADVANCED = ("headers", "query", "extra_body")

_LLM_PROVIDER_FIELDS: dict[str, tuple[_Extra, ...]] = {
    "openai-completions": (
        _Extra("base_url", default="https://api.openai.com/v1", into_options=False, required=True),
        _Extra("api_key", kind="password", into_options=False, secret=True, required=True),
    ),
    "openai-responses": (
        _Extra("base_url", default="https://api.openai.com/v1", into_options=False, required=True),
        _Extra("api_key", kind="password", into_options=False, secret=True, required=True),
    ),
    "openai-codex-responses": (
        _Extra(
            "base_url",
            default="https://chatgpt.com/backend-api",
            into_options=False,
            required=True,
        ),
        _Extra("api_key", kind="password", into_options=False, secret=True, required=True),
    ),
    "azure-openai-responses": (
        _Extra(
            "base_url",
            default="https://YOUR-RESOURCE.openai.azure.com",
            into_options=False,
            required=True,
        ),
        _Extra("api_key", kind="password", into_options=False, secret=True, required=True),
        _Extra("api_version", default="preview"),
    ),
    "anthropic-messages": (
        _Extra("base_url", default="https://api.anthropic.com", into_options=False, required=True),
        _Extra("api_key", kind="password", into_options=False, secret=True, required=True),
    ),
    "google-generative-ai": (
        _Extra(
            "base_url",
            default="https://generativelanguage.googleapis.com",
            into_options=False,
            required=True,
        ),
        _Extra("api_key", kind="password", into_options=False, secret=True, required=True),
    ),
    "google-vertex": (
        _Extra("project", required=True),
        _Extra("location", default="us-central1"),
        _Extra("service_account_file"),
        _Extra("api_key", kind="password", into_options=False, secret=True),
    ),
}
# the legacy alias behaves like plain completions
_LLM_PROVIDER_FIELDS["openai-compatible"] = _LLM_PROVIDER_FIELDS["openai-completions"]

_MINIMAL_CORE_FIELDS: dict[str, frozenset[str]] = {
    # everything else (timeouts, retries, fallback chains, raw mappings) is
    # editable in config.toml or via `config set`; forms stay minimal
    "llms": frozenset({"llm_id", "model", "provider"}),
    "accounts": frozenset({"account_id", "provider", "email", "enabled"}),
}

_IMAP_PRESET_HOSTS: dict[str, tuple[str, int, bool]] = {
    "qq": ("imap.qq.com", 993, True),
    "163": ("imap.163.com", 993, True),
    "outlook": ("outlook.office365.com", 993, True),
    "gmail": ("imap.gmail.com", 993, True),
}


class EntryFormScreen(ModalScreen[dict[str, Any] | None]):
    """Provider-aware form for one structured entry (mailbox, LLM, ...).

    The ``provider`` dropdown re-renders the provider-specific section, so
    users never hand-write TOML mappings; optional mapping fields stay
    available under an "advanced" separator.
    """

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss_modal", "Back")]

    def __init__(
        self,
        service: MailFlowService,
        group: str,
        *,
        values: dict[str, Any] | None = None,
        hidden: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self._service = service
        self._group = group
        self._values = dict(values or {})
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
        else:
            self._provider_choices = ()

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def _describe(self, spec: OptionSpec) -> str:
        key = f"config.desc.{self._group}[].{spec.label}"
        translated = self._service.t(key)
        return spec.description if translated == key else translated

    # -- helpers -------------------------------------------------------------

    def _current_provider(self) -> str:
        if self._group not in ("llms", "accounts"):
            return ""
        try:
            selected = _select_text(self.query_one("#field-provider", Select))
        except Exception:
            selected = ""
        return selected or str(self._values.get("provider", "")) or self._default_provider

    def _extras_for(self, provider: str) -> tuple[_Extra, ...]:
        if self._group == "llms":
            return _LLM_PROVIDER_FIELDS.get(provider, ())
        if self._group == "accounts" and provider == "imap":
            return (
                _Extra("preset", default="qq"),
                _Extra("username", required=True),
                _Extra("password", kind="password", secret=True, required=True),
                _Extra("imap_folder", default="INBOX"),
                _Extra("interval_seconds", default="300"),
                _Extra("limit", default="20"),
            )
        return ()

    def _core_value(self, label: str) -> Any:
        spec = next((s for s in self._specs if s.label == label), None)
        value = self._values.get(label, spec.default if spec else None)
        return "" if value is None else value

    def _extra_value(self, extra: _Extra) -> str:
        pool: dict[str, Any] = dict(self._values.get("options") or {})
        fallback = self._values.get(extra.field_id)
        raw: Any = fallback if not extra.into_options else pool.get(extra.field_id, fallback)
        if raw is None or raw == "":
            return extra.default
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
                if self._group == "llms":
                    yield Button(self._t("tui.btn_test"), id="entry-form-test", variant="primary")
                yield Button(self._t("tui.btn_save"), id="entry-form-save", variant="success")
                yield Button(self._t("tui.btn_back"), id="entry-form-back", variant="primary")

    def _field_widgets(self, spec: OptionSpec) -> ComposeResult:
        widget_id = f"field-{_slug(spec.label)}"
        current = self._values.get(spec.label, spec.default)
        choices = spec.choices
        if spec.label == "provider" and self._provider_choices:
            choices = self._provider_choices
        yield Label(f"{spec.label}{' *' if spec.required else ''}", classes="field-label")
        description = self._describe(spec)
        if description:
            yield Static(escape(description), classes="field-desc")
        if spec.editor is EditorKind.BOOLEAN:
            yield Switch(value=bool(current), id=widget_id)
        elif spec.editor is EditorKind.CHOICE or (spec.label == "provider" and choices):
            initial = str(current) if str(current) in [str(c) for c in choices] else None
            if spec.label == "provider" and not self._editing:
                # a brand-new entry defaults to the most common transport
                initial = self._default_provider or initial
            # literal None is an illegal Select value: NULL makes Textual pick
            # the first option when blank is not allowed, instead of crashing
            initial_value: Any = initial if initial is not None else Select.NULL
            yield Select(
                [(str(choice), str(choice)) for choice in choices],
                value=initial_value,
                id=widget_id,
                allow_blank=False,
            )
        elif spec.editor in _MULTILINE_EDITORS:
            yield TextArea(
                as_editor_text(spec.editor, current), id=widget_id, classes="field-multiline"
            )
        elif spec.secret:
            with Horizontal(classes="secret-row"):
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
            yield Label(extra.label + marker, classes="field-label")
            desc_key = f"tui.extras_{_slug(extra.field_id).replace('-', '_')}"
            translated_desc = self._t(desc_key)
            if translated_desc != desc_key:
                yield Static(escape(translated_desc), classes="field-desc")
            if extra.kind == "choice":
                yield Select(
                    [(name, name) for name in _IMAP_PRESET_HOSTS],
                    value=self._extra_value(extra) or "qq",
                    id=widget_id,
                    allow_blank=False,
                )
            elif extra.secret:
                # mounted via mount_all (outside compose), so children are
                # attached explicitly instead of with-block composition
                row = Horizontal(classes="secret-row")
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
        container.mount_all(list(self._render_extras()))

    # -- collection ---------------------------------------------------------------

    @staticmethod
    def _split_mapping(text: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            name, _, value = line.partition("=")
            out[name.strip()] = value.strip()
        return out

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
                values[spec.label] = coerce_value(spec, text)
        return values

    def _collect_extras(self) -> dict[str, Any]:
        provider = self._current_provider()
        extras = self._extras_for(provider)
        collected: dict[str, Any] = {"options": dict(self._values.get("options") or {})}
        for extra in extras:
            node: Any | None = self.query_one_optional(f"#extra-{_slug(extra.field_id)}")
            raw = ""
            if isinstance(node, Select):
                raw = _select_text(node)
            elif isinstance(node, TextArea):
                raw = node.text
            elif isinstance(node, Input):
                raw = node.value
            if extra.field_id == "preset":
                host, port, ssl_flag = _IMAP_PRESET_HOSTS.get(raw or "qq", ("", 993, True))
                collected["options"]["imap_host"] = host
                collected["options"]["imap_port"] = port
                collected["options"]["imap_ssl"] = ssl_flag
                continue
            if isinstance(node, TextArea):  # headers / query / extra_body
                parsed = self._split_mapping(raw)
                if parsed:
                    collected[extra.field_id] = parsed
                continue
            text = raw.strip()
            if not text:
                if extra.required:
                    raise SettingsError(extra.field_id, f"{extra.label} is required")
                continue
            target = collected["options"] if extra.into_options else collected
            try:
                target[extra.field_id] = (
                    int(text) if extra.field_id in ("interval_seconds", "limit") else text
                )
            except ValueError as exc:
                raise SettingsError(extra.field_id, f"{extra.label} must be a number") from exc
        if (
            provider == "google-vertex"
            and not collected["options"].get("service_account_file")
            and not collected.get("api_key")
        ):
            raise SettingsError(
                "credential",
                "google-vertex needs api_key (access token) or service_account_file",
            )
        if not collected["options"]:
            collected.pop("options")
        return collected

    def _collect(self) -> dict[str, Any]:
        values = self._collect_core()
        values.update(self._collect_extras())
        return values

    # -- llm connectivity test ---------------------------------------------------

    async def _test_llm(self) -> None:
        status = self.query_one("#entry-form-status", Static)
        try:
            values = self._collect()
        except SettingsError as exc:
            status.update(f"[red]{escape(exc.message)}[/red]")
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
            timeout_seconds=20.0,
            max_retries=0,
        )
        provider = config.provider
        from mailflow.contracts import LLMBackend

        backend: LLMBackend
        try:
            factory = self._service.registry.llm_factory(provider)
            backend = factory(config)
        except Exception as exc:
            status.update(f"[red]{escape(str(exc))}[/red]")
            return
        started = datetime.now()
        status.update(self._t("tui.llm_testing"))
        try:
            completion = await asyncio.wait_for(
                backend.chat([{"role": "user", "content": "ping"}], temperature=0.0),
                timeout=25.0,
            )
        except TimeoutError:
            status.update(f"[red]{self._t('tui.llm_test_timeout')}[/red]")
            return
        except Exception as exc:
            message = str(exc)
            if config.api_key and config.api_key in message:
                message = message.replace(config.api_key, "***")
            status.update(f"[red]{escape(message[:300])}[/red]")
            return
        elapsed = (datetime.now() - started).total_seconds()
        preview = escape(completion.text[:80])
        status.update(
            f"[green]{self._t('tui.llm_test_ok', seconds=f'{elapsed:.1f}', reply=preview)}[/green]"
        )

    # -- events -----------------------------------------------------------------

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "field-provider":
            await self._rebuild_extras()
        elif event.select.id == "extra-preset":
            preset = _IMAP_PRESET_HOSTS.get(_select_text(event.select))
            if preset:
                host, port, ssl_flag = preset
                for field_id, value in (
                    ("imap_host", host),
                    ("imap_port", str(port)),
                    ("imap_ssl", "true" if ssl_flag else "false"),
                ):
                    node = self.query_one_optional(f"#extra-{_slug(field_id)}")
                    if isinstance(node, Input):
                        node.value = value

    def action_dismiss_modal(self) -> None:
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
            self.dismiss(None)
            return
        if button_id == "entry-form-test":
            await self._test_llm()
            return
        if button_id != "entry-form-save":
            return
        try:
            values = self._collect()
        except SettingsError as exc:
            self.query_one("#entry-form-status", Static).update(f"[red]{escape(exc.message)}[/red]")
            return
        self.dismiss({**self._values, **values})


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
            self._t("tui.settings_default_hint", value=default_text(spec)),
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
            yield Select(
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
        self._render_lock = asyncio.Lock()
        self._reload_lock = asyncio.Lock()

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Input(placeholder=self._t("tui.settings_search_placeholder"), id="settings-search")
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
        await self.reload()

    async def relabel(self) -> None:
        await self.reload()

    # -- language ----------------------------------------------------------

    async def refresh_languages(self) -> None:
        select = self.query_one_optional("#language-select", Select)  # pyright: ignore[reportUnknownVariableType]
        if select is None:
            return
        select.set_options(  # pyright: ignore[reportUnknownMemberType]
            [
                (f"{info.name} ({info.code})", info.code)
                for info in self._service.i18n.available_languages()
            ]
        )
        if select.value != self._service.i18n.language:  # pyright: ignore[reportUnknownMemberType]
            select.value = self._service.i18n.language

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

    def _visible_options(self) -> list[OptionSpec]:
        query = self._query.strip().lower()
        if query:
            return [
                spec
                for section in self._sections
                for spec in section.options
                if query in f"{spec.key} {self._describe(spec)}".lower()
            ]
        section = next((s for s in self._sections if s.section_id == self._active), None)
        return list(section.options) if section is not None else []

    async def _render_options(self) -> None:
        """Replace the option cards; serialized so overlapping renders (a
        search keystroke landing during a reload) cannot mount duplicate ids."""
        async with self._render_lock:
            container = self.query_one_optional("#settings-options", ScrollableContainer)
            if container is None:
                return
            await container.remove_children()
            options = self._visible_options()
            if not options:
                await container.mount(
                    Static(self._t("tui.settings_no_matches"), classes="settings-empty")
                )
                return
            await container.mount_all(
                [OptionCard(self._service, spec, self._describe(spec)) for spec in options]
            )

    def _set_status(self, text: str) -> None:
        node = self.query_one_optional("#settings-status", Static)
        if node is not None:
            node.update(text)

    # -- events ------------------------------------------------------------

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "settings-search":
            self._query = event.value
            await self._render_options()

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
        button_id = event.button.id or ""
        card = self._card_of(event.button)
        if card is None:
            return
        event.stop()
        if button_id.startswith("list-"):
            await self._edit_list(card)
        elif button_id.startswith("save-"):
            await self._save(card.spec.key, card.entered_value())
        elif button_id.startswith("reset-"):
            await self._reset(card.spec.key)

    async def _edit_list(self, card: OptionCard) -> None:
        def after(result: str | None) -> None:
            if result is not None:
                self.run_worker(self._save(card.spec.key, result))

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ListEditScreen(self._service, card.spec, self._describe(card.spec)),
            callback=after,
        )

    async def _save(self, key: str, value: Any) -> None:
        try:
            await self._service.set_setting(key, value)
        except SettingsError as exc:
            message = self._t("tui.settings_invalid", option=exc.option, reason=exc.message)
            self._set_status(f"[red]{escape(message)}[/red]")
            self.notify(message, severity="error", timeout=8)
            return
        except ValueError as exc:
            self._set_status(f"[red]{escape(str(exc))}[/red]")
            self.notify(str(exc), severity="error", timeout=8)
            return
        self._set_status(f"[green]{self._t('tui.settings_saved', option=key)}[/green]")
        await self.reload()

    async def _reset(self, key: str) -> None:
        try:
            await self._service.reset_setting(key)
        except SettingsError as exc:
            message = self._t("tui.settings_invalid", option=exc.option, reason=exc.message)
            self._set_status(f"[red]{escape(message)}[/red]")
            return
        except ValueError as exc:
            self._set_status(f"[red]{escape(str(exc))}[/red]")
            return
        self._set_status(f"[green]{self._t('tui.settings_reset_done', option=key)}[/green]")
        await self.reload()


class LLMPane(Vertical):
    """The ordered LLM chain: first entry is default, the rest are fallbacks."""

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._selected: int | None = None

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Static(self._t("tui.llms_title"), id="llms-title")
        yield Static(self._t("tui.llms_help"), id="llms-help")
        yield DataTable(id="llms-table")
        with Horizontal(id="llms-actions"):
            yield Button(self._t("tui.btn_new"), id="llm-add", variant="success")
            yield Button(self._t("tui.btn_edit"), id="llm-edit", variant="primary")
            yield Button(self._t("tui.btn_delete"), id="llm-delete", variant="error")
            yield Button(self._t("tui.btn_move_up"), id="llm-up", variant="primary")
            yield Button(self._t("tui.btn_move_down"), id="llm-down", variant="primary")
        yield Static("", id="llms-status")

    async def on_mount(self) -> None:
        await self.reload()

    async def relabel(self) -> None:
        self._columns_done = False
        await self.reload()

    def _table(self) -> DataTable[Any] | None:
        return self.query_one_optional("#llms-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

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
        for key in ("order", "llm", "model", "provider", "role"):
            with contextlib.suppress(Exception):
                table.remove_column(key)
        table.add_column(self._t("tui.llms_column_order"), key="order")
        table.add_column(self._t("plugin.header_id"), key="llm")
        table.add_column(self._t("llm.header_model"), key="model")
        table.add_column(self._t("llm.header_backend"), key="provider")
        table.add_column(self._t("tui.llms_column_role"), key="role")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        self._columns_done = True

    async def reload(self) -> None:
        table = self._table()
        if table is None:
            return
        table.clear()
        self._ensure_columns()
        llms = self._service.config.llms
        for position, llm in enumerate(llms):
            role = (
                self._t("tui.llms_role_default")
                if position == 0
                else self._t("tui.llms_role_fallback", position=position)
            )
            table.add_row(
                str(position + 1),
                escape(llm.llm_id),
                escape(llm.model),
                escape(llm.provider),
                role,
                key=str(position),
            )
        if not llms:
            self._set_status(self._t("tui.llms_empty"))
            self._selected = None

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if _table_id(event) == "llms-table":
            self._selected = int(str(event.row_key.value))

    async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if _table_id(event) == "llms-table" and event.row_key.value is not None:
            self._selected = int(str(event.row_key.value))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
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
            self._selected = None
            return
        offset = -1 if button_id == "llm-up" else 1
        await self._mutate(self._service.move_config_entry("llms", index, offset), "")
        self._selected = max(0, min(index + offset, len(self._service.config.llms) - 1))

    def _open_form(self, index: int | None) -> None:
        values = (
            self._service.config.llms[index].model_dump(exclude={"default", "fallback"})
            if index is not None
            else None
        )

        def after(result: dict[str, Any] | None) -> None:
            if result is None:
                return
            if index is None:
                self.run_worker(self._add(result))
            else:
                self.run_worker(self._update(index, result))

        # default/fallback are derived from the list order, never typed in
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            EntryFormScreen(self._service, "llms", values=values, hidden=("default", "fallback")),
            callback=after,
        )

    async def _add(self, values: dict[str, Any]) -> None:
        await self._mutate(self._service.add_config_entry("llms", values), "entry_added")

    async def _update(self, index: int, values: dict[str, Any]) -> None:
        await self._mutate(
            self._service.update_config_entry("llms", index, values), "entry_updated"
        )

    async def _mutate(self, action: Any, message_key: str) -> None:
        try:
            await action
        except SettingsError as exc:
            message = self._t("tui.settings_invalid", option=exc.option, reason=exc.message)
            self._set_status(f"[red]{escape(message)}[/red]")
            self.notify(message, severity="error", timeout=8)
            return
        except ValueError as exc:
            self._set_status(f"[red]{escape(str(exc))}[/red]")
            self.notify(str(exc), severity="error", timeout=8)
            return
        if message_key:
            self._set_status(
                f"[green]{self._t(f'tui.{message_key}', group=self._t('tui.tab_llms'))}[/green]"
            )
        await self.reload()


class AccountsPane(Vertical):
    """Mailboxes plus the history browser (select mail, run the pipeline)."""

    _PAGE = 25

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._selected: int | None = None
        self._history: list[MailMessage] = []
        self._picked: set[str] = set()
        self._known: set[str] = set()
        self._history_account = ""

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Static(self._t("tui.accounts_title"), id="accounts-title")
        yield Static(self._t("tui.accounts_help"), id="accounts-help")
        yield DataTable(id="accounts-table")
        with Horizontal(id="accounts-actions"):
            yield Button(self._t("tui.btn_new"), id="account-add", variant="success")
            yield Button(self._t("tui.btn_edit"), id="account-edit", variant="primary")
            yield Button(self._t("tui.btn_delete"), id="account-delete", variant="error")
            yield Button(self._t("tui.history_load"), id="account-history", variant="primary")
        yield Static(self._t("tui.history_title"), id="history-title")
        yield Static(self._t("tui.history_select_hint"), id="history-help")
        yield DataTable(id="history-table")
        with Horizontal(id="history-actions"):
            yield Button(self._t("tui.history_analyze"), id="history-analyze", variant="success")
            yield Button(
                self._t("tui.history_select_all"), id="history-select-all", variant="primary"
            )
            yield Button(self._t("tui.history_more"), id="history-more", variant="primary")
        yield Static("", id="accounts-status")

    async def on_mount(self) -> None:
        await self.reload()

    async def relabel(self) -> None:
        self._columns_done = False
        self._history_columns_done = False
        await self.reload()

    # -- helpers -----------------------------------------------------------

    def _accounts_table(self) -> DataTable[Any] | None:
        return self.query_one_optional("#accounts-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    def _history_table(self) -> DataTable[Any] | None:
        return self.query_one_optional("#history-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    def _set_status(self, text: str) -> None:
        node = self.query_one_optional("#accounts-status", Static)
        if node is not None:
            node.update(text)

    def _ensure_columns(self) -> None:
        table = self._accounts_table()
        if table is None or getattr(self, "_columns_done", False):
            return
        for key in ("account", "email", "provider", "status"):
            with contextlib.suppress(Exception):
                table.remove_column(key)
        table.add_column(self._t("account.header_id"), key="account")
        table.add_column(self._t("account.header_email"), key="email")
        table.add_column(self._t("account.header_provider"), key="provider")
        table.add_column(self._t("tui.accounts_column_status"), key="status")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        self._columns_done = True

    def _ensure_history_columns(self) -> None:
        table = self._history_table()
        if table is None or getattr(self, "_history_columns_done", False):
            return
        for key in ("pick", "subject", "sender", "date", "state"):
            with contextlib.suppress(Exception):
                table.remove_column(key)
        table.add_column(self._t("tui.history_column_selected"), key="pick")
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
        table.clear()
        self._ensure_columns()
        accounts = self._service.config.accounts
        for position, account in enumerate(accounts):
            table.add_row(
                escape(account.account_id),
                escape(account.email or "-"),
                escape(account.provider),
                self._service.runtime.account_status(account.account_id),
                key=str(position),
            )
        if not accounts:
            self._set_status(self._t("tui.accounts_empty"))
            self._selected = None
        self._render_history()

    def _render_history(self) -> None:
        table = self._history_table()
        if table is None:
            return
        table.clear()
        self._ensure_history_columns()
        for mail in self._history:
            record_id = mail.normalized_message_id()
            state = (
                self._t("tui.history_marked_known")
                if record_id in self._known
                else self._t("tui.history_marked_new")
            )
            table.add_row(
                "(x)" if record_id in self._picked else "( )",
                escape(mail.subject or "(no subject)"),
                escape(mail.sender.address),
                mail.date.strftime("%Y-%m-%d %H:%M"),
                state,
                key=record_id,
            )

    # -- events ------------------------------------------------------------

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if _table_id(event) == "accounts-table":
            self._selected = int(str(event.row_key.value))
            return
        if _table_id(event) == "history-table":
            record_id = str(event.row_key.value)
            if record_id in self._picked:
                self._picked.discard(record_id)
            else:
                self._picked.add(record_id)
            self._render_history()

    async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if _table_id(event) == "accounts-table" and event.row_key.value is not None:
            self._selected = int(str(event.row_key.value))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "account-add":
            self._open_form(None)
            return
        if button_id == "history-select-all":
            await self._select_all_history()
            return
        if button_id == "history-analyze":
            await self._analyze_selected()
            return
        if button_id == "history-more":
            await self._load_history(offset=len(self._history))
            return
        index = self._selected
        if index is None or index >= len(self._service.config.accounts):
            self._set_status(f"[yellow]{self._t('tui.entry_pick_first')}[/yellow]")
            return
        if button_id == "account-edit":
            self._open_form(index)
        elif button_id == "account-delete":
            await self._mutate(
                self._service.remove_config_entry("accounts", index), "entry_removed"
            )
            self._selected = None
        elif button_id == "account-history":
            self._history = []
            self._picked.clear()
            self._history_account = self._service.config.accounts[index].account_id
            await self._load_history(offset=0)

    def _open_form(self, index: int | None) -> None:
        values = self._service.config.accounts[index].model_dump() if index is not None else None

        def after(result: dict[str, Any] | None) -> None:
            if result is None:
                return
            if index is None:
                self.run_worker(self._add(result))
            else:
                self.run_worker(self._update(index, result))

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            EntryFormScreen(self._service, "accounts", values=values),
            callback=after,
        )

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
            message = self._t("tui.settings_invalid", option=exc.option, reason=exc.message)
            self._set_status(f"[red]{escape(message)}[/red]")
            self.notify(message, severity="error", timeout=8)
            return
        except ValueError as exc:
            self._set_status(f"[red]{escape(str(exc))}[/red]")
            self.notify(str(exc), severity="error", timeout=8)
            return
        if message_key:
            self._set_status(
                f"[green]{self._t(f'tui.{message_key}', group=self._t('tui.accounts_title'))}[/green]"
            )
        await self.reload()

    # -- history -----------------------------------------------------------

    async def _load_history(self, *, offset: int) -> None:
        account_id = self._history_account
        if not account_id:
            self._set_status(f"[yellow]{self._t('tui.entry_pick_first')}[/yellow]")
            return
        self._set_status(self._t("tui.history_loading", account=account_id))
        try:
            page = await self._service.fetch_history(account_id, limit=self._PAGE, offset=offset)
        except NotImplementedError:
            self._set_status(f"[yellow]{self._t('tui.history_unsupported')}[/yellow]")
            return
        except KeyError as exc:
            self._set_status(f"[red]{escape(str(exc))}[/red]")
            return
        except Exception as exc:  # provider/transport failures stay in the pane
            self._set_status(f"[red]{escape(str(exc))}[/red]")
            return
        known: set[str] = set(self._known)
        for mail in page:
            if await self._service.is_mail_known(mail):
                known.add(mail.normalized_message_id())
        self._known = known
        self._history = [*self._history, *page] if offset else list(page)
        self._render_history()
        if not self._history:
            self._set_status(self._t("tui.history_empty"))
        else:
            self._set_status(self._t("tui.history_loaded", count=len(self._history)))

    def _displayed_history_ids(self) -> list[str]:
        table = self._history_table()
        if table is None:
            return []
        return [str(row_key.value) for row_key in table.rows.keys()]

    async def on_button_pressed_select_all(self) -> None:
        pass

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
            self._set_status(f"[yellow]{self._t('tui.history_none_selected')}[/yellow]")
            return
        self._set_status(self._t("tui.history_analyzing", count=len(chosen)))
        processed = 0
        skipped = 0
        failed: list[str] = []
        for mail in chosen:
            message_id = mail.normalized_message_id()
            try:
                record = await self._service.process_mail(mail)
            except Exception as exc:
                # one bad mail must not abort the batch: keep it picked so
                # the user can retry after fixing the cause
                failed.append(f"{mail.subject[:40]}: {exc}")
                continue
            self._known.add(message_id)
            self._picked.discard(message_id)
            if record is None:
                skipped += 1
            else:
                processed += 1
        self._render_history()
        if failed:
            detail = "; ".join(failed[:3])
            more = f" (+{len(failed) - 3})" if len(failed) > 3 else ""
            self._set_status(
                f"[red]{self._t('tui.history_failed', count=len(failed))}: "
                f"{escape(detail)}{more}[/red]"
            )
        else:
            self._set_status(
                f"[green]{self._t('tui.history_analyzed', count=processed, skipped=skipped)}[/green]"
            )


__all__ = [
    "AccountsPane",
    "EntryFormScreen",
    "LLMPane",
    "ListEditScreen",
    "OptionCard",
    "SettingsPane",
]
