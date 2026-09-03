"""MailFlow Textual TUI: a client of the Core service, not another
implementation. Every screen reads service snapshots/data and calls service
methods; no business logic lives here."""

from __future__ import annotations

import asyncio
import contextlib
import queue as queue_module
import re
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar, cast

from mailflow.domain import ActionItem, MailRecord, ReplyDraft, Urgency
from mailflow.plugin_market import MarketPlugin, Repository
from mailflow.service import MailFlowService
from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.coordinate import Coordinate
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
from mailflow_tui.notifications import NotificationsPane
from mailflow_tui.repos import ReposScreen
from mailflow_tui.scaffold import PluginScaffoldScreen
from mailflow_tui.settings import AccountsPane, LLMPane, SettingsPane

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


def _event_select_value(event: Any) -> str:
    """Value of a Select.Changed event, as a plain string ('' on NULL)."""
    value = getattr(event.select, "value", Select.NULL)
    return "" if value is Select.NULL else str(value)


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
            yield Label(
                f"{self._t('tui.reply_to_label')}: {escape(self._record.mail.sender.display)}"
            )
            yield Label(
                f"{self._t('tui.reply_subject_label')}: "
                f"{self._service.t('tui.reply.subject_prefix')} {escape(self._record.mail.subject)}"
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
                self._set_status(self._t("tui.reply_saved"))
            except ValueError as exc:
                self._set_status(str(exc))
            return
        if button_id == "reply-prepare":
            # prepare calls the LLM (seconds to minutes with retries):
            # a worker keeps the modal painting while it runs
            draft_id = self._draft.draft_id
            self._set_status(self._t("tui.reply_preparing"))
            self.run_worker(
                self._prepare_reply(draft_id),
                exclusive=True,
                group="reply-prepare",
                exit_on_error=False,
            )
            return
        if button_id == "reply-confirm":
            assert self._draft is not None and self._draft.token is not None
            draft = self._draft
            self.query_one("#reply-confirm", Button).disabled = True
            self._set_status(self._t("tui.reply_sending"))
            self.run_worker(
                self._confirm_reply(draft),
                exclusive=True,
                group="reply-confirm",
                exit_on_error=False,
            )

    async def _prepare_reply(self, draft_id: str) -> None:
        try:
            self._draft = await self._service.prepare_reply(draft_id)
        except ValueError as exc:
            self._set_status(str(exc))
            return
        self._set_status(self._t("tui.reply_prepared", token=self._draft.token or ""))
        self.query_one("#reply-confirm", Button).disabled = False

    async def _confirm_reply(self, draft: ReplyDraft) -> None:
        token = draft.token
        if token is None:  # pragma: no cover - confirm is gated on prepare
            return
        try:
            await self._service.confirm_reply(draft.draft_id, token)
        except PermissionError as exc:
            self._set_status(str(exc))
            self.query_one("#reply-confirm", Button).disabled = False
            return
        self._set_status(self._t("tui.reply_sent"))


class ActionModal(ModalScreen[Any]):
    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss", "Close")]

    def __init__(self, service: MailFlowService, item: ActionItem) -> None:
        super().__init__()
        self._service = service
        self._item = item

    def compose(self) -> ComposeResult:
        item = self._item
        yield Static(self._service.t("tui.action_detail"), id="action-title")
        with ScrollableContainer(id="action-detail-scroll"):
            yield Label(f"{self._service.t('tui.action_time')}: {escape(item.time_range)}")
            yield Label(f"{self._service.t('tui.action_type')}: {escape(item.action_type)}")
            yield Label(f"{self._service.t('tui.action_content')}: {escape(item.summary)}")
            yield Label(f"{self._service.t('tui.action_notes')}: {escape(item.notes or '-')}")
            yield Label(f"{self._service.t('tui.action_source')}: {escape(item.mail_id)}")
            # the source mail's own details load asynchronously below
            yield Static("", id="action-mail-detail")
        # outside the scroll box: always visible, never scrolls away
        with Horizontal(id="action-footer"):
            yield Button(self._service.t("tui.btn_close"), id="action-close", variant="primary")

    async def on_mount(self) -> None:
        """Load the source mail: a todo without its original message context
        (subject, sender, body) forces the user to hunt through the mail tab."""
        record = await self._service.get_mail(self._item.mail_id)
        node = self.query_one_optional("#action-mail-detail", Static)
        if node is None:
            return
        if record is None:
            node.update(f"[dim]{self._service.t('tui.action_mail_missing')}[/dim]")
            return
        mail = record.mail
        from mailflow.domain import looks_binary

        body = mail.body_text.strip()
        if looks_binary(body):
            body = self._service.t("tui.detail_binary_body")
        elif not body and mail.body_html:
            body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", mail.body_html)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"[ \t\r\f\v]+", " ", body).strip()
        lines = [
            f"[bold]{self._service.t('tui.column_subject')}:[/bold] {escape(mail.subject)}",
            f"[bold]{self._service.t('tui.column_sender')}:[/bold] "
            f"{escape(mail.sender.display or mail.sender.address)}",
            f"[bold]{self._service.t('tui.column_date')}:[/bold] "
            f"{mail.date.strftime('%Y-%m-%d %H:%M')}",
        ]
        analysis_summary = record.analysis.summary if record.analysis else ""
        if analysis_summary:
            lines.append(
                f"[bold]{self._service.t('tui.detail_summary')}:[/bold] {escape(analysis_summary)}"
            )
        reason = record.analysis.reason if record.analysis else ""
        if reason:
            lines.append(
                f"[bold]{self._service.t('tui.detail_reason')}:[/bold] "
                f"{escape(self._service.display_text(reason))}"
            )
        lines.append(
            f"[bold]{self._service.t('tui.detail_body')}:[/bold] "
            f"{escape(body[:800] or '(no body)')}"
        )
        node.update("\n".join(lines))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "action-close":
            self.dismiss(None)


class MailPane(Vertical):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._records: list[MailRecord] = []
        self._selected_id: str | None = None
        # refresh_mail runs from several entry points (mount worker, filter
        # changes, reload-all); concurrent clear+add_row passes interleave
        # into DuplicateKeys — serialize them
        self._refresh_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        # the urgency Select auto-selects "auto" while mounting and fires
        # Changed; swallow that programmatic event (and the relabel restore)
        # so it never stamps an override onto the selected mail
        self._urgency_suppress = True
        yield Input(placeholder=self._service.t("tui.search_placeholder"), id="mail-search")
        yield Static("", id="mail-empty-hint")
        with Horizontal():
            yield DataTable(id="mail-table")
            with ScrollableContainer(id="mail-detail"):
                yield Static("", id="mail-summary")
                yield Static("", id="mail-reason")
                yield Static("", id="mail-actions")
                yield Static("", id="mail-body")
                yield Static("", id="mail-attachments")
                yield Static("", id="mail-notes")
        with Horizontal(id="mail-controls"):
            yield Select(self._urgency_options(), id="urgency-select", allow_blank=False)
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
            with Vertical(id="mail-actions-buttons"):
                with Horizontal(id="mail-actions-row1"):
                    yield Button(
                        self._service.t("tui.btn_refresh"), id="btn-refresh", variant="primary"
                    )
                    yield Button(self._service.t("tui.btn_trash"), id="btn-trash", variant="error")
                    yield Button(
                        self._service.t("tui.btn_ask_correct"),
                        id="btn-ask-correct",
                        variant="warning",
                    )
                yield Static("", id="mail-actions-spacer")
                with Horizontal(id="mail-actions-row2"):
                    yield Button(
                        self._service.t("tui.btn_reply"), id="btn-reply", variant="success"
                    )
                    yield Button(
                        self._service.t("tui.btn_reparse"), id="btn-reparse", variant="primary"
                    )
                    yield Button(
                        self._service.t("tui.btn_reparse_failed"),
                        id="btn-reparse-failed",
                        variant="error",
                    )

    async def on_mount(self) -> None:
        self._urgency_suppress = False
        # never block startup on storage reads + row rendering: the pane
        # paints empty and fills in as the exclusive worker delivers
        self.run_worker(
            self.refresh_mail(), exclusive=True, group="mail-refresh", exit_on_error=False
        )

    def _mail_table(self) -> DataTable[Any] | None:
        return self.query_one_optional("#mail-table", DataTable)  # pyright: ignore[reportUnknownVariableType]

    def _urgency_select(self) -> Select[Any] | None:
        return self.query_one_optional("#urgency-select", Select)  # pyright: ignore[reportUnknownVariableType]

    def _urgency_options(self) -> list[tuple[str, str]]:
        """Localized manual-override labels. "auto" comes FIRST: a Select
        with allow_blank=False auto-selects its first option on mount and
        fires Changed — routing that into the reset path is a harmless
        no-op, while any other level would stamp a bogus manual override
        onto the selected mail."""
        t = self._service.t
        return [
            (t("tui.urgency_opt_auto"), "auto"),
            (t("tui.urgency_opt_ad"), "ad"),
            (t("tui.urgency_opt_info"), "info"),
            (t("tui.urgency_opt_important"), "important"),
            (t("tui.urgency_opt_urgent"), "urgent"),
        ]

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
        self._urgency_suppress = True
        try:
            _apply_options(self, "#urgency-select", self._urgency_options())
        finally:
            self._urgency_suppress = False
        hint = self.query_one_optional("#urgency-hint", Static)
        if hint is not None:
            hint.update(self._service.t("tui.urgency_select_hint"))
        await self.refresh_mail()

    async def refresh_mail(self) -> None:
        async with self._refresh_lock:
            await self._refresh_mail_unlocked()

    async def _refresh_mail_unlocked(self) -> None:
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
        for position, record in enumerate(records, start=1):
            urgency = record.effective_urgency
            table.add_row(
                RichText(f"■ {urgency.value}", style=urgency.color),
                escape(record.mail.subject or self._service.t("tui.mail_no_subject")),
                escape(record.mail.sender.address),
                _localize(self._service, record.mail.received_at, "%m-%d %H:%M"),
                key=record.record_id,
            )
            if position % 50 == 0:
                # yield to the event loop so the UI stays interactive while
                # a large mailbox renders
                await asyncio.sleep(0)
        hint = self.query_one_optional("#mail-empty-hint", Static)
        if hint is not None:
            if not records:
                if self._records:
                    hint.update(self._service.t("tui.mail_no_match"))
                else:
                    hint.update(self._service.t("tui.mail_empty"))
                hint.display = "block"  # pyright: ignore[reportUnknownMemberType]
            else:
                hint.update(_BLANK)
                # an empty Static still occupies a row; hide it so the
                # search box and the mail table sit flush (the redundant
                # gap between them comes from this placeholder)
                hint.display = "none"  # pyright: ignore[reportUnknownMemberType]
        visible_ids = {record.record_id for record in records}
        if self._selected_id not in visible_ids:
            self._selected_id = records[0].record_id if records else None
        # keep the override dropdown in sync with the selected mail without
        # re-triggering the mutation handler
        selected = next((r for r in records if r.record_id == self._selected_id), None)
        if selected is not None:
            wanted = "auto" if selected.manual_urgency is None else selected.manual_urgency.value
            self._urgency_suppress = True
            try:
                select = self._urgency_select()
                if select is not None and str(select.value) != wanted:
                    select.value = wanted
            finally:
                self._urgency_suppress = False
        await self._show_selected()

    def _select_value(self, selector: str) -> str:
        select = _typed_select(self, selector if selector.startswith("#") else f"#{selector}")
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
        if self._selected_id is None:
            return
        record = await self._service.get_mail(self._selected_id)
        if record is not None:
            self._sync_urgency_select(record)
        await self._show_selected()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in ("mail-urgency-filter", "mail-sort"):
            await self.refresh_mail()
        elif (
            event.select.id == "urgency-select"
            and self._selected_id is not None
            and not getattr(self, "_urgency_suppress", False)
        ):
            value = event.value
            if value is Select.NULL or str(value) == "":
                # a blank sentinel means "back to automatic": reset the
                # override instead of constructing an invalid Urgency
                urgency: Urgency | None = None
            else:
                try:
                    urgency = Urgency(str(value))
                except ValueError:
                    return
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
            f"[bold]{service.t('tui.detail_summary')}:[/bold] "
            f"{escape(service.display_text(record.summary))}",
        )
        reason = record.analysis.reason if record.analysis else ""
        self._set_static(
            "#mail-reason",
            f"[bold]{service.t('tui.detail_reason')}:[/bold] "
            f"{escape(service.display_text(reason) or '-')}",
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
        from mailflow.domain import looks_binary

        body = record.mail.body_text.strip()
        if looks_binary(body):
            body = service.t("tui.detail_binary_body")
        elif not body and record.mail.body_html:
            # mails stored before the HTML-only fallback existed still carry
            # a readable html body — render it as text instead of "(no body)"
            body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", record.mail.body_html)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"[ \t\r\f\v]+", " ", body).strip()
        if len(body) > 4000:
            # a 200 KB dump into one Static is unusable and slow to render
            body = body[:4000] + "…"
        self._set_static(
            "#mail-body",
            f"[bold]{service.t('tui.detail_body')}:[/bold]\n{escape(body or '(no body)')}",
        )
        attachment_lines = [
            f"  {escape(a.filename)} ({a.content_type}, {a.size} B)"
            for a in record.mail.attachments
        ]
        if attachment_lines:
            self._set_static(
                "#mail-attachments",
                f"[bold]{service.t('tui.detail_attachments')}:[/bold]\n"
                + "\n".join(attachment_lines),
            )
        else:
            self._set_static("#mail-attachments", "")
        reply_flag = (
            f"\n[bold yellow]{service.t('tui.detail_reply_required', answer=service.t('common.yes'))}[/bold yellow]"
            if record.analysis and record.analysis.reply_required
            else ""
        )
        feedback = ""
        if not getattr(service, "remote", False):
            existing = await service.get_feedback(record.record_id)
            if existing:
                feedback = (
                    f"\n[bold yellow]{service.t('tui.feedback_marker')}: "
                    f"{escape(existing)}[/bold yellow]"
                )
        failed_notes = [note for note in record.processor_notes if note.status == "failed"]
        failure_text = ""
        if failed_notes:
            shown = "; ".join(f"{note.processor_id}: {note.message}" for note in failed_notes[:2])
            more = f" (+{len(failed_notes) - 2})" if len(failed_notes) > 2 else ""
            failure_text = (
                f"\n[red]{service.t('tui.detail_failed_note')}: {escape(shown + more)}[/red]"
            )
        self._set_static(
            "#mail-notes",
            f"{reply_flag}{feedback}{failure_text}\n{service.t('tui.urgency_label')}: "
            f"{record.effective_urgency.value} "
            f"({service.t('tui.detail_manual_marker') if record.manual_urgency is not None else service.t('tui.detail_auto_marker')})",
        )

    def _sync_urgency_select(self, record: MailRecord) -> None:
        """Mirror the selected mail's manual override into the dropdown
        (suppressed: a programmatic change is not a user mutation)."""
        wanted = "auto" if record.manual_urgency is None else record.manual_urgency.value
        self._urgency_suppress = True
        try:
            select = self._urgency_select()
            if select is not None and str(select.value) != wanted:
                select.value = wanted
        finally:
            self._urgency_suppress = False

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
        if button_id == "btn-ask-correct":
            record = await self._service.get_mail(self._selected_id)
            if record is None:
                return
            if getattr(self._service, "remote", False):
                self._set_static(
                    "#mail-notes",
                    f"[yellow]{self._service.t('tui.ask_correct_local_only')}[/yellow]",
                )
                return
            from mailflow_tui.ask_correct import AskCorrectModal

            cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                AskCorrectModal(self._service, record)
            )
            return
        if button_id == "btn-reparse":
            record = await self._service.get_mail(self._selected_id)
            if record is None:
                return
            self.run_worker(
                self._reparse_batch([record.mail]),
                exclusive=True,
                group="mail-reparse",
                exit_on_error=False,
            )
            return
        if button_id == "btn-reparse-failed":
            self.run_worker(
                self._reparse_failed(),
                exclusive=True,
                group="mail-reparse",
                exit_on_error=False,
            )
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

    async def _reparse_batch(self, mails: list[Any]) -> None:
        """Force re-analysis for the given messages, with per-mail progress."""
        status_node = self.query_one_optional("#mail-notes", Static)
        total = len(mails)
        done = 0
        failed: list[str] = []
        for position, mail in enumerate(mails, start=1):
            subject_short = escape((mail.subject or "")[:36])
            if status_node is not None:
                status_node.update(
                    f"[cyan]{self._service.t('tui.history_progress', position=position, total=total)} "
                    f"{subject_short}[/cyan]"
                )
            try:
                await self._service.process_mail(mail, force=True)
                done += 1
            except Exception as exc:
                failed.append(f"{mail.subject[:40]}: {exc}")
        await self.refresh_mail()
        if status_node is None:
            return
        if failed:
            detail = "; ".join(failed[:3])
            more = f" (+{len(failed) - 3})" if len(failed) > 3 else ""
            status_node.update(
                f"[red]{self._service.t('tui.history_failed', count=len(failed))}: "
                f"{escape(detail)}{more}[/red]"
            )
        else:
            status_node.update(
                f"[green]{self._service.t('tui.history_reanalyzed', count=done)}[/green]"
            )

    async def _reparse_failed(self) -> None:
        failed_records = await self._service.list_failed_mails()
        if not failed_records:
            self._set_static(
                "#mail-notes",
                f"[green]{self._service.t('tui.reparse_none_failed')}[/green]",
            )
            return
        await self._reparse_batch([record.mail for record in failed_records])

    def select_mail(self, mail_id: str) -> None:
        self._selected_id = mail_id
        self.call_after_refresh(lambda: self._show_selected())


class ActionsPane(Vertical):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._items: list[ActionItem] = []
        self._refresh_lock = asyncio.Lock()

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
        with Horizontal(id="actions-buttons"):
            yield Button(
                self._service.t("tui.btn_delete_todo"),
                id="actions-delete",
                variant="error",
            )
        yield Static(self._service.t("tui.empty"), id="actions-hint")

    async def on_mount(self) -> None:
        self.run_worker(
            self.refresh_actions(),
            exclusive=True,
            group="actions-refresh",
            exit_on_error=False,
        )

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
        async with self._refresh_lock:
            await self._refresh_actions_unlocked()

    async def _refresh_actions_unlocked(self) -> None:
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

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "actions-delete":
            return
        table = self._actions_table()
        if table is None:
            return
        row_index = table.cursor_row
        if row_index < 0 or row_index >= table.row_count:
            return
        from textual.coordinate import Coordinate

        row_key = table.coordinate_to_cell_key(Coordinate(row_index, 0)).row_key
        item = next((i for i in self._items if i.item_id == str(row_key.value)), None)
        if item is None:
            return
        await self._service.delete_action(item.item_id)
        await self.refresh_actions()


class RuntimePane(Vertical):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._selected_plugin: str | None = None
        self._detail_open_for: str | None = None
        self._refresh_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="runtime-scroll"):
            yield Static(self._service.t("tui.runtime_plugins"), id="runtime-plugins")
            yield _RuntimePluginTable(
                self._open_plugin_detail,
                id="runtime-plugins-table",
                cursor_type="row",
            )  # pyright: ignore[reportUnknownMemberType]
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
        self.run_worker(
            self.refresh_runtime(),
            exclusive=True,
            group="runtime-refresh",
            exit_on_error=False,
        )

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
        async with self._refresh_lock:
            await self._refresh_runtime_unlocked()

    async def _refresh_runtime_unlocked(self) -> None:
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

    def _open_plugin_detail(self, plugin_id: str) -> None:
        """Open the plugin's detail dialog instantly.

        The dialog renders its content in a worker from the app-wide
        plugin-entry cache — the same entries the Market tab uses — so the
        Runtime and Market details are always identical (readme +
        translations). No network and no import happens on the click path.
        """
        if getattr(self, "_detail_open_for", None) == plugin_id:
            return  # the dialog for this plugin is already up
        self._detail_open_for = plugin_id
        plugin = cast(MailFlowApp, self.app).plugin_entry(plugin_id)  # pyright: ignore[reportUnknownMemberType]
        if plugin is None:
            plugin = self._local_market_plugin(plugin_id)
        if plugin is None:
            self.query_one("#runtime-status", Static).update(
                f"[yellow]{self._service.t('plugin.unknown_plugin', plugin_id=plugin_id)}[/yellow]"
            )
            return
        cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
            MarketDetailScreen(self._service, plugin),
            callback=lambda _result: self._detail_closed(plugin_id),
        )

    def _detail_closed(self, plugin_id: str) -> None:
        """Release the double-open guard when the detail dialog closes."""
        if self._detail_open_for == plugin_id:
            self._detail_open_for = None

    def _local_market_plugin(self, plugin_id: str) -> MarketPlugin | None:
        """App-wide local entry builder (shared with the Market tab)."""
        return cast(MailFlowApp, self.app).local_plugin_entry(plugin_id)  # pyright: ignore[reportUnknownMemberType]

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._selected_plugin is None:
            return
        button_id = event.button.id
        if button_id is None:
            return
        # enable/disable rebuilds the runtime (bounded), uninstall runs pip:
        # neither belongs on the UI handler
        status = self.query_one("#runtime-status", Static)
        status.update(self._service.t("tui.loading"))
        self.run_worker(
            self._apply_plugin_action(button_id, self._selected_plugin),
            exclusive=True,
            group="runtime-plugin-action",
            exit_on_error=False,
        )

    async def _apply_plugin_action(self, button_id: str, plugin_id: str) -> None:
        status = self.query_one("#runtime-status", Static)
        message = ""
        try:
            if button_id == "runtime-plugin-disable":
                await self._service.plugin_disable(plugin_id)
                message = (
                    f"{self._service.t('plugin.disabled_ok', plugin_id=plugin_id)}"
                    + f"\n({self._service.t('plugin.applies_now')})"
                )
            elif button_id == "runtime-plugin-enable":
                created_instance = await self._service.plugin_enable(plugin_id)
                enabled = self._service.t("plugin.enabled_ok", plugin_id=plugin_id)
                if created_instance:
                    enabled += "\n" + self._service.t(
                        "plugin.instance_created", notifier_id=created_instance
                    )
                message = f"{enabled}\n({self._service.t('plugin.applies_now')})"
            elif button_id == "runtime-plugin-uninstall":
                output_text = await self._service.plugin_uninstall(plugin_id)
                message = (
                    f"{self._service.t('plugin.uninstalled_ok', plugin_id=plugin_id)}"
                    + f" ({self._service.t('plugin.restart_note')})"
                    + (f"\n{output_text}" if output_text else "")
                )
            else:
                return
        except Exception as exc:  # uv failures surface here too
            status.update(f"[red]{exc}[/red]")
            return
        status.update(message)
        await self.refresh_runtime()


class LogsPane(Vertical):
    """Filterable log viewer.

    Buffers the last N formatted log lines and re-renders them through
    three filters: a minimum level (WARNING+ERROR by default, expandable
    to INFO or DEBUG), a source group (empty = all), and a substring
    search. The buffer is bounded so the pane never grows without limit.
    """

    _LEVEL_STYLES: ClassVar[dict[str, str]] = {
        "ERROR": "bold red",
        "CRITICAL": "bold red",
        "WARNING": "yellow",
        "INFO": "dim",
        "DEBUG": "dim",
    }
    _LEVEL_RANK: ClassVar[dict[str, int]] = {
        "DEBUG": 10,
        "INFO": 20,
        "WARNING": 30,
        "ERROR": 40,
        "CRITICAL": 50,
    }
    _DEFAULT_MIN_LEVEL = "WARNING"
    _MAX_LINES = 2000

    def __init__(self, service: MailFlowService, log_queue: queue_module.Queue[Any]) -> None:
        super().__init__()
        self._service = service
        self._log_queue = log_queue
        self._buffer: deque[str] = deque(maxlen=self._MAX_LINES)
        self._min_level = self._DEFAULT_MIN_LEVEL
        self._source = ""
        self._query = ""
        self._seen_sources: set[str] = set()
        # incremental rendering: newly pulled lines are appended to the
        # RichLog (capped by its own max_lines) instead of re-rendering the
        # whole 2000-line buffer every second — on slow terminals full
        # rebuilds stall the event loop. A full rebuild happens only when a
        # filter changes or the pane (re)mounts.

    def compose(self) -> ComposeResult:
        with Horizontal(id="log-controls"):
            yield Select(
                [
                    (self._service.t("tui.logs_level_warning"), "WARNING"),
                    (self._service.t("tui.logs_level_info"), "INFO"),
                    (self._service.t("tui.logs_level_debug"), "DEBUG"),
                ],
                value=self._min_level,
                id="log-level",
                allow_blank=False,
            )
            yield Select(
                [(self._service.t("tui.logs_all_sources"), "")],
                id="log-source",
                allow_blank=False,
            )
            yield Input(
                placeholder=self._service.t("tui.logs_search_placeholder"),
                id="log-search",
            )
        with ScrollableContainer(id="log-scroll"):
            yield RichLog(id="log-view", wrap=True, highlight=True, max_lines=2000)

    async def on_mount(self) -> None:
        self._render_logs()

    def relabel(self) -> None:
        # language switches refresh the control labels without clearing
        # received log lines
        level = _typed_select(self, "#log-level")
        if level is not None:
            self._set_level_options(level)
        source = _typed_select(self, "#log-source")
        if source is not None:
            self._refresh_source_options()
        search = self.query_one_optional("#log-search", Input)
        if search is not None:
            search.placeholder = self._service.t("tui.logs_search_placeholder")
        self._render_logs()

    def _set_level_options(self, level: Select[str]) -> None:
        current = level.value
        level.set_options(
            [
                (self._service.t("tui.logs_level_warning"), "WARNING"),
                (self._service.t("tui.logs_level_info"), "INFO"),
                (self._service.t("tui.logs_level_debug"), "DEBUG"),
            ]
        )
        level.value = current

    def _refresh_source_options(self) -> None:
        source = _typed_select(self, "#log-source")
        if source is None:
            return
        current = source.value
        pairs = [(self._service.t("tui.logs_all_sources"), "")] + [
            (name, name) for name in sorted(self._seen_sources)
        ]
        # drain() runs every second; poking Select.set_options with an
        # identical list re-renders the widget each tick — skip it
        signature = repr(pairs)
        if getattr(self, "_source_signature", None) != signature:
            source.set_options(pairs)  # pyright: ignore[reportUnknownMemberType]
            self._source_signature = signature
        if current is not Select.NULL and current != "":
            source.value = current if current in self._seen_sources else ""

    def drain(self) -> None:
        # remounts (language switch, tab re-compose) can tick the interval
        # while the widget tree is detached — never crash the app for it
        pulled: list[str] = []
        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue_module.Empty:
                break
            self._buffer.append(line)
            pulled.append(line)
            parts = line.split("|", 3)
            if len(parts) == 4:
                self._seen_sources.add(parts[2])
        if not pulled:
            return
        self._refresh_source_options()
        self._append_new_lines(pulled)

    def _render_logs(self) -> None:
        log_view = self.query_one_optional("#log-view", RichLog)
        if log_view is None:
            return
        log_view.clear()
        for line in self._buffer:
            text = self._line_to_text(line)
            if text is None:
                continue
            log_view.write(text)

    def _line_to_text(self, line: str) -> Any:
        """Format one buffered line under the current filters; '' means the
        line is filtered out (or is a non-log line that passes through)."""
        parts = line.split("|", 3)
        if len(parts) != 4:
            return line
        time_part, level, logger_name, message = parts
        if self._LEVEL_RANK.get(level, 0) < self._LEVEL_RANK.get(
            self._min_level, self._LEVEL_RANK[self._DEFAULT_MIN_LEVEL]
        ):
            return None
        if self._source and logger_name != self._source:
            return None
        if self._query and self._query.lower() not in message.lower():
            return None
        from rich.text import Text

        text = Text(f"{time_part} ")
        text.append(f"{level:<7}", style=self._LEVEL_STYLES.get(level, "bold"))
        text.append(f" {logger_name} ")
        text.append(message)
        return text

    def _append_new_lines(self, new_lines: list[str]) -> None:
        """Incremental path: write only rows added since the last drain."""
        log_view = self.query_one_optional("#log-view", RichLog)
        if log_view is None:
            return  # detached: on remount, _render_logs rebuilds cleanly
        for line in new_lines:
            text = self._line_to_text(line)
            if text is None:
                continue
            log_view.write(text)

    def on_select_changed(self, event: Any) -> None:
        if event.select.id == "log-level":
            self._min_level = _event_select_value(event)
            self._render_logs()
        elif event.select.id == "log-source":
            self._source = _event_select_value(event)
            self._render_logs()

    def on_input_changed(self, event: Any) -> None:
        if event.input.id == "log-search":
            self._query = str(event.input.value)
            self._render_logs()


def plugin_doc_readme(info: Any) -> str:
    """Turn a plugin's module docstring into the market detail readme.

    The real documentation lives in ``<package>.plugin`` (the module that
    registers the components), not in the package ``__init__`` whose
    docstring is a one-line description — import the submodule first and
    fall back to the package docstring only when it is missing.
    """
    package = info.plugin_id.replace("-", "_")
    doc = ""
    for candidate in (f"{package}.plugin", package):
        try:
            module = __import__(candidate, fromlist=["plugin"])
        except Exception:
            continue
        doc = (module.__doc__ or "").strip()
        if doc:
            break
    if not doc:
        return ""
    lines: list[str] = []
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.startswith("Options:"):
            lines.append("")
            lines.append("## Options")
            continue
        if stripped.startswith(("Component id", "Component id:")):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


class _RuntimePluginTable(DataTable[Any]):
    """DataTable whose double-click opens the plugin detail.

    Textual's DataTable stops Click events while handling them, so the
    double-click never reaches the pane; MouseDown, however, is delivered
    before Click synthesis and is not stopped — recognize the second
    MouseDown of the chain here and call back with the row key."""

    def __init__(self, on_double_click: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._on_double_click = on_double_click

    async def on_mouse_down(self, event: Any) -> None:
        # The second MouseDown of a double-click is delivered immediately —
        # waiting for Click chain synthesis costs the double-click timeout
        # (~0.5s). Fire on the second press: instant dialog.
        await super()._on_mouse_down(event)  # pyright: ignore[reportAttributeAccessIssue]
        now = time.monotonic()
        style = getattr(event, "style", None)
        meta = dict(style.meta or {}) if style is not None else {}
        row_index = meta.get("row")
        column_index = meta.get("column", 0) or 0
        if not isinstance(row_index, int) or row_index < 0 or row_index >= self.row_count:
            self._last_press = (0.0, -1)
            return
        last_time, last_row = getattr(self, "_last_press", (0.0, -1))  # pyright: ignore[reportUnknownVariableType, reportUnknownVariableType]
        self._last_press = (now, row_index)
        if now - last_time <= 0.5 and last_row == row_index:
            self._last_press = (0.0, -1)
            key = self.coordinate_to_cell_key(Coordinate(row_index, column_index)).row_key
            self._on_double_click(str(key.value))


class MarketDetailScreen(ModalScreen[Any]):
    """VS Code-style full-screen plugin detail: metadata, markdown readme
    and install/uninstall/enable/disable actions."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss", "Close")]

    def __init__(self, service: MailFlowService, plugin: MarketPlugin) -> None:
        super().__init__()
        self._service = service
        self._plugin = plugin
        self._content_loaded = False

    def compose(self) -> ComposeResult:
        # skeleton first: the dialog opens instantly, the readme renders in
        # a worker (on_mount) so a large/missing readme never blocks the
        # double-click response
        with Vertical(id="market-detail-dialog"):
            yield Static(self._service.t("tui.market_detail"), id="market-detail-title")
            with ScrollableContainer(id="market-detail-scroll"):
                yield Markdown(self._t("tui.loading"), id="market-detail-readme")
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

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    async def on_mount(self) -> None:
        # build the readme off the UI thread: docstring import + markdown
        # assembly can be slow for bundled plugins without a market entry
        self.run_worker(
            self._load_content(), exclusive=True, group="detail-load", exit_on_error=False
        )

    async def _load_content(self) -> None:
        plugin = self._plugin
        language = self._service.i18n.language
        # the app-wide cache may have been populated after this dialog was
        # pushed (preload finished, market fetched): re-resolve the readme
        # from the shared entry so both tabs always render the identical,
        # best-available (translated) content
        app = cast(MailFlowApp, self.app)  # pyright: ignore[reportUnknownMemberType]
        with contextlib.suppress(Exception):
            cached = app.plugin_entry(plugin.id)  # pyright: ignore[reportUnknownMemberType]
            if cached is not None and cached.readme_for(language):
                readme = await asyncio.to_thread(self._readme_for, cached, language)
                meta = (
                    f"**{cached.name or cached.id}** v{cached.version} — `{cached.id}`\n\n"
                    f"{self._service.t('plugin.field_author')}: {cached.author or '-'} · "
                    f"{self._service.t('plugin.field_updated')}: {cached.updated or '-'}\n"
                    f"{self._service.t('plugin.field_homepage')}: {cached.homepage or '-'}\n"
                )
                node = self.query_one_optional("#market-detail-readme", Markdown)
                if node is not None:
                    node.update(meta + "\n---\n\n" + readme)
                self._content_loaded = True
                return
        readme = await asyncio.to_thread(self._readme_for, plugin, language)
        meta = (
            f"**{plugin.name or plugin.id}** v{plugin.version} — `{plugin.id}`\n\n"
            f"{self._service.t('plugin.field_author')}: {plugin.author or '-'} · "
            f"{self._service.t('plugin.field_updated')}: {plugin.updated or '-'}\n"
            f"{self._service.t('plugin.field_homepage')}: {plugin.homepage or '-'}\n"
        )
        node = self.query_one_optional("#market-detail-readme", Markdown)
        if node is not None:
            node.update(meta + "\n---\n\n" + readme)
        self._content_loaded = True

    @staticmethod
    def _readme_for(plugin: MarketPlugin, language: str) -> str:
        """Localized readme for the detail dialog.

        Priority: market readme (may carry translations) > the plugin
        module's docstring (full local documentation) > the one-line
        description. The docstring import runs here, in the worker, never
        on the click path."""
        readme = plugin.readme_for(language)
        if readme:
            return readme
        if plugin.source == "local":
            from mailflow.plugins import PluginInfo

            info = PluginInfo(
                plugin_id=plugin.id,
                name=plugin.name,
                version=plugin.version,
                description=plugin.description,
            )
            doc = plugin_doc_readme(info)
            if doc:
                return f"# {plugin.name or plugin.id}\n\n{doc}"
        description = plugin.description_for(language)
        if description:
            return f"# {plugin.name or plugin.id}\n\n{description}"
        return ""

    def _set_status(self, text: str) -> None:
        status = self.query_one_optional("#market-detail-status", Static)
        if status is not None:
            status.update(text)

    async def on_markdown_link_clicked(self, event: Any) -> None:
        href = str(getattr(event, "href", "") or "")
        if not href:
            return
        result = self._service.open_url(href)
        link = self.query_one_optional("#market-detail-link", Static)
        if link is not None:
            link.update(f"[cyan]{escape(result)}[/cyan]")

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
        self._detail_open_for: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="market-controls"):
            with Horizontal(id="market-controls-top"):
                yield Input(
                    placeholder=self._service.t("tui.market_search_placeholder"),
                    id="market-search",
                )
                yield Select([], id="market-category")
                yield Select(
                    [
                        (self._service.t("tui.market_sort_name"), "name"),
                        (self._service.t("tui.market_sort_status"), "status"),
                        (self._service.t("tui.market_sort_category"), "category"),
                        (self._service.t("tui.market_sort_installed"), "installed"),
                        (self._service.t("tui.market_sort_enabled"), "enabled"),
                        (self._service.t("tui.market_sort_not_installed"), "not-installed"),
                    ],
                    id="market-sort",
                    allow_blank=False,
                )
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
            with ScrollableContainer(id="market-detail-scroll"):
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

    def _select_value(self, selector: str) -> str:
        select = _typed_select(self, selector)
        if select is not None and select.value is not Select.NULL:
            return str(select.value)
        return "name"

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

    def _category_label(self, category: str) -> str:
        """Localized label for a known plugin category id; unknown ids stay
        as-is rather than guessing."""
        translated = self._service.t(f"tui.market_category_{category}")
        if translated != f"tui.market_category_{category}":
            return translated
        return category

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
        # locally installed plugins (bundled + local folders) are market
        # entries too: they get a detail view with the plugin's own
        # documentation, so providers can ship config docs
        local_repo = Repository("local", "")
        local_entries: list[tuple[Repository, MarketPlugin]] = []
        seen = {plugin.id for _repo, plugin in entries}
        for info in self._service.plugin_manager.enabled_infos():
            if info.plugin_id in seen:
                continue
            local = cast(MailFlowApp, self.app).local_plugin_entry(info.plugin_id)  # pyright: ignore[reportUnknownMemberType]
            if local is not None:
                # fill the docstring here (this whole method runs in the
                # market-fetch worker): the market pane's inline preview
                # needs it synchronously, and the shared app cache then
                # carries it to the Runtime tab as well
                local.readme = plugin_doc_readme(info)
                local_entries.append((local_repo, local))
        self._entries = [*entries, *local_entries]
        self._installed = {}  # re-derive install state for the new metadata
        self._loading = False
        # share with the Runtime tab: both tabs now resolve the detail from
        # the same entries (market readme + translations for remote plugins,
        # docstring for local-only ones); persist them so a later session
        # keeps the translations without a fresh network fetch
        cast(MailFlowApp, self.app).set_plugin_entries(self._entries)  # pyright: ignore[reportUnknownMemberType]
        await self._service.market_cache_save(self._entries)
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
        sort_mode = self._select_value("#market-sort")
        ordered = list(self._entries)
        if sort_mode == "name":
            ordered.sort(key=lambda item: (item[1].name or item[1].id).lower())
        elif sort_mode == "category":
            ordered.sort(key=lambda item: sorted(item[1].categories)[:1])
        elif sort_mode == "installed":
            ordered.sort(
                key=lambda item: (
                    not self._market_status_of(item[1]).startswith(
                        self._service.t("plugin.installed")
                    ),
                    (item[1].name or item[1].id).lower(),
                )
            )
        elif sort_mode == "enabled":
            ordered.sort(
                key=lambda item: (
                    self._market_status_of(item[1]) != self._service.t("tui.plugin_status_enabled"),
                    (item[1].name or item[1].id).lower(),
                )
            )
        elif sort_mode == "not-installed":
            ordered.sort(
                key=lambda item: (
                    self._market_status_of(item[1]).startswith(self._service.t("plugin.installed")),
                    (item[1].name or item[1].id).lower(),
                )
            )
        for _repo, plugin in ordered:
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
                select.set_options(  # pyright: ignore[reportUnknownMemberType]
                    [("all", "all"), *[(self._category_label(c), c) for c in categories]]
                )
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
        if plugin is None:
            return
        if self._detail_open_for == plugin.id:
            return  # a double-click fires RowSelected twice: one dialog
        self._detail_open_for = plugin.id
        self._show_detail(plugin)
        cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
            MarketDetailScreen(self._service, plugin),
            callback=lambda _result: self._detail_closed(plugin.id),
        )

    def _detail_closed(self, plugin_id: str) -> None:
        """Release the double-open guard when the detail dialog closes."""
        if self._detail_open_for == plugin_id:
            self._detail_open_for = None

    async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        plugin = next((p for _r, p in self._entries if p.id == event.row_key.value), None)
        if plugin is not None and plugin is not self._selected:
            self._show_detail(plugin)

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in ("market-category", "market-sort"):
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
        if button_id is None:
            return
        plugin = self._selected
        # pip operations and runtime rebuilds take seconds: run them in
        # an exclusive worker so the market stays interactive
        self.run_worker(
            self._apply_plugin_action(button_id, plugin),
            exclusive=True,
            group="market-action",
            exit_on_error=False,
        )

    async def _apply_plugin_action(self, button_id: str, plugin: MarketPlugin) -> None:
        market = self._service.market
        status_node = self.query_one("#market-status", Static)
        try:
            if button_id == "market-install":
                if market.is_installed(plugin.id, package=plugin.package):
                    status_node.update(
                        self._service.t("plugin.already_installed", plugin_id=plugin.id)
                    )
                    return
                status_node.update(self._service.t("tui.loading"))
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
            status_node.update(str(exc))
            return
        self._installed.pop(plugin.id, None)  # install state changed
        self._render_entries()
        status_node.update(
            self._service.t(message_key, plugin_id=plugin.id)
            + f" ({self._service.t('plugin.restart_note')})"
        )

    async def service_uninstall(self, plugin: MarketPlugin) -> str:
        if not plugin.package:
            raise ValueError(f"plugin {plugin.id!r} has no pip package to uninstall")
        return await self._service.market.uninstall(plugin)


async def pilot_pause_if_possible(app: Any, seconds: float = 0.05) -> None:
    """Yield control so remounted widgets finish composing; a no-op when the
    app is not currently running its test/message pump."""
    await asyncio.sleep(seconds)


class MailFlowApp(App[None]):
    """Eight-tab administration UI."""

    CSS_PATH = "app.tcss"
    BINDINGS: ClassVar[list[Any]] = [
        Binding("ctrl+q", "quit", "Quit"),
        # tab switching: ctrl+number jumps straight to a tab (labels are
        # localized, ids are stable; missing tabs — remote mode hides some —
        # are skipped in the action)
        # quoted ids: Textual parses action args with ast.literal_eval, so a
        # bare ``tab-mail`` would be evaluated as ``tab - mail`` and fail
        Binding("ctrl+1", "goto_tab('tab-mail')", "Mail", show=False),
        Binding("ctrl+2", "goto_tab('tab-actions')", "Actions", show=False),
        Binding("ctrl+3", "goto_tab('tab-mailboxes')", "Mailboxes", show=False),
        Binding("ctrl+4", "goto_tab('tab-llms')", "LLMs", show=False),
        Binding("ctrl+5", "goto_tab('tab-runtime')", "Runtime", show=False),
        Binding("ctrl+6", "goto_tab('tab-market')", "Market", show=False),
        Binding("ctrl+7", "goto_tab('tab-notifications')", "Notifications", show=False),
        Binding("ctrl+8", "goto_tab('tab-settings')", "Settings", show=False),
        Binding("ctrl+9", "goto_tab('tab-logs')", "Logs", show=False),
    ]

    def action_goto_tab(self, tab_id: str) -> None:
        """Switch to a tab by its stable id; silently ignore hidden tabs
        (remote mode omits mailboxes/llms/market)."""
        try:
            tabs = self.query_one(TabbedContent)
            tabs.get_tab(tab_id)  # pyright: ignore[reportUnknownMemberType]
            tabs.active = tab_id  # pyright: ignore[reportUnknownMemberType]
        except Exception:
            pass

    TITLE = "MailFlow"

    def __init__(
        self,
        service: Any,  # MailFlowService | remote.RemoteServiceAdapter
        log_queue: queue_module.Queue[Any],
        *,
        remote: bool = False,
        splash: bool = False,
    ) -> None:
        super().__init__()
        self._service = service
        self._log_queue = log_queue
        self._remote = remote
        self._splash = splash
        self._log_timer: Any = None
        # shared plugin-entry cache: the single source of truth for the
        # detail dialog. MarketPane writes its fetched entries here, and
        # the Runtime tab reads from here, so both tabs show the exact
        # same detail (same readme, same translations).
        self._plugin_entries: dict[str, MarketPlugin] = {}

    def set_plugin_entries(self, entries: list[tuple[Repository, MarketPlugin]]) -> None:
        """Record the market entries (called by MarketPane after a fetch)."""
        for _repo, plugin in entries:
            self._plugin_entries[plugin.id] = plugin

    def plugin_entry(self, plugin_id: str) -> MarketPlugin | None:
        """The shared entry for a plugin id ('' when unknown)."""
        return self._plugin_entries.get(plugin_id)

    def local_plugin_entry(self, plugin_id: str) -> MarketPlugin | None:
        """A market entry for a locally loaded plugin (bundled or installed).

        The readme stays empty: importing the plugin module and rendering
        its docstring is deferred to the detail dialog's worker, so neither
        tab's click path ever blocks on it."""
        for info in self._service.plugin_manager.enabled_infos():
            if info.plugin_id != plugin_id:
                continue
            return MarketPlugin(
                id=info.plugin_id,
                name=info.name or info.plugin_id,
                version=info.version,
                description=info.description,
                categories=[k.value for k in info.kinds],
                package=info.plugin_id,
                source="local",
                readme="",
            )
        return None

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
            if not self._remote:
                # marketplace installs run uv against the local environment
                with TabPane(self._service.t("tui.tab_market"), id="tab-market"):
                    yield MarketPane(self._service)
            with TabPane(self._service.t("tui.tab_notifications"), id="tab-notifications"):
                yield NotificationsPane(self._service)
            with TabPane(self._service.t("tui.tab_settings"), id="tab-settings"):
                yield SettingsPane(self._service)
            with TabPane(self._service.t("tui.tab_logs"), id="tab-logs"):
                yield LogsPane(self._service, self._log_queue)
        yield Footer()

    async def on_mount(self) -> None:
        self.title = self._service.t("tui.title")
        self.sub_title = self._version_string()
        self._log_timer = self.set_interval(1.0, self._drain_logs)
        # preload the persisted marketplace cache so the Runtime tab's
        # plugin detail keeps its translations before the first fetch
        self.run_worker(
            self._preload_market_cache(), exclusive=True, group="market-cache", exit_on_error=False
        )
        # eagerly fetch the marketplace in the background too: the very
        # first session gets translations for the Runtime detail without
        # having to open the Market tab first
        self.run_worker(
            self._eager_market_fetch(), exclusive=True, group="market-eager", exit_on_error=False
        )
        self._refresh_lock = asyncio.Lock()
        self._service.on("mailflow.mail.processed", self._on_mail_processed)
        self._service.on("language.changed", self._on_language_changed)
        if self._splash:
            from mailflow_tui.splash import SplashScreen

            self.push_screen(SplashScreen(self._service.t, self._version_string()))  # pyright: ignore[reportUnknownMemberType]

    def _version_string(self) -> str:
        """App version for the title bar; remote adapter snapshots are
        plain dicts (``snapshot_sync``) while the local service returns a
        typed :class:`RuntimeSnapshot`."""
        if self._remote:
            return f"v{self._service.snapshot_sync().get('version', '')}"
        return f"v{self._service.snapshot().version}"

    async def _preload_market_cache(self) -> None:
        """Load persisted marketplace entries into the shared cache."""
        try:
            entries = await self._service.market_cache_load()
        except Exception:
            entries = []
        if entries:
            self.set_plugin_entries(
                [(Repository("cache", ""), cast("MarketPlugin", entry)) for entry in entries]  # pyright: ignore[reportUnknownVariableType]
            )

    async def _eager_market_fetch(self) -> None:
        """Background marketplace fetch at startup: fills the shared cache
        (and the persisted cache) so the Runtime tab's plugin detail shows
        translated readmes without the user opening the Market tab."""
        try:
            entries = await asyncio.to_thread(self._service.market.list_plugins)
        except Exception:
            return
        if entries:
            self.set_plugin_entries(entries)
            with contextlib.suppress(Exception):
                await self._service.market_cache_save(entries)

    async def _on_language_changed(self, event: str, **payload: Any) -> None:
        # the service runs on the same loop as the app: schedule directly
        self._apply_language()

    def _pane_factories(self) -> dict[str, tuple[Callable[[Any], Any], str]]:
        return {
            "tab-mail": (MailPane, "tui.tab_mail"),
            "tab-mailboxes": (AccountsPane, "tui.tab_mailboxes"),
            "tab-actions": (ActionsPane, "tui.tab_actions"),
            "tab-llms": (LLMPane, "tui.tab_llms"),
            "tab-runtime": (RuntimePane, "tui.tab_runtime"),
            "tab-notifications": (NotificationsPane, "tui.tab_notifications"),
            "tab-market": (MarketPane, "tui.tab_market"),
            "tab-settings": (SettingsPane, "tui.tab_settings"),
        }

    def _tab_label_keys(self) -> dict[str, str]:
        """Every tab id → its label key, including tabs that are never
        remounted (logs survive language switches to keep their history)."""
        keys = {pane_id: key for pane_id, (_f, key) in self._pane_factories().items()}
        keys["tab-logs"] = "tui.tab_logs"
        return keys

    def _apply_language(self) -> None:
        """Re-translate tab titles and remount every composed pane.

        Remounting (instead of patching individual labels) guarantees the
        whole UI — buttons, placeholders, table headers, static titles —
        switches to the new language at once."""
        self.title = self._service.t("tui.title")
        tabs = self.query_one(TabbedContent)
        for pane_id, key in self._tab_label_keys().items():
            try:
                tab = tabs.get_tab(pane_id)
            except Exception:
                continue  # remote mode hides some tabs entirely
            tab.label = self._service.t(key)  # pyright: ignore[reportUnknownMemberType]
        self.run_worker(cast(Any, self._remount_guarded))

    async def _remount_guarded(self) -> None:
        async with self._refresh_lock:
            await self._remount_panes()

    async def _remount_panes(self) -> None:
        factories = self._pane_factories()
        for pane_id, (factory, _key) in factories.items():
            panes = self.query(f"#{pane_id}")
            if not panes:
                continue  # lazy: composes with current language on activation
            container = panes.first()
            if not container.is_mounted or not container.children:
                continue
            await container.remove_children()
            await container.mount(factory(self._service))
        # panes that survive remounting still need their statics retranslated
        for pane in self.query(LogsPane):
            pane.relabel()
        await pilot_pause_if_possible(self)

    async def _on_mail_processed(self, event: str, **payload: Any) -> None:
        # the service runs on the same loop as the app: schedule directly
        self._schedule_reload()

    def _schedule_reload(self) -> None:
        # a bulk re-analysis emits one event per mail: coalesce the burst
        # into a single refresh instead of queueing a full reload per mail
        if getattr(self, "_reload_scheduled", False):
            return
        self._reload_scheduled = True

        def _run() -> None:
            self._reload_scheduled = False
            self.run_worker(cast(Any, self._reload_guarded))

        self.call_after_refresh(_run)

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
