"""MailFlow Textual TUI: a client of the Core service, not another
implementation. Every screen reads service snapshots/data and calls service
methods; no business logic lives here."""

from __future__ import annotations

import asyncio
import queue as queue_module
from datetime import datetime
from typing import Any, ClassVar, cast

from mailflow.domain import ActionItem, MailRecord, ReplyDraft, Urgency
from mailflow.plugin_market import MarketPlugin, Repository
from mailflow.service import MailFlowService
from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

_URGENCY_OPTIONS = [
    ("ad (gray: ads)", "ad"),
    ("info (green: useful)", "info"),
    ("important (orange: read)", "important"),
    ("urgent (red: act now)", "urgent"),
    ("auto", "auto"),
]

_BLANK = ""


def _localize(service: MailFlowService, value: datetime) -> str:
    from zoneinfo import ZoneInfo

    return value.astimezone(ZoneInfo(service.config.general.timezone)).strftime("%Y-%m-%d %H:%M")


class ReplyModal(ModalScreen[Any]):
    """Draft, prepare and confirm a reply with a mandatory confirmation token."""

    BINDINGS: ClassVar[list[Any]] = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, service: MailFlowService, record: MailRecord) -> None:
        super().__init__()
        self._service = service
        self._record = record
        self._draft: ReplyDraft | None = None

    def compose(self) -> ComposeResult:
        yield Static(self._service.t("tui.reply.label", default="Reply"), id="reply-title")
        with Vertical():
            yield Label(f"To: {self._record.mail.sender.display}")
            yield Label(
                f"Subject: {self._service.t('tui.reply.subject_prefix')} {self._record.mail.subject}"
            )
            yield TextArea(
                placeholder=self._service.t("tui.reply_body_placeholder"),
                id="reply-body",
            )
            with Horizontal(id="reply-actions"):
                yield Button(self._service.t("tui.reply_save"), id="reply-save", variant="primary")
                yield Button(
                    self._service.t("tui.reply_prepare"), id="reply-prepare", variant="warning"
                )
                yield Button(
                    self._service.t("tui.reply_confirm"),
                    id="reply-confirm",
                    variant="success",
                    disabled=True,
                )
                yield Button(
                    self._service.t("tui.reply_cancel"), id="reply-cancel", variant="default"
                )
            yield Static(self._service.t("tui.reply_confirm_hint"), id="reply-status")

    async def on_mount(self) -> None:
        self._draft = await self._service.create_reply(self._record.record_id)
        self.query_one("#reply-body", TextArea).text = self._draft.body

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        assert self._draft is not None
        if button_id == "reply-cancel":
            self.dismiss(None)
            return
        if button_id == "reply-save":
            body = self.query_one("#reply-body", TextArea).text
            try:
                self._draft = await self._service.edit_draft(
                    self._draft.draft_id, self._draft.subject, body
                )
                self.query_one("#reply-status", Static).update("✓ saved")
            except ValueError as exc:
                self.query_one("#reply-status", Static).update(str(exc))
            return
        if button_id == "reply-prepare":
            try:
                self._draft = await self._service.prepare_reply(self._draft.draft_id)
            except ValueError as exc:
                self.query_one("#reply-status", Static).update(str(exc))
                return
            status = self.query_one("#reply-status", Static)
            status.update(self._service.t("tui.reply_prepared", token=self._draft.token or ""))
            self.query_one("#reply-confirm", Button).disabled = False
            return
        if button_id == "reply-confirm":
            assert self._draft is not None and self._draft.token is not None
            try:
                await self._service.confirm_reply(self._draft.draft_id, self._draft.token)
            except PermissionError as exc:
                self.query_one("#reply-status", Static).update(str(exc))
                return
            self.query_one("#reply-status", Static).update(self._service.t("tui.reply_sent"))
            self.query_one("#reply-confirm", Button).disabled = True


class ActionModal(ModalScreen[Any]):
    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss", "Close")]

    def __init__(self, service: MailFlowService, item: ActionItem) -> None:
        super().__init__()
        self._service = service
        self._item = item

    def compose(self) -> ComposeResult:
        item = self._item
        yield Static(self._service.t("tui.action_detail"), id="action-title")
        with Vertical():
            yield Label(f"{self._service.t('tui.action_time')}: {item.time_range}")
            yield Label(f"{self._service.t('tui.action_type')}: {item.action_type}")
            yield Label(f"{self._service.t('tui.action_content')}: {item.summary}")
            yield Label(f"{self._service.t('tui.action_notes')}: {item.notes or '-'}")
            yield Label(f"{self._service.t('tui.action_source')}: {item.mail_id}")
            yield Button(self._service.t("tui.btn_close"), id="action-close")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "action-close":
            self.dismiss(None)


class MailPane(Vertical):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._records: list[MailRecord] = []
        self._selected_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Input(placeholder=self._service.t("tui.search_placeholder"), id="mail-search")
        with Horizontal():
            yield DataTable(id="mail-table")
            with Vertical(id="mail-detail"):
                yield Static("", id="mail-summary")
                yield Static("", id="mail-reason")
                yield Static("", id="mail-actions")
                yield Static("", id="mail-body")
                yield Static("", id="mail-notes")
        with Horizontal(id="mail-controls"):
            yield Select(_URGENCY_OPTIONS, id="urgency-select")
            yield Button(self._service.t("tui.btn_refresh"), id="btn-refresh", variant="primary")
            yield Button(self._service.t("tui.btn_trash"), id="btn-trash", variant="error")
            yield Button(self._service.t("tui.btn_reply"), id="btn-reply", variant="success")

    async def on_mount(self) -> None:
        table = self._mail_table()
        table.add_column(self._service.t("tui.column_urgency"), key="urgency")
        table.add_column(self._service.t("tui.column_subject"), key="subject")
        table.add_column(self._service.t("tui.column_sender"), key="sender")
        table.add_column(self._service.t("tui.column_date"), key="date")
        self._urgency_select().tooltip = self._service.t("tui.urgency_help")
        await self.refresh_mail()

    def _mail_table(self) -> DataTable[Any]:
        return self.query_one("#mail-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    def _urgency_select(self) -> Select[Any]:
        return self.query_one("#urgency-select", Select)  # pyright: ignore[reportUnknownVariableType]

    async def refresh_mail(self) -> None:
        table = self._mail_table()
        table.clear()
        self._records = await self._service.list_mails()
        query = self.query_one("#mail-search", Input).value.strip().lower()
        for record in self._records:
            if query and not self._matches(record, query):
                continue
            urgency = record.effective_urgency
            table.add_row(
                RichText(f"■ {urgency.value}", style=urgency.color),
                record.mail.subject or "(no subject)",
                record.mail.sender.address,
                _localize(self._service, record.mail.received_at),
                key=record.record_id,
            )
        if self._records and self._selected_id is None:
            self._selected_id = self._records[0].record_id
        await self._show_selected()

    def _matches(self, record: MailRecord, query: str) -> bool:
        haystack = f"{record.mail.subject} {record.mail.sender.address} {record.summary}".lower()
        return query in haystack

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "mail-search":
            await self.refresh_mail()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._selected_id = event.row_key.value
        await self._show_selected()

    async def _show_selected(self) -> None:
        if self._selected_id is None:
            return
        record = await self._service.get_mail(self._selected_id)
        if record is None:
            return
        service = self._service
        self.query_one("#mail-summary", Static).update(
            f"[bold]{service.t('tui.detail_summary')}:[/bold] {record.summary}"
        )
        reason = record.analysis.reason if record.analysis else ""
        self.query_one("#mail-reason", Static).update(
            f"[bold]{service.t('tui.detail_reason')}:[/bold] {reason or '-'}"
        )
        actions_text = ""
        if record.action_items:
            lines = [
                f"  {item.time_range} [{item.action_type}] {item.summary}"
                + (f" — {item.notes}" if item.notes else "")
                for item in record.action_items
            ]
            actions_text = f"\n[bold]{service.t('tui.action_content')}:[/bold]\n" + "\n".join(lines)
        self.query_one("#mail-actions", Static).update(actions_text)
        body = record.mail.body_text.strip() or "(no body)"
        self.query_one("#mail-body", Static).update(
            f"[bold]{service.t('tui.detail_body')}:[/bold]\n{body}"
        )
        reply_flag = (
            f"\n[bold yellow]{service.t('tui.detail_reply_required', answer=service.t('common.yes'))}[/bold yellow]"
            if record.analysis and record.analysis.reply_required
            else ""
        )
        self.query_one("#mail-notes", Static).update(
            f"{reply_flag}\n{service.t('tui.urgency_label')}: {record.effective_urgency.value} "
            f"({'manual' if record.manual_urgency is not None else 'auto'})"
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-refresh":
            await self.refresh_mail()
            return
        if self._selected_id is None:
            return
        if button_id == "btn-trash":
            await self._service.delete_mail(self._selected_id)
            self._selected_id = None
            await self.refresh_mail()
            return
        if button_id == "btn-reply":
            record = await self._service.get_mail(self._selected_id)
            if record is not None:
                # textual types Widget.app loosely; the cast pins the concrete app
                cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                    ReplyModal(self._service, record)
                )

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "urgency-select" or self._selected_id is None:
            return
        value = event.value
        urgency: Urgency | None = None if value == "auto" else Urgency(str(value))
        await self._service.set_mail_urgency(self._selected_id, urgency)
        await self.refresh_mail()

    def select_mail(self, mail_id: str) -> None:
        self._selected_id = mail_id
        self.call_after_refresh(lambda: self._show_selected())


class ActionsPane(Vertical):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._items: list[ActionItem] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="actions-table")
        yield Static(self._service.t("tui.empty"), id="actions-hint")

    async def on_mount(self) -> None:
        table = self._actions_table()
        table.add_column(self._service.t("tui.action_time"), key="time")
        table.add_column(self._service.t("tui.action_type"), key="type")
        table.add_column(self._service.t("tui.action_content"), key="content")
        table.add_column(self._service.t("tui.action_notes"), key="notes")
        table.add_column(self._service.t("tui.action_source"), key="source")
        await self.refresh_actions()

    def _actions_table(self) -> DataTable[Any]:
        return self.query_one("#actions-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    async def refresh_actions(self) -> None:
        table = self._actions_table()
        table.clear()
        self._items = await self._service.list_actions()
        self.query_one("#actions-hint", Static).update(
            self._service.t("tui.empty") if not self._items else _BLANK
        )
        for item in self._items:
            table.add_row(
                item.time_range,
                item.action_type,
                item.summary,
                item.notes or "-",
                item.mail_id,
                key=item.item_id,
            )

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        item = next((i for i in self._items if i.item_id == event.row_key.value), None)
        if item is not None:
            # textual types Widget.app loosely; the cast pins the concrete app
            cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                ActionModal(self._service, item)
            )


class RuntimePane(Vertical):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service

    def compose(self) -> ComposeResult:
        with ScrollableContainer():
            yield Static("", id="runtime-plugins")
            yield Static("", id="runtime-adapters")
            yield Static("", id="runtime-accounts")
            yield Static("", id="runtime-llms")
            yield Static("", id="runtime-bindings")
            yield Static("", id="runtime-storage")

    async def on_mount(self) -> None:
        await self.refresh_runtime()

    async def refresh_runtime(self) -> None:
        snapshot = self._service.snapshot()
        service = self._service
        plugins = "\n".join(
            f"  {p.plugin_id}  [{', '.join(k.value for k in p.kinds) or '-'}]"
            for p in snapshot.plugins
        )
        adapters = "\n".join(
            f"  {c.component_id} (plugin: {c.plugin_id})"
            for c in snapshot.components
            if c.kind.value == "mail_source"
        )
        accounts = "\n".join(
            f"  {a.account_id}  {a.email}  {a.provider}  [{a.status}]"
            + (f"  error: {a.error}" if a.error else "")
            for a in snapshot.accounts
        )
        llms = "\n".join(
            f"  {llm.llm_id}  model={llm.model}  backend={llm.backend}"
            + ("  [default]" if llm.default else "")
            for llm in snapshot.llms
        )
        bindings = "\n".join(
            f"  {b.processor_id} -> {b.llm_id or 'rules'}"
            + (f"  fallback: {', '.join(b.fallback_llm_ids)}" if b.fallback_llm_ids else "")
            for b in snapshot.processors
        )
        self.query_one("#runtime-plugins", Static).update(
            f"[bold]{service.t('tui.runtime_plugins')}:[/bold]\n{plugins or '  -'}"
        )
        self.query_one("#runtime-adapters", Static).update(
            f"\n[bold]{service.t('tui.runtime_adapters')}:[/bold]\n{adapters or '  -'}"
        )
        self.query_one("#runtime-accounts", Static).update(
            f"\n[bold]{service.t('tui.runtime_accounts')}:[/bold]\n{accounts or '  -'}"
        )
        self.query_one("#runtime-llms", Static).update(
            f"\n[bold]{service.t('tui.runtime_llms')}:[/bold]\n{llms or '  -'}"
        )
        self.query_one("#runtime-bindings", Static).update(
            f"\n[bold]{service.t('tui.runtime_bindings')}:[/bold]\n{bindings or '  -'}"
        )
        self.query_one("#runtime-storage", Static).update(
            f"\n[bold]{service.t('tui.runtime_storage')}:[/bold] {snapshot.storage or '-'}"
        )


class LogsPane(Vertical):
    def __init__(self, service: MailFlowService, log_queue: queue_module.Queue[Any]) -> None:
        super().__init__()
        self._service = service
        self._log_queue = log_queue

    def compose(self) -> ComposeResult:
        yield RichLog(id="log-view", wrap=True, highlight=True, markup=True)

    async def on_mount(self) -> None:
        self.query_one("#log-view", RichLog).write(self._service.t("tui.logs_title"))

    def drain(self) -> None:
        log_view = self.query_one("#log-view", RichLog)
        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue_module.Empty:
                break
            log_view.write(line)


class SettingsPane(Vertical):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service

    def compose(self) -> ComposeResult:
        yield Label(self._service.t("tui.settings_language"), id="settings-label")
        yield Select([], id="language-select")
        yield Static(self._service.t("tui.settings_language_help"), id="settings-help")
        yield Static(self._service.t("tui.settings_config_title"), id="settings-config-title")
        with ScrollableContainer(id="settings-config-scroll"):
            yield DataTable(id="config-table")

    async def on_mount(self) -> None:
        await self.refresh_languages()
        await self.refresh_config()

    async def refresh_languages(self) -> None:
        options = [
            (f"{info.name} ({info.code})", info.code)
            for info in self._service.i18n.available_languages()
        ]
        select = self.query_one("#language-select", Select)  # pyright: ignore[reportUnknownVariableType]
        select.set_options(options)  # pyright: ignore[reportUnknownMemberType]
        select.value = self._service.i18n.language

    def _config_table(self) -> DataTable[Any]:
        return self.query_one("#config-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    async def refresh_config(self) -> None:
        """List every configurable option (required/optional, default, value)."""
        table = self._config_table()
        if not table.columns:
            table.add_column(self._service.t("config.option"), key="option")
            table.add_column(self._service.t("config.type"), key="type")
            table.add_column(self._service.t("config.required"), key="required")
            table.add_column(self._service.t("config.value"), key="value")
            table.add_column(self._service.t("config.description"), key="description")
        table.clear()
        for option in self._service.list_config_options():
            value = option.value
            if isinstance(value, (list, dict)):
                value_text = f"{len(value)} items"  # pyright: ignore[reportUnknownArgumentType]
            elif isinstance(value, bool):
                value_text = "true" if value else "false"
            elif value is None:
                value_text = "-"
            else:
                value_text = str(value)[:24]
            key = option.key + ("*" if option.is_secret() else "")
            required = self._service.t("common.yes") if option.required else ""
            table.add_row(key, option.type_name, required, value_text, option.description[:60])

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "language-select" and event.value:
            await self._service.set_language(str(event.value))
            await self.refresh_config()


class MarketPane(Vertical):
    """Browse the plugin marketplace and install plugins."""

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._entries: list[tuple[Repository, MarketPlugin]] = []
        self._status = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="market-controls"):
            yield Select([], id="market-category")
            yield Button(self._service.t("tui.btn_refresh"), id="market-refresh", variant="primary")
            yield Button(
                self._service.t("tui.market_install"), id="market-install", variant="success"
            )
        yield DataTable(id="market-table")
        yield Static("", id="market-status")

    async def on_mount(self) -> None:
        table = self._market_table()
        table.add_column(self._service.t("plugin.header_id"), key="plugin")
        table.add_column(self._service.t("plugin.header_version"), key="version")
        table.add_column(self._service.t("plugin.market_categories"), key="categories")
        table.add_column(self._service.t("plugin.market_description"), key="description")
        table.add_column(self._service.t("plugin.market_status"), key="status")
        await self.refresh_market()

    def _market_table(self) -> DataTable[Any]:
        return self.query_one("#market-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    async def refresh_market(self) -> None:
        market = self._service.market
        self.query_one("#market-status", Static).update(self._service.t("tui.loading"))
        try:
            self._entries = await asyncio.to_thread(market.list_plugins)
        except Exception as exc:
            self.query_one("#market-status", Static).update(str(exc))
            return
        table = self._market_table()
        table.clear()
        filter_value = cast(str, self.query_one("#market-category", Select).value or "all")  # pyright: ignore[reportUnknownMemberType]
        for _repo, plugin in self._entries:
            if filter_value and filter_value != "all" and filter_value not in plugin.categories:
                continue
            installed = (
                self._service.t("plugin.installed")
                if market.is_installed(plugin.id, package=plugin.package)
                else ""
            )
            table.add_row(
                plugin.id,
                plugin.version,
                ",".join(plugin.categories),
                plugin.description[:40],
                installed,
                key=plugin.id,
            )
        categories = sorted({c for _r, p in self._entries for c in p.categories})
        select = self.query_one("#market-category", Select)  # pyright: ignore[reportUnknownVariableType]
        select.set_options([("all", "all"), *[(c, c) for c in categories]])  # pyright: ignore[reportUnknownMemberType]
        select.value = filter_value if filter_value in {"all", *categories} else "all"
        self.query_one("#market-status", Static).update("")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "market-refresh":
            await self.refresh_market()
            return
        if event.button.id == "market-install":
            table = self._market_table()
            cursor = table.cursor_row
            if cursor < 0 or cursor >= len(self._entries):
                return
            _repo, plugin = self._entries[cursor]
            market = self._service.market
            if market.is_installed(plugin.id, package=plugin.package):
                self.query_one("#market-status", Static).update(
                    self._service.t("plugin.already_installed", plugin_id=plugin.id)
                )
                return
            self.query_one("#market-status", Static).update(self._service.t("tui.loading"))
            try:
                await market.install(plugin)
            except (ValueError, RuntimeError) as exc:
                self.query_one("#market-status", Static).update(str(exc))
                return
            self.query_one("#market-status", Static).update(
                self._service.t("plugin.installed_ok", plugin_id=plugin.id)
                + f" ({self._service.t('plugin.restart_note')})"
            )
            await self.refresh_market()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "market-category":
            await self.refresh_market()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        plugin = next((p for _r, p in self._entries if p.id == event.row_key.value), None)
        if plugin is not None:
            # textual types Widget.app loosely; the cast pins the concrete app
            cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                MarketDetail(self._service, plugin)
            )


class MarketDetail(ModalScreen[Any]):
    """Market plugin detail with an install button."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss", "Close")]

    def __init__(self, service: MailFlowService, plugin: Any) -> None:
        super().__init__()
        self._service = service
        self._plugin = plugin

    def compose(self) -> ComposeResult:
        plugin = self._plugin
        yield Static(f"{plugin.name or plugin.id} {plugin.version}", id="market-detail-title")
        with Vertical():
            yield Label(f"{self._service.t('plugin.header_id')}: {plugin.id}")
            yield Label(
                f"{self._service.t('plugin.market_categories')}: {', '.join(plugin.categories) or '-'}"
            )
            yield Label(
                f"{self._service.t('plugin.market_description')}: {plugin.description or '-'}"
            )
            yield Label(f"{self._service.t('plugin.market_author')}: {plugin.author or '-'}")
            yield Label(f"{self._service.t('plugin.market_license')}: {plugin.license or '-'}")
            yield Label(f"{self._service.t('plugin.market_source')}: {plugin.source or '-'}")
            yield Static("", id="market-detail-status")
            with Horizontal():
                yield Button(
                    self._service.t("tui.market_install"),
                    id="market-detail-install",
                    variant="success",
                )
                yield Button(self._service.t("tui.btn_close"), id="market-detail-close")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "market-detail-close":
            self.dismiss(None)
            return
        if button_id == "market-detail-install":
            market = self._service.market
            plugin = self._plugin
            if market.is_installed(plugin.id, package=plugin.package):
                self.query_one("#market-detail-status", Static).update(
                    self._service.t("plugin.already_installed", plugin_id=plugin.id)
                )
                return
            self.query_one("#market-detail-status", Static).update(self._service.t("tui.loading"))
            try:
                await market.install(plugin)
            except (ValueError, RuntimeError) as exc:
                self.query_one("#market-detail-status", Static).update(str(exc))
                return
            self.query_one("#market-detail-status", Static).update(
                self._service.t("plugin.installed_ok", plugin_id=plugin.id)
                + f" ({self._service.t('plugin.restart_note')})"
            )


class MailFlowApp(App[None]):
    """Five-tab administration UI."""

    CSS_PATH = "app.tcss"
    BINDINGS: ClassVar[list[Any]] = [
        Binding("ctrl+q", "quit", "Quit"),
    ]
    TITLE = "MailFlow"

    def __init__(self, service: MailFlowService, log_queue: queue_module.Queue[Any]) -> None:
        super().__init__()
        self._service = service
        self._log_queue = log_queue
        self._log_timer: Any = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="tab-mail"):
            with TabPane(self._service.t("tui.tab_mail"), id="tab-mail"):
                yield MailPane(self._service)
            with TabPane(self._service.t("tui.tab_actions"), id="tab-actions"):
                yield ActionsPane(self._service)
            with TabPane(self._service.t("tui.tab_runtime"), id="tab-runtime"):
                yield RuntimePane(self._service)
            with TabPane(self._service.t("tui.tab_logs"), id="tab-logs"):
                yield LogsPane(self._service, self._log_queue)
            with TabPane(self._service.t("tui.tab_market"), id="tab-market"):
                yield MarketPane(self._service)
            with TabPane(self._service.t("tui.tab_settings"), id="tab-settings"):
                yield SettingsPane(self._service)
        yield Footer()

    async def on_mount(self) -> None:
        self.title = self._service.t("tui.title")
        self.sub_title = f"v{self._service.snapshot().version}"
        self._log_timer = self.set_interval(1.0, self._drain_logs)
        self._service.on("mail.processed", self._on_mail_processed)

    async def _on_mail_processed(self, event: str, **payload: Any) -> None:
        self.call_from_thread(self._schedule_reload)

    def _schedule_reload(self) -> None:
        self.run_worker(cast(Any, self._reload_all))

    async def _reload_all(self) -> None:
        for pane_type in (MailPane, ActionsPane, RuntimePane):
            panes = self.query(pane_type)
            if not panes:
                continue
            pane = panes.first()
            if isinstance(pane, MailPane):
                await pane.refresh_mail()
            elif isinstance(pane, ActionsPane):
                await pane.refresh_actions()
            else:
                await pane.refresh_runtime()  # type: ignore[attr-defined]

    def _drain_logs(self) -> None:
        log_query = self.query(LogsPane)
        logs = log_query.first() if log_query else None
        if logs is not None:
            logs.drain()

    def open_mail(self, mail_id: str) -> None:
        """Switch to the Mail tab and select a mail (used by action drill-down)."""
        tabs = self.query_one(TabbedContent)
        tabs.active = "tab-mail"
        pane = self.query_one(MailPane)
        pane.select_mail(mail_id)
