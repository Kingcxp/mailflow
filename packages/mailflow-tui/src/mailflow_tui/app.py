"""MailFlow Textual TUI: a client of the Core service, not another
implementation. Every screen reads service snapshots/data and calls service
methods; no business logic lives here."""

from __future__ import annotations

import asyncio
import contextlib
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
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from mailflow_tui.export import BotExportScreen
from mailflow_tui.install import InstallScreen
from mailflow_tui.repos import ReposScreen
from mailflow_tui.scaffold import PluginScaffoldScreen
from mailflow_tui.settings import AccountsPane, LLMPane, SettingsPane

_URGENCY_OPTIONS = [
    ("ad (gray: ads)", "ad"),
    ("info (green: useful)", "info"),
    ("important (orange: read)", "important"),
    ("urgent (red: act now)", "urgent"),
    ("auto", "auto"),
]

_BLANK = ""


def _localize(service: MailFlowService, value: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    from zoneinfo import ZoneInfo

    return value.astimezone(ZoneInfo(service.config.general.timezone)).strftime(fmt)


def _remove_column(table: DataTable[Any], key: str) -> None:
    """Drop a column by key, tolerating absence (used when relabeling)."""
    with contextlib.suppress(Exception):
        table.remove_column(key)


def _typed_select(owner: Any, selector: str) -> Select[str] | None:
    """query_one_optional without leaking an unparametrized Select (whose
    ``value`` attribute comes back Unknown under strict type checking)."""
    return cast("Select[str] | None", owner.query_one_optional(selector))


def _apply_options(owner: Any, selector: str, pairs: list[tuple[str, str]]) -> None:
    """set_options + restore value, skipping identical lists so startup never
    pokes freshly mounted widgets (a Textual render race)."""
    select = owner.query_one_optional(selector, Select)  # pyright: ignore[reportUnknownVariableType]
    if select is None:
        return
    signatures = getattr(owner, "_options_signatures", {})
    signature = repr(pairs)
    if signatures.get(selector) == signature:
        return
    if not getattr(owner, "_options_seeded", False):
        # compose already mounted these exact options; poking a freshly
        # mounted Select races its internal label rendering (Textual #bug)
        owner._options_seeded = True
        return
    owner._options_signatures = {**signatures, selector: signature}
    current = select.value
    select.set_options(pairs)  # pyright: ignore[reportUnknownMemberType]
    if current is not Select.NULL:
        select.value = current  # pyright: ignore[reportUnknownMemberType]


class ReplyModal(ModalScreen[Any]):
    """Draft, prepare and confirm a reply with a mandatory confirmation token.

    A formal-letter template (Chinese / English) pre-fills the body with the
    date filled automatically and a right-aligned signature block; the
    toolbar wraps the selection in bold/italic and aligns paragraphs.
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, service: MailFlowService, record: MailRecord) -> None:
        super().__init__()
        self._service = service
        self._record = record
        self._draft: ReplyDraft | None = None

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Static(self._service.t("tui.reply.label", default="Reply"), id="reply-title")
        with Vertical(id="reply-dialog"):
            yield Label(f"To: {escape(self._record.mail.sender.display)}")
            yield Label(
                f"Subject: {self._service.t('tui.reply.subject_prefix')} {escape(self._record.mail.subject)}"
            )
            with Horizontal(id="reply-templates"):
                yield Button(self._t("tui.reply_tpl_cn"), id="reply-tpl-cn", variant="primary")
                yield Button(self._t("tui.reply_tpl_en"), id="reply-tpl-en", variant="primary")
                yield Static(self._t("tui.reply_tpl_hint"), id="reply-tpl-hint")
            yield TextArea(
                placeholder=self._service.t("tui.reply_body_placeholder"),
                id="reply-body",
            )
            with Horizontal(id="reply-toolbar"):
                yield Button(
                    self._t("tui.reply_toolbar_bold"),
                    id="reply-bold",
                    variant="primary",
                    classes="reply-tool",
                )
                yield Button(
                    self._t("tui.reply_toolbar_italic"),
                    id="reply-italic",
                    variant="primary",
                    classes="reply-tool",
                )
                yield Button(
                    self._t("tui.reply_toolbar_left"),
                    id="reply-align-left",
                    variant="primary",
                    classes="reply-tool",
                )
                yield Button(
                    self._t("tui.reply_toolbar_center"),
                    id="reply-align-center",
                    variant="primary",
                    classes="reply-tool",
                )
                yield Button(
                    self._t("tui.reply_toolbar_right"),
                    id="reply-align-right",
                    variant="primary",
                    classes="reply-tool",
                )
                yield Static(self._t("tui.reply_markup_hint"), id="reply-markup-hint")
            with Horizontal(id="reply-actions"):
                yield Button(self._t("tui.reply_save"), id="reply-save", variant="primary")
                yield Button(self._t("tui.reply_prepare"), id="reply-prepare", variant="warning")
                yield Button(
                    self._t("tui.reply_confirm"),
                    id="reply-confirm",
                    variant="success",
                    disabled=True,
                )
                yield Button(self._t("tui.reply_cancel"), id="reply-cancel", variant="error")
            yield Static(self._t("tui.reply_confirm_hint"), id="reply-status")

    async def on_mount(self) -> None:
        self._draft = await self._service.create_reply(self._record.record_id)
        self.query_one("#reply-body", TextArea).text = self._draft.body

    def _set_status(self, text: str) -> None:
        self.query_one("#reply-status", Static).update(text)

    def _apply_template(self, language: str) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from mailflow.letters import build_letter

        tz = ZoneInfo(self._service.config.general.timezone)
        today = datetime.now(tz).date()
        recipient = self._record.mail.sender.display or self._record.mail.sender.address
        body = build_letter(language, recipient=recipient, today=today)
        self.query_one("#reply-body", TextArea).text = body
        label = self._t("tui.reply_tpl_cn" if language == "cn" else "tui.reply_tpl_en")
        self._set_status(self._t("tui.reply_tpl_applied", language=label))

    def _wrap_selection(self, tag: str) -> None:
        from textual.widgets.text_area import Selection

        textarea = self.query_one("#reply-body", TextArea)
        selection = textarea.selection  # pyright: ignore[reportUnknownVariableType]
        if selection is None or selection.is_empty:  # pyright: ignore[reportUnnecessaryComparison]
            self._set_status(self._t("tui.reply_select_hint"))
            return
        text = textarea.get_text_range(selection.start, selection.end)
        wrapped = f"<{tag}>{text}</{tag}>"
        textarea.replace(wrapped, selection.start, selection.end, maintain_selection_offset=False)
        textarea.selection = Selection(textarea.cursor_location, textarea.cursor_location)
        self._set_status("")

    def _align_paragraph(self, align: str) -> None:
        from textual.widgets.text_area import Selection

        textarea = self.query_one("#reply-body", TextArea)
        selection = textarea.selection  # pyright: ignore[reportUnknownVariableType]
        if selection is None or selection.is_empty:  # pyright: ignore[reportUnnecessaryComparison]
            row, _ = textarea.cursor_location
            line = textarea.document.get_line(row)
            start = (row, 0)
            end = (row, len(line))
        else:
            start, end = selection.start, selection.end
        text = textarea.get_text_range(start, end)
        textarea.replace(
            f'<div style="text-align:{align}">{text}</div>',
            start,
            end,
            maintain_selection_offset=False,
        )
        textarea.selection = Selection(textarea.cursor_location, textarea.cursor_location)
        self._set_status("")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        assert self._draft is not None
        if button_id == "reply-cancel":
            self.dismiss(None)
            return
        if button_id in ("reply-tpl-cn", "reply-tpl-en"):
            self._apply_template("cn" if button_id == "reply-tpl-cn" else "en")
            return
        if button_id in ("reply-bold", "reply-italic"):
            self._wrap_selection("b" if button_id == "reply-bold" else "i")
            return
        if button_id in ("reply-align-left", "reply-align-center", "reply-align-right"):
            self._align_paragraph(
                {
                    "reply-align-left": "left",
                    "reply-align-center": "center",
                    "reply-align-right": "right",
                }[button_id]
            )
            return
        if button_id == "reply-save":
            body = self.query_one("#reply-body", TextArea).text
            try:
                self._draft = await self._service.edit_draft(
                    self._draft.draft_id, self._draft.subject, body
                )
                self._set_status("✓ saved")
            except ValueError as exc:
                self._set_status(str(exc))
            return
        if button_id == "reply-prepare":
            try:
                self._draft = await self._service.prepare_reply(self._draft.draft_id)
            except ValueError as exc:
                self._set_status(str(exc))
                return
            self._set_status(self._t("tui.reply_prepared", token=self._draft.token or ""))
            self.query_one("#reply-confirm", Button).disabled = False
            return
        if button_id == "reply-confirm":
            assert self._draft is not None and self._draft.token is not None
            try:
                await self._service.confirm_reply(self._draft.draft_id, self._draft.token)
            except PermissionError as exc:
                self._set_status(str(exc))
                return
            self._set_status(self._t("tui.reply_sent"))
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
            yield Label(f"{self._service.t('tui.action_time')}: {escape(item.time_range)}")
            yield Label(f"{self._service.t('tui.action_type')}: {escape(item.action_type)}")
            yield Label(f"{self._service.t('tui.action_content')}: {escape(item.summary)}")
            yield Label(f"{self._service.t('tui.action_notes')}: {escape(item.notes or '-')}")
            yield Label(f"{self._service.t('tui.action_source')}: {escape(item.mail_id)}")
            yield Button(self._service.t("tui.btn_close"), id="action-close", variant="primary")

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
            yield Select(
                [("all", "all")] + [(u.value, u.value) for u in Urgency],
                id="mail-urgency-filter",
                allow_blank=False,
            )
            yield Select(
                [
                    (self._service.t("tui.sort_urgency"), "urgency"),
                    (self._service.t("tui.sort_time"), "time"),
                ],
                id="mail-sort",
                allow_blank=False,
            )
            yield Button(self._service.t("tui.btn_refresh"), id="btn-refresh", variant="primary")
            yield Button(self._service.t("tui.btn_trash"), id="btn-trash", variant="error")
            yield Button(self._service.t("tui.btn_reply"), id="btn-reply", variant="success")

    async def on_mount(self) -> None:
        await self.refresh_mail()

    def _mail_table(self) -> DataTable[Any] | None:
        return self.query_one_optional("#mail-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    def _urgency_select(self) -> Select[Any] | None:
        return self.query_one_optional("#urgency-select", Select)  # pyright: ignore[reportUnknownVariableType]

    def _ensure_columns(self) -> None:
        if getattr(self, "_columns_done", False):
            return
        table = self._mail_table()
        if table is None:
            return
        for key in ("urgency", "subject", "sender", "date"):
            _remove_column(table, key)
        table.add_column(self._service.t("tui.column_urgency"), key="urgency")
        table.add_column(self._service.t("tui.column_subject"), key="subject")
        table.add_column(self._service.t("tui.column_sender"), key="sender")
        table.add_column(self._service.t("tui.column_date"), key="date")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        urgency = self._urgency_select()
        if urgency is not None:
            urgency.tooltip = self._service.t("tui.urgency_help")
        self._columns_done = True

    async def relabel(self) -> None:
        self._columns_done = False
        await self.refresh_mail()

    async def refresh_mail(self) -> None:
        table = self._mail_table()
        if table is None:
            return
        table.clear()
        self._ensure_columns()
        self._refresh_view_options()
        self._records = await self._service.list_mails()
        search = self.query_one_optional("#mail-search", Input)
        query = search.value.strip().lower() if search is not None else ""
        records = list(self._records)
        if query:
            records = [record for record in records if self._matches(record, query)]
        urgency_filter = self._select_value("#mail-urgency-filter")
        if urgency_filter != "all":
            records = [
                record for record in records if record.effective_urgency.value == urgency_filter
            ]
        # stable two-pass sort: newest first, then urgency when requested —
        # the default view puts urgent mail on top without losing recency
        records.sort(key=lambda record: record.mail.received_at, reverse=True)
        if self._select_value("#mail-sort") == "urgency":
            records.sort(key=lambda record: record.effective_urgency.rank, reverse=True)
        for record in records:
            urgency = record.effective_urgency
            table.add_row(
                RichText(f"■ {urgency.value}", style=urgency.color),
                escape(record.mail.subject or "(no subject)"),
                escape(record.mail.sender.address),
                _localize(self._service, record.mail.received_at, "%m-%d %H:%M"),
                key=record.record_id,
            )
        visible_ids = {record.record_id for record in records}
        if self._selected_id not in visible_ids:
            self._selected_id = records[0].record_id if records else None
        await self._show_selected()

    def _select_value(self, selector: str) -> str:
        select = _typed_select(self, selector)
        if select is not None:
            value = select.value
            if value is not Select.NULL:
                return str(value)
        return "all" if "filter" in selector else "time"

    def _refresh_view_options(self) -> None:
        """Re-translate the sort/filter dropdowns after a language switch."""
        service = self._service
        _apply_options(
            self, "#mail-urgency-filter", [("all", "all")] + [(u.value, u.value) for u in Urgency]
        )
        _apply_options(
            self,
            "#mail-sort",
            [
                (service.t("tui.sort_urgency"), "urgency"),
                (service.t("tui.sort_time"), "time"),
            ],
        )

    def _matches(self, record: MailRecord, query: str) -> bool:
        haystack = f"{record.mail.subject} {record.mail.sender.address} {record.summary}".lower()
        return query in haystack

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "mail-search":
            await self.refresh_mail()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._selected_id = event.row_key.value
        await self._show_selected()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in ("mail-urgency-filter", "mail-sort"):
            await self.refresh_mail()
        elif event.select.id == "urgency-select" and self._selected_id is not None:
            value = event.value
            urgency: Urgency | None = None if value == "auto" else Urgency(str(value))
            await self._service.set_mail_urgency(self._selected_id, urgency)
            await self.refresh_mail()

    async def _show_selected(self) -> None:
        """Render the detail column for the selected mail."""
        if self._selected_id is None:
            return
        record = await self._service.get_mail(self._selected_id)
        if record is None:
            return
        service = self._service
        self._set_static(
            "#mail-summary",
            f"[bold]{service.t('tui.detail_summary')}:[/bold] {escape(record.summary)}",
        )
        reason = record.analysis.reason if record.analysis else ""
        self._set_static(
            "#mail-reason",
            f"[bold]{service.t('tui.detail_reason')}:[/bold] {escape(reason or '-')}",
        )
        actions_text = ""
        if record.action_items:
            lines = [
                "  "
                + escape(f"{item.time_range} ({item.action_type}) {item.summary}")
                + (f" — {escape(item.notes)}" if item.notes else "")
                for item in record.action_items
            ]
            actions_text = f"\n[bold]{service.t('tui.action_content')}:[/bold]\n" + "\n".join(lines)
        self._set_static("#mail-actions", actions_text)
        body = record.mail.body_text.strip() or "(no body)"
        self._set_static(
            "#mail-body", f"[bold]{service.t('tui.detail_body')}:[/bold]\n{escape(body)}"
        )
        reply_flag = (
            f"\n[bold yellow]{service.t('tui.detail_reply_required', answer=service.t('common.yes'))}[/bold yellow]"
            if record.analysis and record.analysis.reply_required
            else ""
        )
        self._set_static(
            "#mail-notes",
            f"{reply_flag}\n{service.t('tui.urgency_label')}: {record.effective_urgency.value} "
            f"({'manual' if record.manual_urgency is not None else 'auto'})",
        )

    def _set_static(self, selector: str, content: str) -> None:
        node = self.query_one_optional(selector, Static)
        if node is not None:
            node.update(content)

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
            if getattr(self._service, "remote", False):
                from mailflow_server.client import RemoteUnsupported

                try:
                    raise RemoteUnsupported("reply drafts require a local service")
                except RemoteUnsupported as exc:
                    self._set_static("#mail-notes", f"[yellow]{exc}[/yellow]")
                return
            record = await self._service.get_mail(self._selected_id)
            if record is not None:
                # textual types Widget.app loosely; the cast pins the concrete app
                cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                    ReplyModal(self._service, record)
                )

    def select_mail(self, mail_id: str) -> None:
        self._selected_id = mail_id
        self.call_after_refresh(lambda: self._show_selected())


class ActionsPane(Vertical):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._items: list[ActionItem] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="actions-controls"):
            yield Select(
                [
                    (self._service.t("tui.range_all"), "all"),
                    (self._service.t("tui.range_today"), "today"),
                    (self._service.t("tui.range_week"), "week"),
                ],
                id="actions-range",
                allow_blank=False,
            )
            yield Select(
                [(self._service.t("tui.filter_all_types"), "all")],
                id="actions-type-filter",
                allow_blank=False,
            )
        yield DataTable(id="actions-table")
        yield Static(self._service.t("tui.empty"), id="actions-hint")

    async def on_mount(self) -> None:
        await self.refresh_actions()

    def _actions_table(self) -> DataTable[Any] | None:
        return self.query_one_optional("#actions-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    def _ensure_columns(self) -> None:
        if getattr(self, "_columns_done", False):
            return
        table = self._actions_table()
        if table is None:
            return
        for key in ("time", "type", "content", "notes", "source"):
            _remove_column(table, key)
        table.add_column(self._service.t("tui.action_time"), key="time")
        table.add_column(self._service.t("tui.action_type"), key="type")
        table.add_column(self._service.t("tui.action_content"), key="content")
        table.add_column(self._service.t("tui.action_notes"), key="notes")
        table.add_column(self._service.t("tui.action_source"), key="source")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        self._columns_done = True

    async def relabel(self) -> None:
        self._columns_done = False
        await self.refresh_actions()

    async def refresh_actions(self) -> None:
        table = self._actions_table()
        if table is None:
            return
        table.clear()
        self._ensure_columns()
        self._items = await self._service.list_actions()
        items = list(self._items)
        type_filter = self._select_value("#actions-type-filter")
        if type_filter != "all":
            items = [item for item in items if item.action_type == type_filter]
        range_mode = self._select_value("#actions-range")
        if range_mode != "all":
            from datetime import UTC, datetime, timedelta

            now = datetime.now(UTC)
            horizon = (
                now.replace(hour=23, minute=59, second=59, microsecond=0)
                if range_mode == "today"
                else now + timedelta(days=7)
            )
            items = [item for item in items if item.due_at <= horizon]
        types = sorted({item.action_type for item in self._items})
        _apply_options(
            self,
            "#actions-type-filter",
            [(self._service.t("tui.filter_all_types"), "all")] + [(t, t) for t in types],
        )
        current = self._select_value("#actions-type-filter")
        if current not in {"all", *types}:
            # stale selection vanished from the list: reset without recursing
            type_select = _typed_select(self, "#actions-type-filter")
            if type_select is not None:
                selected_value = type_select.value
                if selected_value is not Select.NULL and str(selected_value) != "all":
                    type_select.value = "all"
        hint = self.query_one_optional("#actions-hint", Static)
        if hint is not None:
            hint.update(self._service.t("tui.empty") if not items else _BLANK)
        for item in items:
            table.add_row(
                escape(item.time_range),
                escape(item.action_type),
                escape(item.summary),
                escape(item.notes or "-"),
                escape(item.mail_id),
                key=item.item_id,
            )

    def _select_value(self, selector: str) -> str:
        select = _typed_select(self, selector)
        if select is None or select.value is Select.NULL:
            return "all"
        return str(select.value)

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in ("actions-type-filter", "actions-range"):
            await self.refresh_actions()

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
        self._selected_plugin: str | None = None

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="runtime-scroll"):
            yield Static(self._service.t("tui.runtime_plugins"), id="runtime-plugins")
            yield DataTable(id="runtime-plugins-table")
            yield Static(self._service.t("tui.runtime_plugins_help"), id="runtime-plugins-hint")
            with Horizontal(id="runtime-plugin-actions"):
                yield Button(
                    self._service.t("tui.btn_disable"), id="runtime-plugin-disable", variant="error"
                )
                yield Button(
                    self._service.t("tui.btn_enable"), id="runtime-plugin-enable", variant="success"
                )
                yield Button(
                    self._service.t("tui.btn_uninstall"),
                    id="runtime-plugin-uninstall",
                    variant="warning",
                )
            yield Static("", id="runtime-status")
            yield Static("", id="runtime-adapters")
            yield Static("", id="runtime-accounts")
            yield Static("", id="runtime-llms")
            yield Static("", id="runtime-bindings")
            yield Static("", id="runtime-storage")

    async def on_mount(self) -> None:
        await self.refresh_runtime()

    async def relabel(self) -> None:
        self._columns_done = False
        await self.refresh_runtime()

    def _set_static(self, selector: str, content: str) -> None:
        node = self.query_one_optional(selector, Static)
        if node is not None:
            node.update(content)

    def _plugins_table(self) -> DataTable[Any] | None:
        return self.query_one_optional("#runtime-plugins-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    def _ensure_columns(self) -> None:
        if getattr(self, "_columns_done", False):
            return
        table = self._plugins_table()
        if table is None:
            return
        for key in ("plugin", "name", "kinds", "status"):
            _remove_column(table, key)
        table.add_column(self._service.t("plugin.header_id"), key="plugin")
        table.add_column(self._service.t("plugin.header_name"), key="name")
        table.add_column(self._service.t("plugin.header_provides"), key="kinds")
        table.add_column(self._service.t("plugin.market_status"), key="status")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        self._columns_done = True

    async def refresh_runtime(self) -> None:
        snapshot = self._service.snapshot()
        service = self._service
        table = self._plugins_table()
        if table is None:
            return
        table.clear()
        self._ensure_columns()
        for plugin in snapshot.plugins:
            status_key = service.plugin_status(plugin.plugin_id)
            status_text = {
                "enabled": service.t("tui.plugin_status_enabled"),
                "disabled": service.t("tui.plugin_status_disabled"),
                "not_loaded": service.t("tui.plugin_status_not_loaded"),
            }.get(status_key, status_key)
            table.add_row(
                escape(plugin.plugin_id),
                escape(plugin.name),
                ",".join(k.value for k in plugin.kinds) or "-",
                status_text,
                key=plugin.plugin_id,
            )
        adapters = "\n".join(
            escape(f"  {c.component_id} (plugin: {c.plugin_id})")
            for c in snapshot.components
            if c.kind.value == "mail_source"
        )
        accounts = "\n".join(
            escape(
                f"  {a.account_id}  {a.email}  {a.provider} ({a.status})"
                + (f"  error: {a.error}" if a.error else "")
            )
            for a in snapshot.accounts
        )
        llms = "\n".join(
            escape(f"  {llm.llm_id}  model={llm.model}  backend={llm.backend}")
            + ("  (default)" if llm.default else "")
            for llm in snapshot.llms
        )
        bindings = "\n".join(
            escape(
                f"  {b.processor_id} -> {b.llm_id or 'rules'}"
                + (f"  fallback: {', '.join(b.fallback_llm_ids)}" if b.fallback_llm_ids else "")
            )
            for b in snapshot.processors
        )
        self._set_static(
            "#runtime-adapters",
            f"\n[bold]{service.t('tui.runtime_adapters')}:[/bold]\n{adapters or '  -'}",
        )
        self._set_static(
            "#runtime-accounts",
            f"\n[bold]{service.t('tui.runtime_accounts')}:[/bold]\n{accounts or '  -'}",
        )
        self._set_static(
            "#runtime-llms",
            f"\n[bold]{service.t('tui.runtime_llms')}:[/bold]\n{llms or '  -'}",
        )
        self._set_static(
            "#runtime-bindings",
            f"\n[bold]{service.t('tui.runtime_bindings')}:[/bold]\n{bindings or '  -'}",
        )
        self._set_static(
            "#runtime-storage",
            f"\n[bold]{service.t('tui.runtime_storage')}:[/bold] {snapshot.storage or '-'}",
        )

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        plugin_id = cast(str, event.row_key.value)
        self._selected_plugin = plugin_id
        status = self._service.plugin_status(plugin_id)
        self.query_one("#runtime-status", Static).update(
            f"{self._service.t('plugin.header_id')}: {plugin_id} — {status}"
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._selected_plugin is None:
            return
        button_id = event.button.id
        status = self.query_one("#runtime-status", Static)
        plugin_id = self._selected_plugin
        try:
            if button_id == "runtime-plugin-disable":
                await self._service.plugin_disable(plugin_id)
            elif button_id == "runtime-plugin-enable":
                created = await self._service.plugin_enable(plugin_id)
            elif button_id == "runtime-plugin-uninstall":
                output = await self._service.plugin_uninstall(plugin_id)
            else:
                return
        except (KeyError, ValueError, RuntimeError) as exc:
            status.update(f"[red]{exc}[/red]")
            return
        except Exception as exc:  # uv failures surface here too
            status.update(f"[red]{exc}[/red]")
            return
        if button_id == "runtime-plugin-uninstall":
            status.update(
                f"{self._service.t('plugin.uninstalled_ok', plugin_id=plugin_id)}"
                + f" ({self._service.t('plugin.restart_note')})"
                + (f"\n{output}" if output else "")
            )
        elif button_id == "runtime-plugin-enable":
            text = self._service.t("plugin.enabled_ok", plugin_id=plugin_id)
            if created:
                text += "\n" + self._service.t("plugin.instance_created", notifier_id=created)
            status.update(f"{text}\n({self._service.t('plugin.applies_now')})")
        else:
            status.update(
                f"{self._service.t('plugin.disabled_ok', plugin_id=plugin_id)}"
                + f"\n({self._service.t('plugin.applies_now')})"
            )
        await self.refresh_runtime()


class LogsPane(Vertical):
    def __init__(self, service: MailFlowService, log_queue: queue_module.Queue[Any]) -> None:
        super().__init__()
        self._service = service
        self._log_queue = log_queue

    def compose(self) -> ComposeResult:
        yield RichLog(id="log-view", wrap=True, highlight=True)

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


class MarketDetailScreen(ModalScreen[Any]):
    """VS Code-style full-screen plugin detail: metadata, markdown readme
    and install/uninstall/enable/disable actions."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss", "Close")]

    def __init__(self, service: MailFlowService, plugin: MarketPlugin) -> None:
        super().__init__()
        self._service = service
        self._plugin = plugin

    def compose(self) -> ComposeResult:
        plugin = self._plugin
        language = self._service.i18n.language
        readme = plugin.readme_for(language) or (
            f"# {plugin.name or plugin.id}\n\n{plugin.description_for(language)}"
        )
        meta = (
            f"**{plugin.name or plugin.id}** v{plugin.version} — `{plugin.id}`\n\n"
            f"{self._service.t('plugin.field_author')}: {plugin.author or '-'} · "
            f"{self._service.t('plugin.field_updated')}: {plugin.updated or '-'}\n"
            f"{self._service.t('plugin.field_homepage')}: {plugin.homepage or '-'}\n"
        )
        with Vertical(id="market-detail-dialog"):
            yield Static(self._service.t("tui.market_detail"), id="market-detail-title")
            yield Markdown(meta + "\n---\n\n" + readme, id="market-detail-readme")
            yield Static("", id="market-detail-status")
            with Horizontal(id="market-detail-actions"):
                yield Button(
                    self._service.t("tui.btn_install"),
                    id="detail-install",
                    variant="success",
                )
                yield Button(
                    self._service.t("tui.btn_uninstall"),
                    id="detail-uninstall",
                    variant="warning",
                )
                yield Button(
                    self._service.t("tui.btn_enable"), id="detail-enable", variant="primary"
                )
                yield Button(
                    self._service.t("tui.btn_disable"), id="detail-disable", variant="error"
                )
                yield Button(self._service.t("tui.btn_close"), id="detail-close", variant="primary")

    def _set_status(self, text: str) -> None:
        status = self.query_one_optional("#market-detail-status", Static)
        if status is not None:
            status.update(text)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "detail-close":
            self.dismiss(None)
            return
        plugin = self._plugin
        market = self._service.market
        try:
            if button_id == "detail-install":
                if market.is_installed(plugin.id, package=plugin.package):
                    self._set_status(
                        self._service.t("plugin.already_installed", plugin_id=plugin.id)
                    )
                    return
                await market.install(plugin)
                message_key = "plugin.installed_ok"
            elif button_id == "detail-uninstall":
                if not plugin.package:
                    raise ValueError(f"plugin {plugin.id!r} has no pip package to uninstall")
                await market.uninstall(plugin)
                message_key = "plugin.uninstalled_ok"
            elif button_id == "detail-enable":
                await self._service.plugin_enable(plugin.id)
                message_key = "plugin.enabled_ok"
            elif button_id == "detail-disable":
                await self._service.plugin_disable(plugin.id)
                message_key = "plugin.disabled_ok"
            else:
                return
        except (KeyError, ValueError, RuntimeError) as exc:
            self._set_status(str(exc))
            return
        self._set_status(
            self._service.t(message_key, plugin_id=plugin.id)
            + f" ({self._service.t('plugin.restart_note')})"
        )
        await asyncio.sleep(0.4)
        self.dismiss(button_id)


class MarketPane(Vertical):
    """VS Code-style marketplace: search, category filter, list, markdown
    detail with install/uninstall/enable/disable."""

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._entries: list[tuple[Repository, MarketPlugin]] = []
        self._selected: MarketPlugin | None = None
        self._installed: dict[str, bool] = {}
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical(id="market-controls"):
            with Horizontal(id="market-controls-top"):
                yield Input(
                    placeholder=self._service.t("tui.market_search_placeholder"),
                    id="market-search",
                )
                yield Select([], id="market-category")
            with Horizontal(id="market-controls-buttons"):
                yield Button(
                    self._service.t("tui.btn_refresh"), id="market-refresh", variant="primary"
                )
                yield Button(
                    self._service.t("tui.btn_new_plugin"), id="market-create", variant="primary"
                )
                yield Button(
                    self._service.t("tui.btn_export"), id="market-export", variant="success"
                )
                yield Button(
                    self._service.t("tui.btn_install_local"),
                    id="market-install-local",
                    variant="primary",
                )
                yield Button(self._service.t("tui.btn_repos"), id="market-repos", variant="primary")
        yield DataTable(id="market-table")
        with Vertical(id="market-detail"):
            yield Markdown("", id="market-readme")
            yield Static("", id="market-status")
            with Horizontal(id="market-actions"):
                yield Button(
                    self._service.t("tui.btn_install"), id="market-install", variant="success"
                )
                yield Button(
                    self._service.t("tui.btn_uninstall"), id="market-uninstall", variant="warning"
                )
                yield Button(
                    self._service.t("tui.btn_enable"), id="market-enable", variant="primary"
                )
                yield Button(
                    self._service.t("tui.btn_disable"), id="market-disable", variant="error"
                )

    async def on_mount(self) -> None:
        await self.refresh_market()

    def _market_table(self) -> DataTable[Any] | None:
        return self.query_one_optional("#market-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    def _set_status(self, text: str) -> None:
        status = self.query_one_optional("#market-status", Static)
        if status is not None:
            status.update(text)

    def _ensure_columns(self) -> None:
        if getattr(self, "_columns_done", False):
            return
        table = self._market_table()
        if table is None:
            return
        for key in ("plugin", "description", "version", "status"):
            _remove_column(table, key)
        table.add_column(self._service.t("plugin.header_name"), key="plugin")
        table.add_column(self._service.t("plugin.market_description"), key="description")
        table.add_column(self._service.t("plugin.header_version"), key="version")
        table.add_column(self._service.t("plugin.market_status"), key="status")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        self._columns_done = True

    async def relabel(self) -> None:
        self._columns_done = False
        self._render_entries()

    def _market_status_of(self, plugin: MarketPlugin) -> str:
        installed = self._installed.get(plugin.id)
        if installed is None:
            installed = self._service.market.is_installed(plugin.id, package=plugin.package)
            self._installed[plugin.id] = installed
        if not installed:
            return self._service.t("plugin.not_installed_yet")
        config_status = self._service.plugin_status(plugin.id)
        if config_status == "disabled":
            return self._service.t("tui.plugin_status_disabled")
        if config_status == "enabled":
            return self._service.t("tui.plugin_status_enabled")
        return self._service.t("plugin.installed")

    async def refresh_market(self) -> None:
        """Fetch the marketplace in a worker; the UI stays interactive.

        Only the network fetch is deferred — filtering and rendering run from
        the cached entries, so typing in the search box never re-fetches.
        """
        if self._loading:
            return  # a fetch is already in flight; its result will render
        self._loading = True
        self._set_status(self._service.t("tui.loading"))
        self.run_worker(self._fetch_entries(), exclusive=True, group="market-fetch")

    async def _fetch_entries(self) -> None:
        market = self._service.market
        try:
            entries = await asyncio.to_thread(market.list_plugins)
        except Exception as exc:
            self._loading = False
            self._set_status(str(exc))
            return
        self._entries = entries
        self._installed = {}  # re-derive install state for the new metadata
        self._loading = False
        self._render_entries()

    def _render_entries(self) -> None:
        """Render the cached entries through the current search/category."""
        table = self._market_table()
        if table is None:
            return
        table.clear()
        self._ensure_columns()
        category = self.query_one_optional("#market-category", Select)  # pyright: ignore[reportUnknownVariableType]
        filter_value = cast(str, category.value or "all") if category is not None else "all"  # pyright: ignore[reportUnknownMemberType]
        search = self.query_one_optional("#market-search", Input)
        query = search.value.strip().lower() if search is not None else ""
        language = self._service.i18n.language
        for _repo, plugin in self._entries:
            if filter_value and filter_value != "all" and filter_value not in plugin.categories:
                continue
            if query:
                blob = f"{plugin.id} {plugin.name} {plugin.description}".lower()
                blob += f" {plugin.description_for(language)}".lower()
                if query not in blob:
                    continue
            table.add_row(
                escape(plugin.name or plugin.id),
                escape(plugin.description_for(language)[:44]),
                escape(plugin.version),
                self._market_status_of(plugin),
                key=plugin.id,
            )
        categories = sorted({c for _r, p in self._entries for c in p.categories})
        if categories != getattr(self, "_categories", None):
            select = self.query_one_optional("#market-category", Select)  # pyright: ignore[reportUnknownVariableType]
            if select is not None:
                select.set_options([("all", "all"), *[(c, c) for c in categories]])  # pyright: ignore[reportUnknownMemberType]
                self._categories = categories
        select = self.query_one_optional("#market-category", Select)  # pyright: ignore[reportUnknownVariableType]
        desired = filter_value if filter_value in {*categories, "all"} else "all"
        if select is not None and select.value != desired:  # pyright: ignore[reportUnknownMemberType]
            select.value = desired
        self._set_status("")
        if self._selected is not None:
            self._show_detail(self._selected)

    def _show_detail(self, plugin: MarketPlugin) -> None:
        self._selected = plugin
        language = self._service.i18n.language
        readme = plugin.readme_for(language) or (
            f"# {plugin.name or plugin.id}\n\n{plugin.description_for(language)}"
        )
        meta = (
            f"**{plugin.name or plugin.id}** v{plugin.version}\n\n"
            f"{self._service.t('plugin.field_author')}: {plugin.author or '-'} · "
            f"{self._service.t('plugin.field_updated')}: {plugin.updated or '-'}\n"
            f"{self._service.t('plugin.field_homepage')}: {plugin.homepage or '-'}\n\n---\n\n"
        )
        readme_node = self.query_one_optional("#market-readme", Markdown)
        if readme_node is not None:
            readme_node.update(meta + readme)
        self._set_status(
            f"{self._service.t('plugin.header_id')}: {plugin.id} — {self._market_status_of(plugin)}"
        )

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "market-search":
            # filter the cached entries; no network round-trip per keystroke
            self._render_entries()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        plugin = next((p for _r, p in self._entries if p.id == event.row_key.value), None)
        if plugin is not None:
            self._show_detail(plugin)
            cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                MarketDetailScreen(self._service, plugin)
            )

    async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        plugin = next((p for _r, p in self._entries if p.id == event.row_key.value), None)
        if plugin is not None and plugin is not self._selected:
            self._show_detail(plugin)

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "market-category":
            self._render_entries()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "market-refresh":
            await self.refresh_market()
            return
        if button_id == "market-create":
            self.app.push_screen(PluginScaffoldScreen(self._service))  # pyright: ignore[reportUnknownMemberType]
            return
        if button_id == "market-export":
            cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                BotExportScreen(self._service)
            )
            return
        if button_id == "market-install-local":
            cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                InstallScreen(self._service)
            )
            return
        if button_id == "market-repos":
            cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                ReposScreen(self._service)
            )
            return
        if self._selected is None:
            return
        plugin = self._selected
        market = self._service.market
        try:
            if button_id == "market-install":
                if market.is_installed(plugin.id, package=plugin.package):
                    self.query_one("#market-status", Static).update(
                        self._service.t("plugin.already_installed", plugin_id=plugin.id)
                    )
                    return
                await market.install(plugin)
                message_key = "plugin.installed_ok"
            elif button_id == "market-uninstall":
                await self.service_uninstall(plugin)
                message_key = "plugin.uninstalled_ok"
            elif button_id == "market-enable":
                await self._service.plugin_enable(plugin.id)
                message_key = "plugin.enabled_ok"
            elif button_id == "market-disable":
                await self._service.plugin_disable(plugin.id)
                message_key = "plugin.disabled_ok"
            else:
                return
        except (KeyError, ValueError, RuntimeError) as exc:
            self.query_one("#market-status", Static).update(str(exc))
            return
        self._installed.pop(plugin.id, None)  # install state changed
        self._render_entries()
        self.query_one("#market-status", Static).update(
            self._service.t(message_key, plugin_id=plugin.id)
            + f" ({self._service.t('plugin.restart_note')})"
        )

    async def service_uninstall(self, plugin: MarketPlugin) -> str:
        if not plugin.package:
            raise ValueError(f"plugin {plugin.id!r} has no pip package to uninstall")
        return await self._service.market.uninstall(plugin)


class _BotStatusProbe:
    """Connectivity probes for IM bot backends (OneBot v11 / WeChaty /
    OpenClaw). Each returns a human-readable status line."""

    @staticmethod
    async def probe(provider: str, options: dict[str, Any]) -> str:
        import httpx

        try:
            if provider == "onebot":
                url = str(options.get("http_url", "")).rstrip("/")
                if not url:
                    return "not configured"
                headers = {}
                token = str(options.get("access_token", ""))
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.post(f"{url}/get_login_info", json={}, headers=headers)
                if response.status_code == 200:
                    data = response.json().get("data") or {}
                    return f"logged in as {data.get('nickname', '?')} ({data.get('user_id', '?')})"
                return f"HTTP {response.status_code}"
            if provider == "wechaty":
                url = str(options.get("gateway_url", "")).rstrip("/")
                if not url:
                    return "not configured"
                headers: dict[str, str] = {}
                token_w = str(options.get("token", ""))
                if token_w:
                    headers["Authorization"] = f"Bearer {token_w}"
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(f"{url}/health", headers=headers)
                return "online" if response.status_code == 200 else f"HTTP {response.status_code}"
            if provider == "openclaw-weixin":
                url = str(options.get("base_url", "")).rstrip("/")
                if not url:
                    return "not configured"
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(url)
                return (
                    "gateway reachable"
                    if response.status_code < 500
                    else f"HTTP {response.status_code}"
                )
        except Exception as exc:
            return f"{type(exc).__name__}: unreachable"
        return "unknown provider"


class BotsPane(Vertical):
    """平台登录: manage IM bot instances (OneBot/WeChaty/OpenClaw) and
    check their login state. QR scanning happens in the bot runtime itself
    (NapCat / WeChaty gateway / OpenClaw) — this tab verifies the session."""

    IM_PROVIDERS: ClassVar[frozenset[str]] = frozenset({"onebot", "wechaty", "openclaw-weixin"})

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service

    def compose(self) -> ComposeResult:
        yield Static(self._service.t("tui.bots_title"), id="bots-title")
        yield DataTable(id="bots-table")
        yield Button(self._service.t("tui.bots_check"), id="bots-check", variant="primary")
        yield Static("", id="bots-status")

    def _im_instances(self) -> list[tuple[str, str, dict[str, Any]]]:
        out: list[tuple[str, str, dict[str, Any]]] = []
        for notifier in self._service.config.notifiers:
            if notifier.provider in self.IM_PROVIDERS:
                out.append((notifier.notifier_id, notifier.provider, notifier.options))
        return out

    def _ensure_columns(self) -> None:
        table = self.query_one("#bots-table", DataTable)
        table.clear(columns=True)
        table.add_column(self._service.t("plugin.header_name"), key="name")
        table.add_column(
            self._service.t("plugin.market_provider", default="provider"), key="provider"
        )
        table.add_column(self._service.t("tui.bots_targets"), key="targets")
        table.add_column(self._service.t("tui.bots_status"), key="status")

    def _render(self, statuses: dict[str, str] | None = None) -> None:
        statuses = statuses or {}
        table = self.query_one("#bots-table", DataTable)
        table.clear()
        for notifier_id, provider, options in self._im_instances():
            targets = ", ".join(str(t) for t in options.get("targets") or []) or "-"
            table.add_row(
                notifier_id,
                provider,
                escape(targets),
                statuses.get(notifier_id, "-"),
                key=notifier_id,
            )

    def on_mount(self) -> None:
        self._ensure_columns()
        self._render()

    async def relabel(self) -> None:
        self.on_mount()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "bots-check":
            return
        status_node = self.query_one("#bots-status", Static)
        status_node.update(self._service.t("tui.loading"))
        results: dict[str, str] = {}
        for notifier_id, provider, options in self._im_instances():
            results[notifier_id] = await _BotStatusProbe.probe(provider, options)
        self._render(results)
        status_node.update(self._service.t("tui.bots_checked"))


class MailFlowApp(App[None]):
    """Eight-tab administration UI."""

    CSS_PATH = "app.tcss"
    BINDINGS: ClassVar[list[Any]] = [
        Binding("ctrl+q", "quit", "Quit"),
    ]
    TITLE = "MailFlow"

    def __init__(
        self,
        service: Any,  # MailFlowService | remote.RemoteServiceAdapter
        log_queue: queue_module.Queue[Any],
        *,
        remote: bool = False,
    ) -> None:
        super().__init__()
        self._service = service
        self._log_queue = log_queue
        self._remote = remote
        self._log_timer: Any = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="tab-mail"):
            with TabPane(self._service.t("tui.tab_mail"), id="tab-mail"):
                yield MailPane(self._service)
            with TabPane(self._service.t("tui.tab_actions"), id="tab-actions"):
                yield ActionsPane(self._service)
            if not self._remote:
                # mailbox forms and history browsing need the local service
                with TabPane(self._service.t("tui.tab_mailboxes"), id="tab-mailboxes"):
                    yield AccountsPane(self._service)
                with TabPane(self._service.t("tui.tab_llms"), id="tab-llms"):
                    yield LLMPane(self._service)
            with TabPane(self._service.t("tui.tab_runtime"), id="tab-runtime"):
                yield RuntimePane(self._service)
            with TabPane(self._service.t("tui.tab_logs"), id="tab-logs"):
                yield LogsPane(self._service, self._log_queue)
            if not self._remote:
                # marketplace installs run uv against the local environment
                with TabPane(self._service.t("tui.tab_market"), id="tab-market"):
                    yield MarketPane(self._service)
            with TabPane(self._service.t("tui.tab_bots"), id="tab-bots"):
                yield BotsPane(self._service)
            with TabPane(self._service.t("tui.tab_settings"), id="tab-settings"):
                yield SettingsPane(self._service)
        yield Footer()

    async def on_mount(self) -> None:
        self.title = self._service.t("tui.title")
        self.sub_title = f"v{self._service.snapshot().version}"
        self._log_timer = self.set_interval(1.0, self._drain_logs)
        self._refresh_lock = asyncio.Lock()
        self._service.on("mailflow.mail.processed", self._on_mail_processed)
        self._service.on("language.changed", self._on_language_changed)

    async def _on_language_changed(self, event: str, **payload: Any) -> None:
        # the service runs on the same loop as the app: schedule directly
        self._apply_language()

    def _apply_language(self) -> None:
        """Re-translate tab titles and pane labels after a language switch."""
        self.title = self._service.t("tui.title")
        tabs = self.query_one(TabbedContent)
        for pane_id, key in (
            ("tab-mail", "tui.tab_mail"),
            ("tab-mailboxes", "tui.tab_mailboxes"),
            ("tab-actions", "tui.tab_actions"),
            ("tab-llms", "tui.tab_llms"),
            ("tab-runtime", "tui.tab_runtime"),
            ("tab-logs", "tui.tab_logs"),
            ("tab-market", "tui.tab_market"),
            ("tab-settings", "tui.tab_settings"),
        ):
            tab = tabs.get_tab(pane_id)
            tab.label = self._service.t(key)  # pyright: ignore[reportUnknownMemberType]
        self.run_worker(cast(Any, self._relabel_guarded))

    async def _relabel_guarded(self) -> None:
        async with self._refresh_lock:
            await self._relabel_panes()

    async def _relabel_panes(self) -> None:
        for pane_type in (
            MailPane,
            AccountsPane,
            ActionsPane,
            LLMPane,
            RuntimePane,
            SettingsPane,
            MarketPane,
        ):
            query = self.query(pane_type)
            if not query:
                continue
            pane = query.first()
            # lazy panes compose with the current language on first activation;
            # only already-composed panes need relabeling here
            if not pane.is_mounted or not pane.query("*"):
                continue
            await pane.relabel()  # type: ignore[attr-defined]

    async def _on_mail_processed(self, event: str, **payload: Any) -> None:
        # the service runs on the same loop as the app: schedule directly
        self._schedule_reload()

    def _schedule_reload(self) -> None:
        self.run_worker(cast(Any, self._reload_guarded))

    async def _reload_guarded(self) -> None:
        async with self._refresh_lock:
            await self._reload_all()

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
