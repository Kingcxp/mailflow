"""MailFlow Textual TUI: a client of the Core service, not another
implementation. Every screen reads service snapshots/data and calls service
methods; no business logic lives here."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue as queue_module
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast
from zoneinfo import ZoneInfo

from mailflow.domain import (
    ActionItem,
    MailRecord,
    ReplyDraft,
    ReplyState,
    Urgency,
    looks_binary,
    to_utc,
)
from mailflow.errors import error_text
from mailflow.mail_body import mail_body_text
from mailflow.mail_query import MailQuery
from mailflow.plugin_market import MarketPlugin, Repository
from mailflow.service import MailFlowService
from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingsMap
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.coordinate import Coordinate
from textual.css.query import QueryError
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widget import MountError
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    Markdown,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from mailflow_tui.assistant import AssistantPane
from mailflow_tui.confirm import ConfirmModal
from mailflow_tui.export import BotExportScreen
from mailflow_tui.install import InstallScreen
from mailflow_tui.labels import (
    error_detail,
    error_message,
    mail_status,
    remote_connection_status,
    urgency_label,
)
from mailflow_tui.labels import relabel_buttons as _relabel_buttons
from mailflow_tui.mail_workflows import (
    AttachmentsScreen,
    DraftsScreen,
    MailFilter,
    MailMenu,
    MailReader,
    TaskMenu,
)
from mailflow_tui.notifications import NotificationsPane
from mailflow_tui.profile import UserProfileModal
from mailflow_tui.repos import ReposScreen
from mailflow_tui.scaffold import PluginScaffoldScreen
from mailflow_tui.search import SearchMatcher, build_search_matcher
from mailflow_tui.settings import AccountsPane, LLMPane, SettingsPane
from mailflow_tui.todo_create import TodoCreateModal, TodoEditModal
from mailflow_tui.view_state import TableRowEvent, TableView
from mailflow_tui.widgets import (
    Input,
    SafeSelect,
    SearchToggle,
    SelectionTable,
    ShortcutHelp,
    TextArea,
)

if TYPE_CHECKING:
    from mailflow_tui.sessions import SessionHost

logger = logging.getLogger("mailflow.tui")

_BLANK = ""


def _load_error(service: MailFlowService, exc: Exception) -> str:
    message = service.t("tui.data_load_failed", error=error_detail(service, exc))
    return f"[red]{escape(message)}[/red]"


def _localize(service: MailFlowService, value: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    from zoneinfo import ZoneInfo

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(service.config.general.timezone)).strftime(fmt)


_ACTION_TYPE_KEYS: dict[str, str] = {
    "errand": "tui.action_type_errand",
    "exam": "tui.action_type_exam",
    "meeting": "tui.action_type_meeting",
    "other": "tui.action_type_other",
}


def _action_type_label(service: MailFlowService, action_type: str) -> str:
    """Translate built-in action types without rewriting free-form data."""
    key = _ACTION_TYPE_KEYS.get(action_type)
    return service.t(key) if key else action_type


def _action_time_range(service: MailFlowService, item: ActionItem) -> str:
    """Show an action's UTC storage times in the configured local timezone."""
    return item.time_range_in(service.config.general.timezone) or service.t("ux.no_due")


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


def _select_ready(select: Any) -> bool:
    """Whether a Select finished mounting its internal current-value label.

    Textual builds a Select's value label on mount, and `set_options`/`value`
    changes query that child (`#label`); poking the widget before its mount
    raises NoMatches — a race that slower runners (Windows CI) lose. Every
    Select poke below goes through this check.
    """
    return bool(getattr(select, "is_mounted", False)) and bool(select.query("#label"))


def _safe_set_value(select: Any, value: Any) -> None:
    """Assign ``select.value`` only while the widget is fully mounted.

    Two benign races would otherwise kill the app:
    - the Select is torn down between the check and the assignment (pane
      remount): the watcher's internal ``#label`` query raises NoMatches;
    - the value is no longer a legal option (the last todo of a filtered
      type was just deleted): ``_validate_value`` raises
      InvalidSelectValueError.
    Losing one programmatic sync is harmless — the next render re-derives
    the value — so swallow exactly those.
    """
    from textual.widgets._select import InvalidSelectValueError

    try:
        if select is None or not _select_ready(select):
            return
        with select.prevent(Select.Changed):
            select.value = value  # pyright: ignore[reportUnknownMemberType]
    except (QueryError, InvalidSelectValueError):
        pass


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
    if not _select_ready(select):
        # the widget is still mounting: retry once it has been refreshed, and
        # give up after a few tries rather than poking a half-built Select
        attempts = getattr(owner, "_select_retries", 0)
        if attempts >= 5:
            return
        owner._select_retries = attempts + 1
        owner.call_after_refresh(lambda: _apply_options(owner, selector, pairs))
        return
    owner._select_retries = 0
    if not getattr(owner, "_options_seeded", False):
        # compose already mounted these exact options; poking a freshly
        # mounted Select races its internal label rendering (Textual #bug)
        owner._options_seeded = True
        return
    owner._options_signatures = {**signatures, selector: signature}
    with select.prevent(Select.Changed):
        current = select.value
        try:
            select.set_options(pairs)  # pyright: ignore[reportUnknownMemberType]
        except QueryError:
            # torn down between the ready check and the poke (pane remount race)
            return
        if current is not Select.NULL:
            # the new option list may no longer contain the current value (the
            # last todo of a filtered type was just deleted, so that type vanished
            # from the dropdown): assigning it raises InvalidSelectValueError and
            # kills the app. Skip the restore — the stale-selection reset below
            # (or the widget's own default) resolves the dropdown.
            legal = {value for _prompt, value in pairs}
            if str(current) in legal:
                _safe_set_value(select, current)


class ReplyModal(ModalScreen[Any]):
    """Draft, prepare and confirm a reply with a mandatory confirmation token.

    A formal-letter template (Chinese / English) pre-fills the body with the
    date filled automatically and a right-aligned signature block; the
    toolbar wraps the selection in bold/italic and aligns paragraphs.
    """

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(
        self, service: MailFlowService, record: MailRecord, *, draft: ReplyDraft | None = None
    ) -> None:
        super().__init__()
        self._service = service
        self._record = record
        self._resume_draft = draft
        self._draft: ReplyDraft | None = None
        self._reply_busy = False
        self._bindings.bind("escape", "close_reply", self._t("tui.btn_close"))
        self._bindings.bind("ctrl+s", "save_reply", self._t("tui.reply_save"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Static(self._t("tui.reply_title"), id="reply-title")
        with Vertical(id="reply-dialog"):
            yield Label(f"{self._t('mail.field_account')}: {escape(self._record.mail.account_id)}")
            yield Label(
                f"{self._t('tui.reply_to_label')}: "
                f"{escape((self._record.mail.reply_to or self._record.mail.sender).display)}"
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
        try:
            self._draft = self._resume_draft or await self._service.create_reply(
                self._record.record_id, resume=True
            )
        except Exception as exc:
            self._set_status(escape(error_message(self._service, exc)))
            self._sync_reply_controls()
            return
        self.query_one("#reply-body", TextArea).text = self._draft.body
        self._set_status(
            self._t(
                "tui.reply_draft_status", state=self._t(f"reply.state_{self._draft.state.value}")
            )
        )
        if self._draft.state is ReplyState.SENDING:
            self._set_status(self._t("reply.send_uncertain"))
        elif self._draft.state is ReplyState.PREPARED and not self._draft.is_confirmation_valid(
            self._draft.token or ""
        ):
            self._set_status(self._t("tui.reply_prepare_expired"))
        self._sync_reply_controls()

    def _sync_reply_controls(self) -> None:
        draft = self._draft
        locked = draft is None or draft.state in (
            ReplyState.SENDING,
            ReplyState.SENT,
            ReplyState.CANCELLED,
        )
        self.query_one("#reply-body", TextArea).read_only = self._reply_busy or locked
        for button in self.query(Button):
            button.disabled = self._reply_busy or locked
        confirm = self.query_one("#reply-confirm", Button)
        confirm.disabled = (
            self._reply_busy
            or draft is None
            or not draft.is_confirmation_valid(draft.token or "")
            or self.query_one("#reply-body", TextArea).text != draft.body
        )
        if not self._reply_busy and (
            draft is None or draft.state in (ReplyState.SENT, ReplyState.CANCELLED)
        ):
            self.query_one("#reply-cancel", Button).disabled = False
            self.query_one("#reply-cancel", Button).label = self._t("tui.btn_close")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "reply-body" or self._reply_busy:
            return
        self._sync_reply_controls()
        if (
            self._draft is not None
            and self._draft.token
            and event.text_area.text != self._draft.body
        ):
            self._set_status(self._t("reply.body_changed"))

    async def _reply_failed(self, exc: Exception) -> None:
        if self._draft is not None:
            with contextlib.suppress(Exception):
                self._draft = await self._service.get_draft(self._draft.draft_id)
        self._set_status(escape(error_message(self._service, exc)))

    def _set_status(self, text: str) -> None:
        self.query_one("#reply-status", Static).update(text)

    def action_close_reply(self) -> None:
        if self._reply_busy:
            return
        draft = self._draft
        if (
            draft is not None
            and draft.state in (ReplyState.DRAFT, ReplyState.PREPARED, ReplyState.FAILED)
            and self.query_one("#reply-body", TextArea).text != draft.body
        ):
            self._start_reply_save(close=True)
        else:
            self.dismiss(None)

    def action_save_reply(self) -> None:
        if (
            not self._reply_busy
            and self._draft is not None
            and self._draft.state not in (ReplyState.SENDING, ReplyState.SENT, ReplyState.CANCELLED)
        ):
            self._start_reply_save()

    def _start_reply_save(self, *, close: bool = False) -> None:
        self._reply_busy = True
        self._sync_reply_controls()
        self._set_status(self._t("tui.reply_saving"))
        self.run_worker(
            self._save_reply(close=close), exclusive=True, group="reply-save", exit_on_error=False
        )

    async def _save_reply(self, *, close: bool) -> None:
        try:
            if self._draft is None:
                return
            self._draft = await self._service.edit_draft(
                self._draft.draft_id,
                self._draft.subject,
                self.query_one("#reply-body", TextArea).text,
            )
            if close:
                self.dismiss(None)
            else:
                self._set_status(self._t("tui.reply_saved"))
        except Exception as exc:
            await self._reply_failed(exc)
        finally:
            self._reply_busy = False
            if self.is_mounted:
                self._sync_reply_controls()

    async def _discard_reply(self) -> None:
        try:
            confirmed = await cast(MailFlowApp, self.app).push_screen_wait(  # pyright: ignore[reportUnknownMemberType]
                ConfirmModal(
                    self._service,
                    title=self._t("tui.reply_cancel"),
                    body=self._t("tui.reply_discard_body"),
                    confirm_label=self._t("tui.reply_cancel"),
                    focus_confirm=False,
                )
            )
            if confirmed and self._draft is not None:
                self._draft = await self._service.cancel_reply(self._draft.draft_id)
                self.dismiss(None)
        except Exception as exc:
            await self._reply_failed(exc)
        finally:
            self._reply_busy = False
            if self.is_mounted:
                self._sync_reply_controls()

    def _apply_template(self, language: str) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from mailflow.letters import build_letter

        tz = ZoneInfo(self._service.config.general.timezone)
        today = datetime.now(tz).date()
        recipient = (self._record.mail.reply_to or self._record.mail.sender).display
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
        if self._reply_busy:
            return
        if button_id == "reply-cancel":
            if self._draft is None or self._draft.state in (ReplyState.SENT, ReplyState.CANCELLED):
                self.dismiss(None)
                return
            self._reply_busy = True
            self._sync_reply_controls()
            self.run_worker(
                self._discard_reply(), exclusive=True, group="reply-discard", exit_on_error=False
            )
            return
        if self._draft is None:
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
            self.action_save_reply()
            return
        if button_id == "reply-prepare":
            self._reply_busy = True
            self._sync_reply_controls()
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
            if not self._draft.token:
                return
            draft = self._draft
            self._reply_busy = True
            self._sync_reply_controls()
            self._set_status(self._t("tui.reply_sending"))
            self.run_worker(
                self._confirm_reply(draft),
                exclusive=True,
                group="reply-confirm",
                exit_on_error=False,
            )

    async def _prepare_reply(self, draft_id: str) -> None:
        try:
            draft = await self._service.get_draft(draft_id)
            if draft is None:
                raise ValueError(self._t("reply.draft_not_found", draft_id=draft_id))
            body = self.query_one("#reply-body", TextArea).text
            if body != draft.body:
                await self._service.edit_draft(draft_id, draft.subject, body)
            self._draft = await self._service.prepare_reply(draft_id)
            self._set_status(self._t("tui.reply_prepared", token=self._draft.token or ""))
        except Exception as exc:
            await self._reply_failed(exc)
        finally:
            self._reply_busy = False
            self._sync_reply_controls()

    async def _confirm_reply(self, draft: ReplyDraft) -> None:
        try:
            if not draft.token:
                return
            body = self.query_one("#reply-body", TextArea).text
            if body != draft.body:
                self._draft = await self._service.edit_draft(draft.draft_id, draft.subject, body)
                self._set_status(self._t("reply.body_changed"))
                return
            self._draft = await self._service.confirm_reply(draft.draft_id, draft.token)
            self._set_status(self._t("tui.reply_sent"))
            tasks = [
                item
                for item in await self._service.list_actions()
                if item.mail_id == draft.mail_id and item.reply_task
            ]
            if tasks and await cast("Any", self).app.push_screen_wait(
                ConfirmModal(
                    self._service,
                    title=self._t("tui.reply_sent"),
                    body=self._t("ux.complete_reply_tasks", count=len(tasks)),
                    confirm_label=self._t("ux.complete_tasks"),
                    focus_confirm=False,
                )
            ):
                for item in tasks:
                    await self._service.edit_action(item.item_id, completed=True)
        except Exception as exc:
            await self._reply_failed(exc)
        finally:
            self._reply_busy = False
            self._sync_reply_controls()


class ActionModal(ModalScreen[Any]):
    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService, item: ActionItem) -> None:
        super().__init__()
        self._service = service
        self._item = item
        self._bindings.bind("escape", "dismiss", self._service.t("tui.btn_close"))

    def compose(self) -> ComposeResult:
        item = self._item
        yield Static(self._service.t("tui.action_detail"), id="action-title")
        with ScrollableContainer(id="action-detail-scroll"):
            # every field below is model/user text (summaries routinely
            # contain "[NOTICE]"-style brackets); Rich Text is never parsed
            # as console markup, so the values render verbatim
            yield Label(
                RichText.assemble(
                    (self._service.t("tui.action_time") + ": ", "bold"),
                    (_action_time_range(self._service, item) + "\n", ""),
                )
            )
            yield Label(
                RichText.assemble(
                    (self._service.t("tui.action_type") + ": ", "bold"),
                    (_action_type_label(self._service, item.action_type) + "\n", ""),
                )
            )
            yield Label(
                RichText.assemble(
                    (self._service.t("tui.action_content") + ":\n", "bold"),
                    (item.summary + "\n", ""),
                )
            )
            yield Label(
                RichText.assemble(
                    (self._service.t("tui.action_notes") + ": ", "bold"),
                    ((item.notes or "-") + "\n", ""),
                )
            )
            yield Label(
                RichText.assemble(
                    (self._service.t("tui.action_source") + ": ", "bold"),
                    (item.mail_id, ""),
                )
            )
            if item.location:
                yield Label(
                    RichText.assemble(
                        (self._service.t("tui.action_location") + ": ", "bold"),
                        (item.location + "\n", ""),
                    )
                )
            if item.url:
                yield Label(
                    RichText.assemble(
                        (self._service.t("tui.action_url") + ": ", "bold"),
                        (item.url + "\n", ""),
                    )
                )
            # the source mail's own details load asynchronously below
            yield Static("", id="action-mail-detail")
        # outside the scroll box: always visible, never scrolls away
        with Horizontal(id="action-footer"):
            yield Button(self._service.t("tui.btn_edit"), id="action-edit", variant="primary")
            yield Button(self._service.t("tui.btn_close"), id="action-close", variant="default")

    async def on_mount(self) -> None:
        """Load the source mail: a todo without its original message context
        (subject, sender, body) forces the user to hunt through the mail tab."""
        node = self.query_one_optional("#action-mail-detail", Static)
        if node is None:
            return
        if not self._item.mail_id:
            return
        try:
            record = await self._service.get_mail(self._item.mail_id)
        except Exception as exc:
            node.update(_load_error(self._service, exc))
            return
        if record is None:
            node.update(f"[dim]{self._service.t('tui.action_mail_missing')}[/dim]")
            return
        mail = record.mail
        body = mail_body_text(mail)
        if not body and looks_binary(mail.body_text):
            body = self._service.t("tui.detail_binary_body")
        lines: RichText = RichText()
        status = mail_status(self._service, record)
        if status:
            lines.append(status)
            lines.append("\n")
        lines.append(f"{self._service.t('tui.column_subject')}: ", style="bold")
        lines.append(f"{mail.subject}\n")
        lines.append(f"{self._service.t('tui.column_sender')}: ", style="bold")
        lines.append(f"{mail.sender.display or mail.sender.address}\n")
        lines.append(f"{self._service.t('tui.column_date')}: ", style="bold")
        lines.append(f"{_localize(self._service, mail.date)}\n")
        summary = self._service.display_text(record.summary)
        lines.append(f"{self._service.t('tui.detail_summary')}:\n", style="bold")
        lines.append(summary or self._service.t("tui.detail_analysis_unavailable"))
        if record.analysis_is_fallback and summary:
            lines.append(f" ({self._service.t('tui.detail_summary_fallback')})", style="yellow")
        lines.append("\n")
        reason = record.analysis.reason if record.analysis else ""
        if reason:
            lines.append(f"{self._service.t('tui.detail_reason')}:\n", style="bold")
            lines.append(f"{self._service.display_text(reason)}\n")
        lines.append(f"{self._service.t('tui.detail_body')}:\n", style="bold")
        lines.append(body[:800] or self._service.t("tui.mail_no_body"))
        if mail.image_text.strip():
            lines.append(f"\n\n{self._service.t('mail.field_image_text')}:\n", style="bold")
            lines.append(mail.image_text)
        node.update(lines)

    def _open_editor(self) -> None:
        def _after_edit(changed: bool | None) -> None:
            if changed:
                self.dismiss(True)

        cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
            TodoEditModal(self._service, self._item), callback=_after_edit
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "action-edit":
            self._open_editor()
        elif event.button.id == "action-close":
            self.dismiss(None)


class MailPane(Vertical):
    BINDINGS: ClassVar[list[Any]] = [Binding("ctrl+z", "undo_trash", show=False)]

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._records: list[MailRecord] = []
        self._mail_cache: list[MailRecord] | None = None
        self._expired_cache: set[str] | None = None
        self._selected_id: str | None = None
        self._detail_record_id: str | None = None
        self._rendering = False
        # refresh_mail runs from several entry points (mount worker, filter
        # changes, reload-all); concurrent clear+add_row passes interleave
        # into DuplicateKeys — serialize them
        self._refresh_lock = asyncio.Lock()
        self._mail_filter_generation = 0
        self._mail_matcher = SearchMatcher("")
        # A bulk re-analysis checks _reparse_stop between mails, and the button
        # that started it becomes the stop control while its worker is alive.
        # "running" is derived from the worker (never a hand-set flag) so a
        # cancelled or finished run cannot leave the button stuck as a stop
        # control that no longer responds.
        self._reparse_stop = False
        self._reparse_worker: Any = None
        # Re-analysis emits mail-processed events which asynchronously refresh
        # this pane. Keep the operation outcome separately so that refresh
        # cannot erase feedback before the user can read it.
        self._operation_status = ""
        self._query = MailQuery()
        self._next_offset: int | None = None
        self._page_limit = 200
        self._page_total = 0
        self._trash_busy = False
        self._undo_trash_ids: list[str] = []
        self._selection_hint_visible = False

    def compose(self) -> ComposeResult:
        self._urgency_suppress = True
        remote = bool(getattr(self._service, "remote", False))
        with Horizontal(id="mail-search-row"):
            yield Input(
                placeholder=self._service.t("tui.mail_search_placeholder"),
                id="mail-search",
            )
            yield SearchToggle("regex", ident="mail-search-regex")
            yield SearchToggle("case", ident="mail-search-case")
            yield Button(self._service.t("assistant.handoff"), id="btn-assistant")
        yield Static("", id="mail-empty-hint")
        with Horizontal(id="mail-processing-controls"):
            yield Static("", id="mail-processing-status")
            yield Button(self._service.t("tui.analysis_pause"), id="analysis-toggle")
        with Horizontal():
            yield SelectionTable(t=self._service.t, id="mail-table")
            with ScrollableContainer(id="mail-detail"):
                for ident in (
                    "mail-analysis-status",
                    "mail-operation-status",
                    "mail-summary",
                    "mail-reason",
                    "mail-actions",
                    "mail-body",
                    "mail-attachments",
                    "mail-notes",
                ):
                    yield Static("", id=ident)
        with Horizontal(id="mail-controls"):
            yield SafeSelect(self._urgency_options(), id="urgency-select", allow_blank=False)
            yield SafeSelect(
                self._urgency_filter_options(), id="mail-urgency-filter", allow_blank=False
            )
            yield SafeSelect(
                [
                    (self._service.t("tui.sort_time"), "time"),
                    (self._service.t("tui.sort_urgency"), "urgency"),
                ],
                id="mail-sort",
                allow_blank=False,
            )
            with Vertical(id="mail-actions-buttons"):
                for index, buttons in enumerate(self._button_rows(), 1):
                    with Horizontal(id=f"mail-actions-row{index}"):
                        for ident, key, variant in buttons:
                            if remote and ident not in {
                                "btn-more",
                                "btn-refresh",
                                "btn-trash",
                                "btn-reply",
                                "btn-profile",
                                "btn-ask-correct",
                                "btn-purge-expired",
                            }:
                                continue
                            yield Button(
                                self._service.t(key),
                                id=ident,
                                variant=variant,
                                tooltip=self._service.t(key),
                            )

    @staticmethod
    def _button_rows() -> tuple[tuple[tuple[str, str, Any], ...], ...]:
        return (
            (
                ("btn-refresh", "tui.btn_refresh", "primary"),
                ("btn-trash", "tui.btn_trash", "error"),
                ("btn-ask-correct", "tui.btn_ask_correct", "warning"),
            ),
            (
                ("btn-reply", "tui.btn_reply", "success"),
                ("btn-reparse", "tui.btn_reparse", "primary"),
                ("btn-reparse-failed", "tui.btn_reparse_failed", "error"),
            ),
            (
                ("btn-purge-expired", "tui.btn_purge_expired", "warning"),
                ("btn-reparse-all", "tui.btn_reparse_all", "primary"),
                ("btn-profile", "tui.btn_profile", "success"),
                ("btn-more", "ux.mail_tools", "default"),
            ),
        )

    async def on_mount(self) -> None:
        self._urgency_suppress = False
        self._sync_selection_controls()
        # never block startup on storage reads + row rendering: the pane
        # paints empty and fills in as the exclusive worker delivers
        self.run_worker(
            self.refresh_mail(), exclusive=True, group="mail-refresh", exit_on_error=False
        )

    async def on_unmount(self) -> None:
        self._stop_reparse()

    def _mail_table(self) -> SelectionTable | None:
        return self.query_one_optional("#mail-table", SelectionTable)

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

    def _urgency_filter_options(self) -> list[tuple[str, str]]:
        return (
            [(self._service.t("tui.filter_all_urgencies"), "all")]
            + [(urgency_label(self._service, urgency), urgency.value) for urgency in Urgency]
            + [(self._service.t("tui.filter_expired"), "expired")]
        )

    def _ensure_columns(self) -> None:
        if getattr(self, "_columns_done", False):
            return
        table = self._mail_table()
        if table is None:
            return
        for key in ("selected", "urgency", "subject", "sender", "date"):
            _remove_column(table, key)
        table.add_column(RichText("[ ]"), key="selected", width=3)
        table.add_column(self._service.t("tui.column_date"), key="date")
        table.add_column(self._service.t("tui.column_urgency"), key="urgency")
        table.add_column(self._service.t("tui.column_subject"), key="subject")
        table.add_column(self._service.t("tui.column_sender"), key="sender")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        table.tooltip = self._service.t("tui.selection_help")
        urgency = self._urgency_select()
        if urgency is not None:
            urgency.tooltip = self._service.t("tui.urgency_help")
        self._columns_done = True

    async def relabel(self) -> None:
        self._columns_done = False
        _relabel_buttons(
            self,
            {
                ident: key
                for row in self._button_rows()
                for ident, key, _variant in row
                if not (
                    ident.startswith("btn-reparse")
                    and self._reparse_worker is not None
                    and self._reparse_worker.is_running
                )
            },
        )
        search = self.query_one_optional("#mail-search", Input)
        if search is not None:
            search.placeholder = self._service.t("tui.mail_search_placeholder")
        self._urgency_suppress = True
        try:
            _apply_options(self, "#urgency-select", self._urgency_options())
        finally:
            self._urgency_suppress = False
        hint = self.query_one_optional("#urgency-hint", Static)
        if hint is not None:
            hint.update(self._service.t("tui.urgency_select_hint"))
        await self.refresh_mail()

    async def refresh_mail(self, *, reload: bool = True) -> None:
        async with self._refresh_lock:
            try:
                await self._refresh_mail_unlocked(reload=reload)
            except Exception as exc:
                if self._mail_cache is not None:
                    with contextlib.suppress(Exception):
                        await self._refresh_mail_unlocked(reload=False)
                self._set_static("#mail-empty-hint", _load_error(self._service, exc))

    async def _render_records(
        self,
        records: list[MailRecord],
        *,
        empty_hint_key: str = "tui.mail_empty",
        status_hint: str = "",
    ) -> None:
        """Render records in their supplied order without re-reading storage.

        Smart-search results arrive ranked by the service; ordinary mailbox
        rendering has its own explicit sort path in ``_refresh_mail_unlocked``.
        """
        table = self._mail_table()
        if table is None:
            return
        view = TableView.capture(table, self._selected_id)
        self._rendering = True
        try:
            with table.prevent(DataTable.RowHighlighted, DataTable.RowSelected):
                table.clear()
                self._ensure_columns()
                self._refresh_view_options()
                for position, record in enumerate(records, start=1):
                    table.add_row(*self._mail_cells(record), key=record.record_id)
                    if position % 50 == 0:
                        await asyncio.sleep(0)
                self._selected_id = view.restore(table)
        finally:
            self._rendering = False
        hint = self.query_one_optional("#mail-empty-hint", Static)
        self._selection_hint_visible = False
        if hint is not None:
            if status_hint:
                hint.update(status_hint)
                hint.display = "block"  # pyright: ignore[reportUnknownMemberType]
            elif not records:
                hint.update(self._service.t(empty_hint_key))
                hint.display = "block"  # pyright: ignore[reportUnknownMemberType]
            else:
                hint.update(_BLANK)
                hint.display = "none"  # pyright: ignore[reportUnknownMemberType]
        selected = next((r for r in records if r.record_id == self._selected_id), None)
        if selected is not None:
            self._sync_urgency_select(selected)
        await self._show_selected(cached=True)
        self._sync_selection_controls()
        self.sync_analysis_controls()

    def sync_analysis_controls(self) -> None:
        snapshot = self._service.snapshot()
        pending = max(
            sum(account.pending_count for account in snapshot.accounts),
            sum(r.analysis_state in {"pending", "analysing"} for r in self._mail_cache or []),
        )
        paused = snapshot.analysis_paused
        status = self.query_one_optional("#mail-processing-status", Static)
        if status is not None:
            status.update(
                self._service.t(
                    "tui.analysis_queue_paused" if paused else "tui.analysis_queue", count=pending
                )
            )
        button = self.query_one_optional("#analysis-toggle", Button)
        if button is not None:
            button.label = self._service.t(
                "tui.analysis_resume" if paused else "tui.analysis_pause"
            )
        controls = self.query_one_optional("#mail-processing-controls", Horizontal)
        if controls is not None:
            controls.display = bool(pending or paused)

    async def _toggle_analysis(self) -> None:
        button = self.query_one("#analysis-toggle", Button)
        button.disabled = True
        try:
            await self._service.set_analysis_paused(not self._service.snapshot().analysis_paused)
            await self.refresh_mail()
        except Exception as exc:
            self._set_operation_status(escape(error_text(exc, self._service.t)))
        finally:
            button.disabled = False

    def _mail_cells(self, record: MailRecord) -> tuple[Any, ...]:
        urgency = record.effective_urgency
        table = self._mail_table()
        return (
            table.check_label(record.record_id) if table is not None else "[ ]",
            _localize(self._service, record.mail.received_at, "%m-%d %H:%M"),
            RichText(f"■ {urgency_label(self._service, urgency)}", style=urgency.color),
            RichText(
                ("★ " if record.starred else "")
                + (
                    "↻ "
                    if record.analysis_state == "analysing"
                    else "… "
                    if record.analysis_state == "pending"
                    else ""
                )
                + ("● " if not record.is_read else "")
                + (record.mail.subject or self._service.t("tui.mail_no_subject"))
            ),
            RichText(record.mail.sender.display),
        )

    async def _refresh_mail_unlocked(self, *, reload: bool = True) -> None:
        if self._mail_table() is None:
            return
        if reload or self._mail_cache is None:
            search_input = self.query_one_optional("#mail-search", Input)
            regex_toggle = self.query_one_optional("#mail-search-regex", SearchToggle)
            query = self._query.model_copy(
                update={
                    "text": search_input.value.strip()
                    if search_input is not None and not (regex_toggle and regex_toggle.value)
                    else "",
                    "urgency": self._select_value("#mail-urgency-filter")
                    if self._select_value("#mail-urgency-filter") not in {"all", "expired"}
                    else "",
                    "limit": min(500, self._page_limit),
                    "offset": 0,
                }
            )
            read_page = getattr(self._service, "query_mails", None)
            if read_page is not None:
                page = await read_page(query)
                rows = list(page.records)
                while page.next_offset is not None and len(rows) < self._page_limit:
                    page = await read_page(
                        query.model_copy(
                            update={
                                "offset": page.next_offset,
                                "limit": min(500, self._page_limit - len(rows)),
                            }
                        )
                    )
                    rows.extend(page.records)
                self._mail_cache = rows
                self._next_offset, self._page_total = page.next_offset, page.total
            else:
                self._mail_cache = [
                    row for row in await self._service.list_mails() if query.matches(row)
                ]
            self._expired_cache = None
        records = [record for record in self._mail_cache or [] if self._query.matches(record)]
        has_records = bool(records)
        table = self._mail_table()
        if table is not None:
            available = {record.record_id for record in records}
            if reload:
                for ident in table.checked_keys - available:
                    if await self._service.get_mail(ident) is not None:
                        available.add(ident)
            else:
                available.update(table.checked_keys)
            table.retain_checks(available)
        search = self.query_one_optional("#mail-search", Input)
        regex = self.query_one_optional("#mail-search-regex", SearchToggle)
        case = self.query_one_optional("#mail-search-case", SearchToggle)
        matcher = build_search_matcher(
            search.value if search is not None else "",
            regex=bool(regex.value) if regex is not None else False,
            case_sensitive=bool(case.value) if case is not None else False,
        )
        if matcher.error:
            self._set_static(
                "#mail-empty-hint",
                f"[red]{self._service.t('tui.invalid_regex')}: {escape(self._service.t('errors.invalid_value'))}[/red]",
            )
            return
        self._mail_matcher = matcher
        if matcher.query:
            records = [record for record in records if self._matches(record, matcher)]
        urgency_filter = self._select_value("#mail-urgency-filter")
        if urgency_filter == "expired":
            if self._expired_cache is None:
                self._expired_cache = {
                    record.record_id for record in await self._service.list_expired_mails()
                }
            records = [record for record in records if record.record_id in self._expired_cache]
        elif urgency_filter != "all":
            records = [r for r in records if r.effective_urgency.value == urgency_filter]
        records = sorted(records, key=lambda r: r.mail.received_at, reverse=True)
        if self._select_value("#mail-sort") == "urgency":
            records.sort(key=lambda r: r.effective_urgency.rank, reverse=True)
        status = ""
        if self._next_offset is not None:
            status += " " + self._service.t(
                "ux.loaded_mail", shown=len(records), total=self._page_total
            )
        self._records = records
        await self._render_records(
            records,
            empty_hint_key="tui.mail_no_match" if has_records else "tui.mail_empty",
            status_hint=status,
        )

    def on_selection_table_selection_changed(self, event: SelectionTable.SelectionChanged) -> None:
        if event.table.id == "mail-table":
            self._sync_selection_controls()

    def _sync_selection_controls(self, *, reparse_idle: bool = False) -> None:
        table = self._mail_table()
        if table is None:
            return
        count = len(table.checked_keys)
        has_selected = any(r.record_id == self._selected_id for r in self._records)
        trash = self.query_one_optional("#btn-trash", Button)
        if trash is not None:
            trash.label = (
                self._service.t("tui.btn_delete_selected", count=count)
                if count
                else self._service.t("tui.btn_trash")
            )
            trash.disabled = self._trash_busy or not (count or has_selected)
            trash.tooltip = self._service.t("tui.selection_delete_help")
        purge = self.query_one_optional("#btn-purge-expired", Button)
        if purge is not None:
            purge.disabled = self._trash_busy or not bool(self._mail_cache)
        for ident in ("#btn-reply", "#btn-ask-correct"):
            button = self.query_one_optional(ident, Button)
            if button is not None:
                button.tooltip = self._service.t("tui.selection_current_help")
                button.disabled = self._trash_busy or not has_selected
        table.disabled = self._trash_busy
        reparse = self.query_one_optional("#btn-reparse", Button)
        if reparse is not None and (reparse_idle or not self._reparse_active()):
            reparse.disabled = self._trash_busy or not (count or has_selected)
            reparse.label = (
                self._service.t("tui.btn_reparse_selected", count=count)
                if count
                else self._service.t("tui.btn_reparse")
            )
        if reparse_idle or not self._reparse_active():
            for ident in ("#btn-reparse-all", "#btn-reparse-failed"):
                button = self.query_one_optional(ident, Button)
                if button is not None:
                    button.disabled = self._trash_busy or not bool(self._mail_cache)
        urgency = self._urgency_select()
        if urgency is not None:
            urgency.disabled = not has_selected or self._trash_busy
        if count:
            hidden = count - len(table.checked_keys & set(table.visible_keys()))
            self._set_static(
                "#mail-empty-hint",
                self._service.t("tui.selection_count", count=count, hidden=hidden),
            )
            self._selection_hint_visible = True
        elif table.selection_mode:
            self._set_static("#mail-empty-hint", self._service.t("tui.selection_mode_active"))
            self._selection_hint_visible = True
        elif self._selection_hint_visible:
            self._set_static("#mail-empty-hint", "")
            self._selection_hint_visible = False

    def _mail_targets(self) -> list[str]:
        table = self._mail_table()
        if table is not None and table.checked_keys:
            return sorted(table.checked_keys)
        return [self._selected_id] if self._selected_id is not None else []

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
        _apply_options(self, "#mail-urgency-filter", self._urgency_filter_options())
        _apply_options(
            self,
            "#mail-sort",
            [
                (service.t("tui.sort_time"), "time"),
                (service.t("tui.sort_urgency"), "urgency"),
            ],
        )

    def _matches(self, record: MailRecord, matcher: SearchMatcher) -> bool:
        """Search subject, sender, summary and body through one matcher."""
        from mailflow.ocr import searchable_record_text

        return matcher.matches(searchable_record_text(record))

    def _schedule_mail_filter(self) -> None:
        self._mail_filter_generation += 1
        generation = self._mail_filter_generation

        async def _filter() -> None:
            await asyncio.sleep(0.3)
            if generation != self._mail_filter_generation:
                return
            await self.refresh_mail(reload=True)

        self.run_worker(
            cast(Any, _filter), exclusive=True, group="mail-filter", exit_on_error=False
        )

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "mail-search":
            self._page_limit = 200
            self._schedule_mail_filter()

    def on_search_toggle_toggled(self, event: SearchToggle.Toggled) -> None:
        if event.toggle.id in {"mail-search-regex", "mail-search-case"}:
            self._schedule_mail_filter()

    async def on_data_table_row_selected(self, event: TableRowEvent) -> None:
        if event.data_table.id != "mail-table" or self._rendering:
            return
        self._selected_id = event.row_key.value
        if self._selected_id is None:
            return
        record = next((r for r in self._records if r.record_id == self._selected_id), None)
        if record is not None:
            self._sync_urgency_select(record)
        if record is not None and not record.is_read:
            await self._service.set_mail_state([record.record_id], is_read=True)
            record.is_read = True
        await self._show_selected(cached=True)

        self._sync_selection_controls()

    async def on_data_table_row_highlighted(self, event: TableRowEvent) -> None:
        """Cursor movement (arrow keys) highlights rows without selecting;
        the detail pane must follow the highlight so what the user looks at
        is what the right side shows."""
        if event.data_table.id != "mail-table" or self._rendering or event.row_key.value is None:
            return
        self._selected_id = event.row_key.value
        record = next((r for r in self._records if r.record_id == self._selected_id), None)
        if record is not None:
            self._sync_urgency_select(record)
        await self._show_selected(cached=True)
        self._sync_selection_controls()

    async def on_select_changed(self, event: Select.Changed) -> None:
        try:
            await self._handle_select_changed(event)
        except Exception as exc:
            message = f"[red]{escape(error_message(self._service, exc))}[/red]"
            self._set_operation_status(message)

    async def _handle_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in ("mail-urgency-filter", "mail-sort"):
            if event.select.id == "mail-urgency-filter":
                self._expired_cache = None
            await self.refresh_mail(reload=event.select.id == "mail-urgency-filter")
        elif (
            event.select.id == "urgency-select"
            and self._selected_id is not None
            and not getattr(self, "_urgency_suppress", False)
        ):
            value = event.value
            if value is Select.NULL or str(value) in ("", "auto"):
                # The automatic option and blank sentinel clear the override.
                urgency: Urgency | None = None
            else:
                try:
                    urgency = Urgency(str(value))
                except ValueError:
                    return
            await self._service.set_mail_urgency(self._selected_id, urgency)
            await self.refresh_mail()

    def _clear_mail_detail(self) -> None:
        """Remove detail from a record no longer visible in the table."""
        self._detail_record_id = None
        for selector in (
            "#mail-analysis-status",
            "#mail-operation-status",
            "#mail-summary",
            "#mail-reason",
            "#mail-actions",
            "#mail-body",
            "#mail-attachments",
            "#mail-notes",
        ):
            self._set_static(selector, "")
        self._set_static("#mail-operation-status", self._operation_status)

    async def _show_selected(self, *, cached: bool = False) -> None:
        """Render the detail column for the selected mail.

        Every dynamic field (summary, reason, action items, body, processor
        notes) is pipeline/model/user text that may contain square brackets
        ("[NOTICE] …", a pydantic error repr with ``input_value={'item':
        …}``). Textual 8.2.8's ``escape()`` does not neutralize those — the
        markup parser swallows bracket tags or raises "Expected markup
        value" and kills the app mid mail-switch. Render each field as Rich
        ``Text`` (never markup-parsed) with bold labels as styled spans.
        """
        if self._selected_id is None:
            self._clear_mail_detail()
            return
        selected_id = self._selected_id
        detail = self.query_one_optional("#mail-detail", ScrollableContainer)
        try:
            record = (
                next((r for r in self._records if r.record_id == selected_id), None)
                if cached
                else await self._service.get_mail(selected_id)
            )
            existing = (
                await self._service.get_feedback(selected_id)
                if record is not None and not getattr(self._service, "remote", False)
                else None
            )
        except Exception as exc:
            if selected_id == self._selected_id:
                self._clear_mail_detail()
                self._set_static("#mail-analysis-status", _load_error(self._service, exc))
            return
        if selected_id != self._selected_id:
            return
        if record is None:
            self._clear_mail_detail()
            self._set_static_text(
                "#mail-analysis-status",
                RichText(self._service.t("mail.not_found", mail_id=selected_id), style="yellow"),
            )
            return
        same_record = self._detail_record_id == selected_id
        self._detail_record_id = selected_id
        service = self._service
        status = mail_status(service, record)
        self._set_static_text("#mail-analysis-status", status)
        self._set_static("#mail-operation-status", self._operation_status)
        # The stored summary and reason are the mail's content: they stay
        # visible even when the pipeline had to fall back, because hiding them
        # left the user with no content at all. A fallback is labelled, never
        # presented as a generated analysis.
        summary_value = service.display_text(record.summary).strip()
        summary_line = RichText(service.t("tui.detail_summary") + ": ", style="bold")
        if summary_value:
            summary_line.append(summary_value)
            if record.analysis_is_fallback:
                summary_line.append(
                    f" ({service.t('tui.detail_summary_fallback')})", style="yellow"
                )
        else:
            summary_line.append(service.t("tui.detail_analysis_unavailable"), style="yellow")
        self._set_static_text("#mail-summary", summary_line)
        reason = service.display_text(record.analysis.reason if record.analysis else "").strip()
        reason_line = RichText(service.t("tui.detail_reason") + ": ", style="bold")
        if reason:
            reason_line.append(reason)
        elif record.analysis_is_fallback:
            reason_line.append(service.t("tui.detail_reason_unavailable"), style="yellow")
        else:
            reason_line.append("-")
        self._set_static_text("#mail-reason", reason_line)
        actions_text: RichText | None = None
        if record.action_items:
            actions_text = RichText("\n" + service.t("tui.action_content") + ":", style="bold")
            for item in record.action_items:
                actions_text.append("\n  ")
                actions_text.append(
                    f"{_action_time_range(service, item)} "
                    f"({_action_type_label(service, item.action_type)}) {item.summary}"
                )
                if item.notes:
                    actions_text.append(f" — {item.notes}")
        self._set_static_text("#mail-actions", actions_text or RichText(""))
        body = mail_body_text(record.mail)
        if not body and looks_binary(record.mail.body_text):
            body = service.t("tui.detail_binary_body")
        if len(body) > 4000:
            # a 200 KB dump into one Static is unusable and slow to render
            body = body[:4000] + "…\n" + service.t("ux.read_full_hint")
        body_line = RichText(service.t("tui.detail_body") + ":\n", style="bold")
        body_line.append(body or service.t("tui.mail_no_body"))
        if record.mail.image_text.strip():
            body_line.append(f"\n\n{service.t('mail.field_image_text')}:\n", style="bold")
            body_line.append(record.mail.image_text)
        self._set_static_text("#mail-body", body_line)
        attachments_line: RichText | None = None
        if record.mail.attachments:
            attachments_line = RichText(service.t("tui.detail_attachments") + ":", style="bold")
            for a in record.mail.attachments:
                attachments_line.append(f"\n  {a.filename} ({a.content_type}, {a.size} B)")
        self._set_static_text("#mail-attachments", attachments_line or RichText(""))
        reply_line = RichText()
        if record.analysis and record.analysis.reply_required:
            reply_line.append(
                "\n" + service.t("tui.detail_reply_required", answer=service.t("common.yes")),
                style="bold yellow",
            )
        if existing:
            reply_line.append(
                f"\n{service.t('tui.feedback_marker')}: {existing}", style="bold yellow"
            )
        reply_line.append(
            f"\n{service.t('tui.urgency_label')}: {urgency_label(service, record.effective_urgency)} "
            f"({service.t('tui.detail_manual_marker') if record.manual_urgency is not None else service.t('tui.detail_auto_marker')})"
        )
        self._set_static_text("#mail-notes", reply_line)
        if detail is not None and not same_record:
            detail.scroll_to(x=0, y=0, animate=False, force=True, immediate=True)

    def _set_static_text(self, selector: str, content: RichText | None) -> None:
        """Update a detail Static with Rich ``Text`` — console markup in the
        content is rendered verbatim instead of parsed, so bracket-laden
        pipeline text can never raise MarkupError."""
        if content is None:
            return
        node = self.query_one_optional(selector, Static)
        if node is None:
            return
        node.update(content)
        node.display = bool(content.plain)

    def _sync_urgency_select(self, record: MailRecord) -> None:
        """Mirror the selected mail's manual override into the dropdown
        (suppressed: a programmatic change is not a user mutation)."""
        wanted = "auto" if record.manual_urgency is None else record.manual_urgency.value
        self._urgency_suppress = True
        try:
            select = self._urgency_select()
            if select is not None and _select_ready(select) and str(select.value) != wanted:
                _safe_set_value(select, wanted)
        finally:
            self._urgency_suppress = False

    def _set_static(self, selector: str, content: str) -> None:
        node = self.query_one_optional(selector, Static)
        if node is None:
            return
        node.update(content)
        # an empty Static still occupies a row: hiding it keeps the detail pane
        # flush at the top instead of leaving blank lines above the summary
        node.display = bool(content)

    def _set_operation_status(self, content: str) -> None:
        """Show re-analysis feedback and retain it through event refreshes."""
        self._operation_status = content
        self._set_static("#mail-operation-status", content)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        try:
            await self._handle_button_pressed(event)
        except Exception as exc:
            message = f"[red]{escape(error_message(self._service, exc))}[/red]"
            self._set_operation_status(message)

    async def _handle_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "analysis-toggle":
            self.run_worker(self._toggle_analysis(), group="analysis-control", exit_on_error=False)
            return
        if button_id == "btn-assistant":
            self.run_worker(
                self._handoff_assistant(), group="assistant-context", exit_on_error=False
            )
            return
        if button_id == "btn-more":
            self.run_worker(
                self._mail_tools(), exclusive=True, group="mail-tools", exit_on_error=False
            )
            return
        if button_id == "btn-refresh":
            await self.refresh_mail()
            return
        if button_id == "btn-purge-expired":
            # app-owned worker: a pane remount (language switch) cancels the
            # pane's own workers, which would stop a half-finished purge
            if not self._trash_busy:
                self._start_trash(self._purge_expired)
            return
        if button_id == "btn-trash":
            if not self._trash_busy:
                self._start_trash(self._trash_selected)
            return
        if button_id in self._REPARSE_BUTTONS:
            if self._reparse_active():
                self._stop_reparse()
                return
            # build exactly one coroutine: constructing all three and using one
            # leaks the others ("coroutine was never awaited")
            if button_id == "btn-reparse":
                coro = self._reparse_one()
            elif button_id == "btn-reparse-failed":
                coro = self._reparse_failed()
            else:
                coro = self._reparse_all()
            self._start_reparse(button_id, coro)
            return
        if button_id == "btn-profile":
            cast(MailFlowApp, self.app).run_worker(  # pyright: ignore[reportUnknownMemberType]
                self._edit_profile(), exclusive=True, group="mail-profile", exit_on_error=False
            )
            return
        if self._selected_id is None:
            return
        if button_id == "btn-ask-correct":
            record = await self._service.get_mail(self._selected_id)
            if record is None:
                return
            from mailflow_tui.ask_correct import AskCorrectModal

            cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                AskCorrectModal(self._service, record)
            )
            return
        if button_id == "btn-reply":
            record = await self._service.get_mail(self._selected_id)
            if record is not None:
                # textual types Widget.app loosely; the cast pins the concrete app
                cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                    ReplyModal(self._service, record)
                )

    async def _handoff_assistant(self) -> None:
        from mailflow_tui.mail_workflows import SmartScopeScreen

        mode = await self._ask_screen(SmartScopeScreen(self._service, "auto"))
        if mode is None:
            return
        table = self._mail_table()
        scope: dict[str, Any]
        if mode == "selected" or (mode == "auto" and table is not None and table.checked_keys):
            scope = {"record_ids": self._mail_targets()}
        elif mode == "account" or (mode == "auto" and self._query.account_id):
            account = self._query.account_id or next(
                (r.mail.account_id for r in self._records if r.record_id == self._selected_id), ""
            )
            if not account:
                self._set_operation_status(self._service.t("ux.choose_account"))
                return
            scope = {"account_id": account}
        elif mode == "results":
            scope = {"record_ids": [r.record_id for r in self._records]}
        else:
            scope = {}
        cast(MailFlowApp, self.app).open_assistant(scope, self._service.t("ux.scope_" + str(mode)))  # pyright: ignore[reportUnknownMemberType]

    async def _mail_tools(self) -> None:
        from mailflow_tui.mail_workflows import TrashScreen

        choice = await self._ask_screen(MailMenu(self._service))
        if not choice:
            return
        try:
            if choice == "undo_delete":
                await self._undo_trash()
                return
            elif choice == "sync_mail":
                from mailflow_tui.mail_workflows import SyncScreen

                await self._ask_screen(SyncScreen(self._service, self._query.account_id))
            elif choice == "filter":
                query = await self._ask_screen(MailFilter(self._service, self._query))
                if query is not None:
                    self._query = query
                    self._page_limit = 200
            elif choice == "load_more":
                if self._next_offset is not None:
                    self._page_limit += 200
                    # Continue beyond 500 records without downloading the whole mailbox.
                    page = await self._service.query_mails(
                        self._query.model_copy(
                            update={
                                "offset": self._next_offset,
                                "text": ""
                                if self.query_one("#mail-search-regex", SearchToggle).value
                                else self.query_one("#mail-search", Input).value,
                                "urgency": self._select_value("#mail-urgency-filter")
                                if self._select_value("#mail-urgency-filter")
                                not in {"all", "expired"}
                                else "",
                                "limit": 200,
                            }
                        )
                    )
                    self._mail_cache = list(
                        {
                            r.record_id: r for r in [*(self._mail_cache or []), *page.records]
                        }.values()
                    )
                    self._next_offset = page.next_offset
                    await self.refresh_mail(reload=False)
                    return
            elif choice == "trash":
                await self._ask_screen(TrashScreen(self._service))
            elif choice == "drafts":
                draft = await self._ask_screen(DraftsScreen(self._service))
                if draft is not None:
                    record = await self._service.get_mail(draft.mail_id)
                    if record is not None:
                        await self._ask_screen(ReplyModal(self._service, record, draft=draft))
                    else:
                        self._set_operation_status(
                            self._service.t("mail.not_found", mail_id=draft.mail_id)
                        )
            elif choice == "cleanup_preview":
                records = await self._service.cleanup_preview()
                body = (
                    self._service.t("ux.cleanup_confirm", count=len(records))
                    + "\n"
                    + "\n".join(r.mail.subject for r in records[:30])
                )
                confirmed = (
                    await self._ask_screen(
                        ConfirmModal(
                            self._service,
                            title=self._service.t("ux.cleanup_preview"),
                            body=body,
                            confirm_label=self._service.t("tui.btn_apply"),
                            focus_confirm=False,
                        )
                    )
                    if records
                    else False
                )
                if confirmed:
                    # Only the reviewed set; re-check protection immediately before each move.
                    await self._service.cleanup_selected([r.record_id for r in records])
                elif not records:
                    self._set_operation_status(self._service.t("ux.cleanup_empty"))
            elif choice in {"full_mail", "attachments", "task_from_mail"}:
                record = await self._service.get_mail(self._selected_id or "")
                if record is not None:
                    if choice == "task_from_mail":
                        await self._ask_screen(
                            TodoCreateModal(
                                self._service, mail_id=record.record_id, summary=record.mail.subject
                            )
                        )
                    else:
                        await self._ask_screen(
                            (MailReader if choice == "full_mail" else AttachmentsScreen)(
                                self._service, record
                            )
                        )
            else:
                fields = {
                    "read": ("is_read", True),
                    "unread": ("is_read", False),
                    "star": ("starred", True),
                    "unstar": ("starred", False),
                    "archive": ("archived", True),
                    "unarchive": ("archived", False),
                }
                field, value = fields[str(choice)]
                result = await self._service.set_mail_state(self._mail_targets(), **{field: value})
                self._set_operation_status(
                    self._service.t(
                        "ux.batch_result",
                        success=len(result.succeeded),
                        failed=len(result.failed),
                        skipped=len(result.skipped),
                    )
                )
            await self.refresh_mail()
        except Exception as exc:
            self._set_operation_status(escape(error_text(exc, self._service.t)))

    _REPARSE_CONCURRENCY = 3
    """Mails analyzed at once during a bulk run: a slow endpoint makes a strictly
    sequential sweep feel endless, while a small window keeps rate limits sane."""

    async def _reparse_batch(self, mails: list[Any]) -> None:
        """Force re-analysis for the given messages, with per-mail progress.

        Runs a small window of mails concurrently, stops when the user asks to
        (the worker is cancelled, so an in-flight request ends at once instead
        of holding the UI until its timeout) and reports how many were really
        analyzed. Whatever finished stays analyzed.
        """
        total = len(mails)
        done = 0
        failed: list[str] = []
        completed = 0
        lock = asyncio.Lock()
        window = asyncio.Semaphore(self._REPARSE_CONCURRENCY)

        async def _analyze(mail: Any) -> None:
            nonlocal done, completed
            async with window:
                if self._reparse_stop or not self.is_mounted:
                    return
                async with lock:
                    position = completed + 1
                    completed += 1
                subject_short = escape((mail.subject or "")[:36])
                self._set_operation_status(
                    f"[cyan]{self._service.t('tui.history_progress', position=position, total=total)} "
                    f"{subject_short}[/cyan]"
                )
                try:
                    fresh = await self._service.process_mail(mail, force=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    async with lock:
                        failed.append(f"{mail.subject[:40]}: {error_detail(self._service, exc)}")
                    return
                async with lock:
                    if fresh is not None and any(
                        note.status == "failed" for note in fresh.processor_notes
                    ):
                        # the run itself failed (rate limit, timeout): the record
                        # keeps its previous analysis, so report the mail instead
                        # of counting it as a success
                        note = next(
                            note for note in fresh.processor_notes if note.status == "failed"
                        )
                        failed.append(
                            f"{mail.subject[:40]}: {error_detail(self._service, note.message)}"
                        )
                        return
                    done += 1

        if self.is_mounted:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(*(_analyze(mail) for mail in mails))
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.refresh_mail()
        if self._reparse_stop:
            self._set_operation_status(
                f"[yellow]{self._service.t('tui.reparse_stopped', count=done, total=total)}[/yellow]"
            )
        elif failed:
            detail = "; ".join(failed[:3])
            more = f" (+{len(failed) - 3})" if len(failed) > 3 else ""
            self._set_operation_status(
                f"[red]{self._service.t('tui.history_failed', count=len(failed))}: "
                f"{escape(detail)}{more}[/red]"
            )
        else:
            self._set_operation_status(
                f"[green]{self._service.t('tui.history_reanalyzed', count=done)}[/green]"
            )

    async def _reparse_failed(self) -> None:
        try:
            failed_records = await self._service.list_failed_mails()
            if not failed_records:
                self._set_operation_status(
                    f"[green]{self._service.t('tui.reparse_none_failed')}[/green]"
                )
                return
            # immediate feedback before the first LLM call: each re-analysis can
            # take seconds (and may rate-limit), so without this the bulk
            # action looks like it never started
            self._set_operation_status(
                f"[cyan]{self._service.t('tui.reparse_failed_start', count=len(failed_records))}[/cyan]"
            )
            await self._reparse_batch([record.mail for record in failed_records])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # remote mode, provider failures
            self._report_bulk_failure(exc)

    # every re-analysis button: the one that starts a run becomes its cancel
    _REPARSE_BUTTONS: ClassVar[dict[str, str]] = {
        "btn-reparse": "tui.btn_reparse",
        "btn-reparse-failed": "tui.btn_reparse_failed",
        "btn-reparse-all": "tui.btn_reparse_all",
    }

    def _reparse_active(self) -> bool:
        """Whether a bulk re-analysis worker is still alive.

        Derived from the worker rather than a hand-set flag: a cancelled or
        superseded run used to leave a latched "running" state, which turned
        the button into a stop control that could never start a run again.
        """
        worker = self._reparse_worker
        return bool(worker is not None and not worker.is_finished)

    def _set_reparse_buttons(self, running_button: str | None) -> None:
        """Label the active button as its cancel control and park the others.

        Only one re-analysis may be in flight: a second one would supersede the
        running worker (same exclusive group) and silently drop its work.
        """
        for button_id, key in self._REPARSE_BUTTONS.items():
            button = self.query_one_optional(f"#{button_id}", Button)
            if button is None:
                continue
            if running_button == button_id:
                button.label = self._service.t("tui.reparse_stop")
                button.variant = "error"
                button.disabled = False
            else:
                button.label = self._service.t(key)
                button.variant = "primary"
                button.disabled = running_button is not None
                table = self._mail_table()
                if (
                    button_id == "btn-reparse"
                    and running_button is None
                    and table is not None
                    and table.checked_keys
                ):
                    button.label = self._service.t(
                        "tui.btn_reparse_selected", count=len(table.checked_keys)
                    )
        if running_button is None:
            self._sync_selection_controls(reparse_idle=True)

    def _start_reparse(self, button_id: str, action: Any) -> None:
        """Run one re-analysis and always restore the buttons afterwards.

        The lifecycle lives here, not in each job: whichever way the job ends
        (finished, stopped, cancelled, raised), the button that became the
        cancel control must turn back into its run label, or the user is left
        with a control that can never start a run again.
        """

        async def _run() -> None:
            try:
                await action
            finally:
                self._reparse_stop = False
                self._set_reparse_buttons(None)

        self._reparse_stop = False
        self._set_reparse_buttons(button_id)
        self._reparse_worker = self.run_worker(
            _run(), exclusive=True, group="mail-reparse", exit_on_error=False
        )

    def _stop_reparse(self) -> None:
        """End the running re-analysis now.

        Cancelling the worker is what makes this immediate: setting a flag only
        would leave the UI in "cancelling" until the in-flight request hit its
        own timeout, which is exactly what made the control look stuck.
        """
        self._reparse_stop = True
        worker = self._reparse_worker
        if worker is not None and not worker.is_finished:
            worker.cancel()
        self._set_operation_status(f"[yellow]{self._service.t('tui.reparse_stopping')}[/yellow]")

    async def _reparse_one(self) -> None:
        """Re-analyze the selected mail, with the same run/cancel controls."""
        try:
            targets = self._mail_targets()
            if not targets:
                self._set_operation_status(
                    f"[yellow]{self._service.t('tui.history_none_selected')}[/yellow]"
                )
                return
            records = [
                record
                for ident in targets
                if (record := await self._service.get_mail(ident)) is not None
            ]
            if not records:
                self._set_operation_status(
                    f"[yellow]{self._service.t('tui.mail_no_match')}[/yellow]"
                )
                return
            if len(records) > 1 and not await self._confirm(
                title=self._service.t("tui.btn_reparse_selected", count=len(records)),
                body=self._service.t("tui.reparse_selected_body", count=len(records))
                + "\n\n"
                + self._mail_preview(records),
                confirm_label=self._service.t("tui.btn_reparse"),
            ):
                return
            subject_short = escape((records[0].mail.subject or "")[:36])
            self._set_operation_status(
                f"[cyan]{self._service.t('tui.reparse_one_start', subject=subject_short)}[/cyan]"
            )
            await self._reparse_batch([record.mail for record in records])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._report_bulk_failure(exc)

    async def _reparse_all(self) -> None:
        """Re-analyze every stored mail, after stating the real count.

        Runs until it finishes or the user presses the button again to stop;
        the button is the stop control while the job is in flight. The press
        handler is the only entry point and decides start-vs-stop, so this
        method must not re-check whether a worker is active: its own worker
        already exists by the time it runs.
        """
        try:
            records = await self._service.list_mails()
            if not records:
                self._set_operation_status(f"[yellow]{self._service.t('tui.mail_empty')}[/yellow]")
                return
            confirmed = await self._confirm(
                title=self._service.t("tui.reparse_all_title"),
                body=self._service.t("tui.reparse_all_body", count=len(records)),
                confirm_label=self._service.t("tui.btn_reparse_all"),
            )
            if not confirmed:
                return
            self._set_operation_status(
                f"[cyan]{self._service.t('tui.reparse_all_start', count=len(records))}[/cyan]"
            )
            await self._reparse_batch([record.mail for record in records])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # remote mode, provider failures
            self._report_bulk_failure(exc)

    def _start_trash(self, operation: Callable[[], Awaitable[None]]) -> None:
        self._trash_busy = True
        self._sync_selection_controls()

        async def run() -> None:
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._report_bulk_failure(exc)
            finally:
                self._trash_busy = False
                self._sync_selection_controls()

        cast(MailFlowApp, self.app).run_worker(  # pyright: ignore[reportUnknownMemberType]
            run(), exclusive=True, group="mail-trash", exit_on_error=False
        )

    def _mail_preview(self, records: list[MailRecord]) -> str:
        lines = [
            f"• {r.mail.subject or self._service.t('tui.mail_no_subject')} — {r.mail.sender.display} ({r.mail.account_id})"
            for r in records[:20]
        ]
        if len(records) > 20:
            lines.append(self._service.t("tui.selection_more", count=len(records) - 20))
        return "\n".join(lines)

    async def _trash_selected(self) -> None:
        from mailflow_tui.mail_workflows import TrashChoiceScreen

        targets = self._mail_targets()
        if not targets:
            return
        records = [
            record
            for ident in targets
            if (record := await self._service.get_mail(ident)) is not None
        ]
        linked = [
            item for item in await self._service.list_actions_all() if item.mail_id in targets
        ]
        delete_actions = False
        if linked:
            choice = await self._ask_screen(
                TrashChoiceScreen(self._service, self._mail_preview(records))
            )
            if choice is None:
                return
            delete_actions = bool(choice["delete_actions"])
        else:
            table = self._mail_table()
            hidden = table is not None and bool(set(targets) - set(table.visible_keys()))
            if (len(targets) > 1 or hidden) and not await self._confirm(
                title=self._service.t("tui.btn_delete_selected", count=len(targets)),
                body=self._service.t("tui.mail_delete_body", count=len(targets))
                + "\n\n"
                + self._mail_preview(records),
                confirm_label=self._service.t("tui.btn_trash"),
            ):
                return
        await self._trash_records(targets, delete_actions=delete_actions)

    async def _trash_records(
        self, targets: list[str], *, expired: bool = False, delete_actions: bool = False
    ) -> None:
        moved: list[str] = []
        errors: dict[str, str] = {}
        try:
            for index, ident in enumerate(targets, 1):
                self._set_operation_status(
                    self._service.t("tui.batch_progress", done=index, total=len(targets))
                )
                try:
                    if expired:
                        changed = await self._service.purge_expired_mails([ident])
                    else:
                        result = await self._service.batch_mail(
                            [ident], delete_actions=delete_actions
                        )
                        if result.failed:
                            errors.update(result.failed)
                        changed = bool(result.succeeded)
                    if changed:
                        moved.append(ident)
                except Exception as exc:
                    errors[ident] = error_message(self._service, exc)
        finally:
            if moved:
                self._undo_trash_ids = moved
            table = self._mail_table()
            if table is not None:
                table.set_checks(set(errors))
            await self.refresh_mail()
            message = self._service.t("tui.mail_delete_done", count=len(moved))
            if errors:
                message += "\n" + self._service.t(
                    "tui.batch_failed", count=len(errors), error=next(iter(errors.values()))
                )
            self._set_operation_status(escape(message))

    def action_undo_trash(self) -> None:
        if self._trash_busy or not self._undo_trash_ids:
            return
        self._start_trash(self._undo_trash)

    async def _undo_trash(self) -> None:
        restored = 0
        failed: list[str] = []
        error = ""
        for ident in self._undo_trash_ids:
            try:
                if await self._service.restore_mail(ident) is not None:
                    restored += 1
            except Exception as exc:
                failed.append(ident)
                error = error_message(self._service, exc)
        self._undo_trash_ids = failed
        await self.refresh_mail()
        message = self._service.t("tui.mail_restore_done", count=restored)
        if failed:
            message += "\n" + self._service.t("tui.batch_failed", count=len(failed), error=error)
        self._set_operation_status(escape(message))

    async def _purge_expired(self) -> None:
        """Move mail that has nothing left to act on to the trash."""
        try:
            expired = await self._service.list_expired_mails()
            if not expired:
                self._set_operation_status(
                    f"[green]{self._service.t('tui.purge_expired_none')}[/green]"
                )
                return
            confirmed = await self._confirm(
                title=self._service.t("tui.purge_expired_title"),
                body=self._service.t(
                    "tui.purge_expired_body",
                    count=len(expired),
                    ad=sum(1 for record in expired if record.effective_urgency is Urgency.AD),
                    info=sum(1 for record in expired if record.effective_urgency is Urgency.INFO),
                )
                + "\n\n"
                + self._mail_preview(expired),
                confirm_label=self._service.t("tui.btn_purge_expired"),
            )
            if not confirmed:
                return
            # exactly the records the dialog listed; the service re-verifies
            # each one against the state it holds at delete time
            await self._trash_records([record.record_id for record in expired], expired=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # remote mode, storage failures
            self._report_bulk_failure(exc)

    def _report_bulk_failure(self, exc: BaseException) -> None:
        """A bulk action that cannot run says so instead of doing nothing."""
        self._set_operation_status(f"[red]{escape(error_message(self._service, exc))}[/red]")

    async def _edit_profile(self) -> None:
        """Open the recipient-profile form; the text personalizes analysis."""
        try:
            profile = await self._service.user_profile()
            guidelines = await self._service.feedback_guidelines()
            saved = await self._ask_screen(UserProfileModal(self._service, profile, guidelines))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._report_bulk_failure(exc)
            return
        if not saved:
            return
        self._set_operation_status(f"[green]{self._service.t('tui.profile_saved')}[/green]")

    async def _confirm(self, *, title: str, body: str, confirm_label: str) -> bool:
        """Ask before a destructive or expensive bulk action.

        Focus starts on Cancel: these are the buttons whose Enter would
        destroy data (clear expired mail, wipe the mailbox), so the safe
        default has to be the one that does nothing.
        """
        result = await self._ask_screen(
            ConfirmModal(
                self._service,
                title=title,
                body=body,
                confirm_label=confirm_label,
                focus_confirm=False,
            )
        )
        return bool(result)

    async def _ask_screen(self, screen: Any) -> Any:
        """Push a modal from a worker and await its result.

        ``push_screen`` normally takes a callback; the bulk actions need the
        answer inline, so the worker owns a future the callback completes.
        Cancellation (the pane was remounted by a language switch) dismisses
        the modal instead of leaving a dead dialog that still accepts clicks.
        """
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def _done(value: Any) -> None:
            if not future.done():
                future.set_result(value)

        app = cast(MailFlowApp, self.app)  # pyright: ignore[reportUnknownMemberType]
        app.push_screen(screen, _done)
        try:
            return await future
        except asyncio.CancelledError:
            if screen.is_current:
                screen.dismiss(None)
            raise

    def select_mail(self, mail_id: str) -> None:
        search = self.query_one_optional("#mail-search", Input)
        if search is not None:
            search.value = ""
        urgency = _typed_select(self, "#mail-urgency-filter")
        if urgency is not None:
            _safe_set_value(urgency, "all")
        self._selected_id = mail_id
        self.run_worker(
            self.refresh_mail(), exclusive=True, group="mail-refresh", exit_on_error=False
        )


class ActionsPane(Vertical):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._items: list[ActionItem] = []
        self._actions_loaded = False
        # refresh_actions runs from the mount worker, filter changes and
        # reload-all; concurrent clear+add_row passes interleave into
        # DuplicateKeys — serialize them
        self._refresh_lock = asyncio.Lock()
        self._actions_filter_generation = 0
        self._actions_matcher = SearchMatcher("")
        self._delete_busy = False
        self._action_status = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="actions-search-row"):
            yield Input(
                placeholder=self._service.t("tui.actions_search_placeholder"), id="actions-search"
            )
            yield SearchToggle("regex", ident="actions-search-regex")
            yield SearchToggle("case", ident="actions-search-case")
        with Horizontal(id="actions-controls"):
            yield SafeSelect(
                [
                    (self._service.t("ux.range_open"), "all"),
                    (self._service.t("tui.range_today"), "today"),
                    (self._service.t("tui.range_week"), "week"),
                    (self._service.t("tui.filter_expired"), "expired"),
                ],
                id="actions-range",
                allow_blank=False,
            )
            yield SafeSelect(
                [(self._service.t("tui.filter_all_types"), "all")],
                id="actions-type-filter",
                allow_blank=False,
            )
        yield SelectionTable(t=self._service.t, id="actions-table")
        with Horizontal(id="actions-buttons"):
            yield Button(
                self._service.t("tui.btn_add_todo"),
                id="actions-add",
                variant="primary",
            )
            yield Button(
                self._service.t("tui.btn_edit"),
                id="actions-edit",
                variant="primary",
                disabled=True,
            )
            yield Button(self._service.t("ux.task_operations"), id="actions-status", disabled=True)
            yield Button(
                self._service.t("tui.btn_delete_todo"),
                id="actions-delete",
                variant="error",
                disabled=True,
            )
        yield Button(self._service.t("assistant.handoff"), id="actions-assistant")
        yield Static(self._service.t("tui.empty"), id="actions-hint")

    async def on_mount(self) -> None:
        self.run_worker(
            self.refresh_actions(),
            exclusive=True,
            group="actions-refresh",
            exit_on_error=False,
        )

    def _actions_table(self) -> SelectionTable | None:
        return self.query_one_optional("#actions-table", SelectionTable)

    def _ensure_columns(self) -> None:
        if getattr(self, "_columns_done", False):
            return
        table = self._actions_table()
        if table is None:
            return
        for key in ("selected", "time", "type", "content", "notes", "source"):
            _remove_column(table, key)
        table.add_column(RichText("[ ]"), key="selected", width=3)
        table.add_column(self._service.t("tui.action_time"), key="time")
        table.add_column(self._service.t("tui.action_type"), key="type")
        table.add_column(self._service.t("tui.action_content"), key="content")
        table.add_column(self._service.t("tui.action_notes"), key="notes")
        table.add_column(self._service.t("tui.action_source"), key="source")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        table.tooltip = self._service.t("tui.selection_help")
        self._columns_done = True

    async def relabel(self) -> None:
        self._columns_done = False
        _relabel_buttons(
            self,
            {
                "actions-add": "tui.btn_add_todo",
                "actions-edit": "tui.btn_edit",
                "actions-delete": "tui.btn_delete",
            },
        )
        search = self.query_one_optional("#actions-search", Input)
        if search is not None:
            search.placeholder = self._service.t("tui.actions_search_placeholder")
        _apply_options(
            self,
            "#actions-range",
            [
                (self._service.t("ux.range_open"), "all"),
                (self._service.t("tui.range_today"), "today"),
                (self._service.t("tui.range_week"), "week"),
                (self._service.t("tui.filter_expired"), "expired"),
            ],
        )
        await self.refresh_actions()

    async def refresh_actions(self, *, reload: bool = True) -> None:
        async with self._refresh_lock:
            try:
                await self._refresh_actions_unlocked(reload=reload)
            except Exception as exc:
                hint = self.query_one_optional("#actions-hint", Static)
                if hint is not None:
                    hint.update(_load_error(self._service, exc))

    async def _refresh_actions_unlocked(self, *, reload: bool = True) -> None:
        table = self._actions_table()
        if table is None:
            return
        loaded = (
            await self._service.list_actions_all(preview_days=30)
            if reload or not self._actions_loaded
            else self._items
        )
        self._actions_loaded = True
        self._items = loaded
        search = self.query_one_optional("#actions-search", Input)
        regex = self.query_one_optional("#actions-search-regex", SearchToggle)
        case = self.query_one_optional("#actions-search-case", SearchToggle)
        matcher = build_search_matcher(
            search.value if search is not None else "",
            regex=bool(regex.value) if regex is not None else False,
            case_sensitive=bool(case.value) if case is not None else False,
        )
        if matcher.error:
            hint = self.query_one_optional("#actions-hint", Static)
            if hint is not None:
                hint.update(
                    f"[red]{self._service.t('tui.invalid_regex')}: {escape(self._service.t('errors.invalid_value'))}[/red]"
                )
            return
        self._actions_matcher = matcher
        # a refresh must not throw the user out of the list they were reading:
        # remember the cursor row and scroll offset, restore them below when
        # the row still exists
        view = TableView.capture(table)
        table.clear()
        self._ensure_columns()
        table.unselectable_keys = {item.item_id for item in loaded if item.preview}
        table.retain_checks({item.item_id for item in loaded if not item.preview})
        items = list(self._items)
        types = sorted({item.action_type for item in self._items})
        type_filter = self._select_value("#actions-type-filter")
        if type_filter not in {"all", *types}:
            type_filter = "all"
        if type_filter != "all":
            items = [item for item in items if item.action_type == type_filter]
        range_mode = self._select_value("#actions-range")
        items = (
            [
                item
                for item in items
                if (
                    item.completed_at is not None
                    if range_mode == "completed"
                    else item.completed_at is None
                )
            ]
            if range_mode != "everything"
            else items
        )
        if range_mode == "expired":
            items = [item for item in items if self._service.is_action_expired(item)]
        elif range_mode not in {"all", "completed", "everything"}:
            from datetime import timedelta
            from zoneinfo import ZoneInfo

            now = datetime.now(UTC)
            if range_mode == "today":
                zone = ZoneInfo(self._service.config.general.timezone)
                local_start = now.astimezone(zone).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                lower = local_start.astimezone(UTC)
                upper = (local_start + timedelta(days=1)).astimezone(UTC)
            else:
                lower = now
                upper = now + timedelta(days=7)
            items = [
                item
                for item in items
                if item.due_at is not None
                and to_utc(item.due_at) < upper
                and (to_utc(item.due_end or item.due_at) >= lower)
            ]
        if matcher.query:
            items = [
                item
                for item in items
                if matcher.matches(
                    f"{item.summary} {item.action_type} {item.notes} {item.location} {item.url} {item.category} {' '.join(item.tags)}"
                )
            ]
        _apply_options(
            self,
            "#actions-type-filter",
            [(self._service.t("tui.filter_all_types"), "all")]
            + [
                (_action_type_label(self._service, action_type), action_type)
                for action_type in types
            ],
        )
        current = self._select_value("#actions-type-filter")
        if current not in {"all", *types}:
            # stale selection vanished from the list: reset without recursing
            type_select = _typed_select(self, "#actions-type-filter")
            if type_select is not None and _select_ready(type_select):
                selected_value = type_select.value
                if selected_value is not Select.NULL and str(selected_value) != "all":
                    _safe_set_value(type_select, "all")
        hint = self.query_one_optional("#actions-hint", Static)
        if hint is not None:
            hint.update(self._service.t("tui.empty") if not items else _BLANK)
        for position, item in enumerate(items, start=1):
            # an entry that is over is styled dim instead of being hidden: the
            # user still needs to see what happened, but must not mistake it
            # for something still ahead
            style = "dim" if item.preview or self._service.is_action_expired(item) else ""
            table.add_row(
                RichText("·", style="dim") if item.preview else table.check_label(item.item_id),
                RichText(_action_time_range(self._service, item), style=style),
                RichText(_action_type_label(self._service, item.action_type), style=style),
                RichText(
                    (self._service.t("recurrence.preview") + " · " if item.preview else "")
                    + item.summary,
                    style=style,
                ),
                RichText(
                    " · ".join(filter(None, [item.category, ", ".join(item.tags), item.notes]))
                    or "-",
                    style=style,
                ),
                RichText(item.source_subject or item.mail_id, style=style),
                key=item.item_id,
            )
            if position % 50 == 0:
                await asyncio.sleep(0)
        view.restore(table)
        self._sync_action_controls()

    def on_selection_table_selection_changed(self, event: SelectionTable.SelectionChanged) -> None:
        if event.table.id == "actions-table":
            self._sync_action_controls()

    def _sync_action_controls(self) -> None:
        table = self._actions_table()
        if table is None:
            return
        count = len(table.checked_keys)
        current = self._selected_item()
        real = bool(current and not current.preview)
        button = self.query_one_optional("#actions-delete", Button)
        if button is not None:
            button.label = (
                self._service.t("tui.btn_delete_selected", count=count)
                if count
                else self._service.t("tui.btn_delete_todo")
            )
            button.disabled = self._delete_busy or (not real and not count)
            button.tooltip = self._service.t("tui.selection_help")
        table.disabled = self._delete_busy
        edit = self.query_one_optional("#actions-edit", Button)
        if edit is not None:
            edit.tooltip = self._service.t("tui.selection_current_help")
            edit.disabled = self._delete_busy or self._selected_item() is None
        status = self.query_one_optional("#actions-status", Button)
        if status is not None:
            status.disabled = self._delete_busy or not (count or real)
        hint = self.query_one_optional("#actions-hint", Static)
        if hint is not None:
            message = self._action_status
            if count:
                hidden = count - len(table.checked_keys & set(table.visible_keys()))
                message += "\n" + self._service.t("tui.selection_count", count=count, hidden=hidden)
            elif not table.row_count and not message:
                message = self._service.t("tui.empty")
            hint.update(message.strip())

    def _select_value(self, selector: str) -> str:
        select = _typed_select(self, selector)
        if select is None or select.value is Select.NULL:
            return "all"
        return str(select.value)

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in ("actions-type-filter", "actions-range"):
            await self.refresh_actions(reload=False)

    def _schedule_actions_filter(self) -> None:
        self._actions_filter_generation += 1
        generation = self._actions_filter_generation

        async def _filter() -> None:
            await asyncio.sleep(0.3)
            if generation == self._actions_filter_generation:
                await self.refresh_actions(reload=False)

        self.run_worker(
            cast(Any, _filter), exclusive=True, group="actions-filter", exit_on_error=False
        )

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "actions-search":
            self._schedule_actions_filter()

    def on_search_toggle_toggled(self, event: SearchToggle.Toggled) -> None:
        if event.toggle.id in {"actions-search-regex", "actions-search-case"}:
            self._schedule_actions_filter()

    async def on_data_table_row_selected(self, event: TableRowEvent) -> None:
        if event.data_table.id != "actions-table":
            return
        item = next((i for i in self._items if i.item_id == event.row_key.value), None)
        if item is None:
            return

        def _after_action(changed: bool | None) -> None:
            if changed:
                self.run_worker(
                    self.refresh_actions(),
                    exclusive=True,
                    group="actions-refresh",
                    exit_on_error=False,
                )

        # textual types Widget.app loosely; the cast pins the concrete app
        cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
            ActionModal(self._service, item), callback=_after_action
        )

    def _selected_item(self) -> ActionItem | None:
        """The row under the cursor, by the same rule Delete uses."""
        table = self._actions_table()
        if table is None:
            return None
        row_index = table.cursor_row
        if row_index < 0 or row_index >= table.row_count:
            return None
        from textual.coordinate import Coordinate

        row_key = table.coordinate_to_cell_key(Coordinate(row_index, 0)).row_key
        return next((item for item in self._items if item.item_id == str(row_key.value)), None)

    def _refresh_after_edit(self, changed: bool | None) -> None:
        if changed:
            self.run_worker(
                self.refresh_actions(),
                exclusive=True,
                group="actions-refresh",
                exit_on_error=False,
            )

    def edit_selected(self) -> None:
        """Open the edit form for the selected todo (no-op without a row)."""
        item = self._selected_item()
        if item is None:
            return
        cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
            TodoEditModal(self._service, item), callback=self._refresh_after_edit
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        try:
            await self._handle_button_pressed(event)
        except Exception as exc:
            message = f"[red]{escape(error_message(self._service, exc))}[/red]"
            self.query_one("#actions-hint", Static).update(message)

    async def _handle_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "actions-assistant":
            table = self._actions_table()
            ids = list(table.checked_keys) if table is not None and table.checked_keys else []
            item = self._selected_item()
            if not ids and item is not None and not item.preview:
                ids = [item.item_id]
            cast(MailFlowApp, self.app).open_assistant(  # pyright: ignore[reportUnknownMemberType]
                {"action_ids": ids or [a.item_id for a in self._items if not a.preview]},
                self._service.t("assistant.task_scope"),
            )
            return

        if event.button.id == "actions-status":
            if self._delete_busy:
                return
            self._delete_busy = True
            self._sync_action_controls()
            self.run_worker(
                self._run_task_status(),
                exclusive=True,
                group="actions-status",
                exit_on_error=False,
            )
            return
        if event.button.id == "actions-edit":
            self.edit_selected()
            return
        if event.button.id == "actions-add":

            def _refresh_after_create(_result: bool | None) -> None:
                self.run_worker(
                    self.refresh_actions(),
                    exclusive=True,
                    group="actions-refresh",
                    exit_on_error=False,
                )

            cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                TodoCreateModal(self._service),
                callback=_refresh_after_create,
            )
            return
        if event.button.id != "actions-delete":
            return
        if self._delete_busy:
            return
        table = self._actions_table()
        targets = sorted(table.checked_keys) if table is not None else []
        item = self._selected_item()
        if not targets and item is not None:
            targets = [item.item_id]
        targets = [
            identity
            for identity in targets
            if any(item.item_id == identity and not item.preview for item in self._items)
        ]
        if not targets:
            return
        self._delete_busy = True
        self._sync_action_controls()
        cast(MailFlowApp, self.app).run_worker(  # pyright: ignore[reportUnknownMemberType]
            self._delete_actions(targets),
            exclusive=True,
            group="actions-delete",
            exit_on_error=False,
        )

    async def _run_task_status(self) -> None:
        try:
            await self._change_task_status()
        finally:
            self._delete_busy = False
            if self.is_mounted:
                self._sync_action_controls()

    async def _change_task_status(self) -> None:
        from datetime import timedelta

        table = self._actions_table()
        item = self._selected_item()
        ids = (
            sorted(table.checked_keys)
            if table and table.checked_keys
            else ([item.item_id] if item else [])
        )
        ids = [
            identity
            for identity in ids
            if any(item.item_id == identity and not item.preview for item in self._items)
        ]
        if not ids:
            return
        app = cast("Any", self).app
        choice = await app.push_screen_wait(TaskMenu(self._service))
        if not choice:
            return
        values: dict[str, Any] = {}
        if choice == "complete_tasks":
            values["completed"] = True
        elif choice in {"mute_tasks", "enable_tasks"}:
            values["reminder_enabled"] = choice == "enable_tasks"
        else:
            values.update(
                snoozed_until=datetime.now(UTC)
                + timedelta(hours=1 if choice == "snooze_hour" else 24),
                reminder_enabled=True,
            )
        failed: set[str] = set()
        success = 0
        for item_id in ids:
            try:
                await self._service.edit_action(item_id, **values)
                success += 1
            except Exception as exc:
                logger.warning("task update %s: %s", item_id, exc)
                failed.add(item_id)
        if table is not None:
            table.set_checks(failed)
        self._action_status = self._service.t(
            "ux.batch_result", success=success, failed=len(failed), skipped=0
        )
        await self.refresh_actions()
        self.query_one("#actions-hint", Static).update(self._action_status)

    async def _delete_actions(self, targets: list[str]) -> None:
        try:
            if len(targets) > 1:
                preview = "\n".join(
                    f"• {item.summary}" for item in self._items if item.item_id in targets
                )
                confirmed = await cast(MailFlowApp, self.app).push_screen_wait(  # pyright: ignore[reportUnknownMemberType]
                    ConfirmModal(
                        self._service,
                        title=self._service.t("tui.btn_delete_selected", count=len(targets)),
                        body=self._service.t("tui.actions_delete_body", count=len(targets))
                        + "\n\n"
                        + preview,
                        confirm_label=self._service.t("tui.btn_delete"),
                        focus_confirm=False,
                    )
                )
                if not confirmed:
                    return
            deleted = 0
            errors: dict[str, str] = {}
            for index, ident in enumerate(targets, 1):
                self._action_status = self._service.t(
                    "tui.batch_progress", done=index, total=len(targets)
                )
                self._sync_action_controls()
                try:
                    deleted += bool(await self._service.delete_action(ident))
                except Exception as exc:
                    errors[ident] = error_message(self._service, exc)
            table = self._actions_table()
            if table is not None:
                table.set_checks(set(errors))
            self._action_status = self._service.t("tui.actions_delete_done", count=deleted)
            if errors:
                self._action_status += "\n" + self._service.t(
                    "tui.batch_failed", count=len(errors), error=next(iter(errors.values()))
                )
            await self.refresh_actions()
        except Exception as exc:
            self._action_status = error_message(self._service, exc)
        finally:
            self._delete_busy = False
            self._sync_action_controls()


class RuntimePane(Vertical):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._selected_plugin: str | None = None
        self._detail_open_for: str | None = None
        self._refresh_lock = asyncio.Lock()
        self._load_failed = False

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="runtime-scroll"):
            yield Static(self._runtime_title(), id="runtime-plugins")
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
        _relabel_buttons(
            self,
            {
                "runtime-plugin-disable": "tui.btn_disable",
                "runtime-plugin-enable": "tui.btn_enable",
                "runtime-plugin-uninstall": "tui.btn_uninstall",
            },
        )
        self.update_connection_status()
        self._set_static("#runtime-plugins-hint", self._service.t("tui.runtime_plugins_help"))
        await self.refresh_runtime()

    def _runtime_title(self) -> str:
        title = self._service.t("tui.runtime_plugins")
        status = remote_connection_status(self._service)
        return f"{title}\n{status}" if status else title

    def update_connection_status(self) -> None:
        self._set_static("#runtime-plugins", self._runtime_title())

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
            try:
                await self._refresh_runtime_unlocked()
                if self._load_failed:
                    self._set_static("#runtime-status", "")
                    self._load_failed = False
            except Exception as exc:
                self._load_failed = True
                self._set_static("#runtime-status", _load_error(self._service, exc))

    async def _refresh_runtime_unlocked(self) -> None:
        if getattr(self._service, "remote", False):
            await cast(Any, self._service).refresh_snapshot()
        snapshot = self._service.snapshot()
        service = self._service
        scroll = self.query_one_optional("#runtime-scroll", ScrollableContainer)
        offset = (scroll.scroll_x, scroll.scroll_y) if scroll is not None else (0.0, 0.0)
        table = self._plugins_table()
        if table is None:
            return
        view = TableView.capture(table)
        table.clear()
        self._ensure_columns()
        for plugin in snapshot.plugins:
            status_key = service.plugin_status(plugin.plugin_id)
            status_text = {
                "enabled": service.t("tui.plugin_status_enabled"),
                "disabled": service.t("tui.plugin_status_disabled"),
                "not_loaded": service.t("tui.plugin_status_not_loaded"),
                "error": service.t("tui.plugin_status_error"),
            }.get(status_key, status_key)
            table.add_row(
                escape(plugin.plugin_id),
                escape(plugin.name),
                ",".join(k.value for k in plugin.kinds) or "-",
                status_text,
                key=plugin.plugin_id,
            )
        self._selected_plugin = view.restore(table)
        adapters = "\n".join(
            escape(f"  {c.component_id} (plugin: {c.plugin_id})")
            for c in snapshot.components
            if c.kind.value == "mail_source"
        )
        accounts = "\n".join(
            escape(f"  {a.account_id}  {a.email}  " + service.t("sync.state_" + a.status))
            + "\n  "
            + escape(
                service.t(
                    "ux.sync_detail",
                    time=a.last_sync_at.astimezone(
                        ZoneInfo(service.config.general.timezone)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    if a.last_sync_at
                    else service.t("ux.sync_never"),
                    new=a.new_count,
                    pending=a.pending_count,
                )
            )
            + (
                "\n  "
                + escape(
                    service.t(
                        "sync.progress",
                        account=a.account_id,
                        received=a.sync_received,
                        total=a.sync_total,
                        pending=a.pending_count,
                        state=service.t("sync.state_" + a.status),
                    )
                )
                if a.sync_active
                else ""
            )
            + ("\n  " + escape(a.error) if a.error else "")
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
        if scroll is not None:
            scroll.call_after_refresh(
                lambda: scroll.scroll_to(x=offset[0], y=offset[1], animate=False, force=True)
            )

    async def on_data_table_row_selected(self, event: TableRowEvent) -> None:
        if event.data_table.id != "runtime-plugins-table":
            return
        plugin_id = cast(str, event.row_key.value)
        self._selected_plugin = plugin_id
        status = self._service.plugin_status(plugin_id)
        plugin = self._service.snapshot().plugin(plugin_id)
        detail = f"\n{plugin.error}" if plugin is not None and plugin.error else ""
        self.query_one("#runtime-status", Static).update(
            escape(f"{plugin_id} — {self._service.t(f'tui.plugin_status_{status}')}{detail}")
        )

    async def on_data_table_row_highlighted(self, event: TableRowEvent) -> None:
        if (
            event.data_table.id == "runtime-plugins-table"
            and event.cursor_row == event.data_table.cursor_row
        ):
            self._selected_plugin = str(event.row_key.value)

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
            status.update(f"[red]{escape(error_message(self._service, exc))}[/red]")
            return
        status.update(message)
        await self.refresh_runtime()


class LogsPane(Vertical):
    """Filterable log viewer.

    Buffers the last N formatted log lines and re-renders them through
    three filters: a minimum level (WARNING+ERROR by default, expandable
    to INFO or DEBUG), a source group (empty = all), and a substring
    search. The buffer is bounded so the pane never grows without limit.

    Sources are shown as user-facing categories (Chat, Mail, LLM, ...) in
    the user's language — raw logger names like ``mailflow.llm.openai``
    are plumbing detail nobody filters by.
    """

    # logger segment -> category key (first match wins; localized via
    # tui.logs_cat_*). Chat keywords come before notify so platform
    # notifiers (mailflow.notify.onebot) count as the chat bot they are.
    _SOURCE_CATEGORIES: ClassVar[dict[str, str]] = {
        "bot_server": "chat",
        "gateway": "chat",
        "onebot": "chat",
        "napcat": "chat",
        "openwechat": "chat",
        "wechatpadpro": "chat",
        "telegram": "chat",
        "whatsapp": "chat",
        "llm": "llm",
        "pipeline": "parse",
        "processor": "parse",
        "imap": "mail",
        "mail": "mail",
        "service": "mail",
        "notify": "notify",
        "reminder": "notify",
        "storage": "storage",
    }

    # platform-level sub-identifiers within the chat category: tag text
    # suffix and color, so NapCat/OpenWeChat lines are distinguishable
    _CHAT_PLATFORMS: ClassVar[dict[str, tuple[str, str]]] = {
        "napcat": ("napcat", "#F56C6C"),
        "openwechat": ("openwechat", "#E6A23C"),
        "wechatpadpro": ("wechatpadpro", "#67C23A"),
        "telegram": ("telegram", "#229ED9"),
        "whatsapp": ("whatsapp", "#25D366"),
    }

    @staticmethod
    def _category_of(logger_name: str) -> str:
        """User-facing category for a raw logger name; 'system' fallback.

        Matching walks the logger's dot segments, so nested names like
        ``mailflow.plugins.openwechat`` still classify by their keyword."""
        for segment in logger_name.split("."):
            category = LogsPane._SOURCE_CATEGORIES.get(segment)
            if category is not None:
                return category
        return "system"

    # category tag colors (contract palette reuse: chat=accent blue,
    # mail=info green, llm=important amber, parse=urgent red, notify=sys)
    _CATEGORY_STYLES: ClassVar[dict[str, str]] = {
        "chat": "#7EA7F8",
        "mail": "#67C23A",
        "llm": "#E6A23C",
        "parse": "#F56C6C",
        "notify": "#E6A23C",
        "storage": "#909399",
        "system": "#909399",
    }
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
    # INFO by default: the chat-command chain (received messages, replies)
    # logs at INFO, and WARNING-only would hide the exact lines a user
    # needs when diagnosing why a bot did not answer.
    _DEFAULT_MIN_LEVEL = "INFO"
    _MAX_LINES = 2000

    def __init__(self, service: MailFlowService, log_queue: queue_module.Queue[Any]) -> None:
        super().__init__()
        self._service = service
        self._log_queue = log_queue
        self._buffer: deque[str] = deque(maxlen=self._MAX_LINES)
        self._min_level = self._DEFAULT_MIN_LEVEL
        self._source = ""
        self._seen_sources: set[str] = set()
        self._source_signature = ""
        self._query = ""
        self._log_filter_generation = 0
        self._log_matcher = SearchMatcher("")
        self._rendered_logs: list[Any] = []
        # incremental rendering: newly pulled lines are appended to the
        # RichLog (capped by its own max_lines) instead of re-rendering the
        # whole 2000-line buffer every second — on slow terminals full
        # rebuilds stall the event loop. A full rebuild happens only when a
        # filter changes or the pane (re)mounts.

    def compose(self) -> ComposeResult:
        with Vertical(id="log-controls"):
            with Horizontal(id="log-filters"):
                yield SafeSelect(
                    [
                        (self._service.t("tui.logs_level_warning"), "WARNING"),
                        (self._service.t("tui.logs_level_info"), "INFO"),
                        (self._service.t("tui.logs_level_debug"), "DEBUG"),
                    ],
                    value=self._min_level,
                    id="log-level",
                    allow_blank=False,
                )
                yield SafeSelect(
                    [(self._service.t("tui.logs_all_sources"), "")],
                    id="log-source",
                    allow_blank=False,
                )
            with Horizontal(id="log-search-row"):
                yield Input(
                    placeholder=self._service.t("tui.logs_search_placeholder"),
                    id="log-search",
                )
                yield SearchToggle("regex", ident="log-search-regex")
                yield SearchToggle("case", ident="log-search-case")
                yield Button(
                    self._service.t("tui.logs_export"),
                    id="logs-export",
                    variant="default",
                )
        yield Static("", id="logs-search-status")
        with ScrollableContainer(id="log-scroll"):
            yield RichLog(id="log-view", wrap=True, highlight=True, max_lines=2000)

    async def on_mount(self) -> None:
        self.run_worker(
            cast(Any, self._render_logs), exclusive=True, group="logs-filter", exit_on_error=False
        )

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
        self._schedule_log_filter()

    def _set_level_options(self, level: Select[str]) -> None:
        current = level.value
        if not _select_ready(level):
            return
        try:
            level.set_options(
                [
                    (self._service.t("tui.logs_level_warning"), "WARNING"),
                    (self._service.t("tui.logs_level_info"), "INFO"),
                    (self._service.t("tui.logs_level_debug"), "DEBUG"),
                ]
            )
        except QueryError:
            return
        _safe_set_value(level, current)

    def _refresh_source_options(self) -> None:
        source = _typed_select(self, "#log-source")
        if source is None:
            return
        current = source.value
        pairs = [(self._service.t("tui.logs_all_sources"), "")] + [
            (self._service.t(f"tui.logs_cat_{name}"), name) for name in sorted(self._seen_sources)
        ]
        # drain() runs every second; poking Select.set_options with an
        # identical list re-renders the widget each tick — skip it
        signature = repr(pairs)
        if getattr(self, "_source_signature", None) != signature and _select_ready(source):
            try:
                source.set_options(pairs)  # pyright: ignore[reportUnknownMemberType]
            except QueryError:
                return
            self._source_signature = signature
        if current is not Select.NULL and current != "":
            _safe_set_value(source, current if current in self._seen_sources else "")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "logs-export":
            return
        from mailflow_tui.log_export import LogExportScreen

        def _on_saved(path: str | None) -> None:
            if path is None:
                return
            # confirm inside the log itself, at the saved buffer's level
            # so the confirmation is visible under the current filters
            stamp = __import__("datetime").datetime.now().strftime("%H:%M:%S")
            self._buffer.append(
                f"{stamp}|INFO|mailflow.tui|{self._service.t('tui.logs_exported', path=path)}"
            )
            self._seen_sources.add("tui")
            self._refresh_source_options()
            self._append_new_lines([self._buffer[-1]])

        cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
            LogExportScreen(self._service, list(self._buffer)),
            callback=_on_saved,
        )

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
                self._seen_sources.add(self._category_of(parts[2]))
        if not pulled:
            return
        self._refresh_source_options()
        self._append_new_lines(pulled)

    def _current_log_matcher(self) -> SearchMatcher:
        regex = self.query_one_optional("#log-search-regex", SearchToggle)
        case = self.query_one_optional("#log-search-case", SearchToggle)
        return build_search_matcher(
            self._query,
            regex=bool(regex.value) if regex is not None else False,
            case_sensitive=bool(case.value) if case is not None else False,
        )

    async def _restore_rendered_logs(self) -> None:
        log_view = self.query_one_optional("#log-view", RichLog)
        if log_view is None:
            return
        log_view.clear()
        for index, line in enumerate(self._rendered_logs, start=1):
            log_view.write(line)
            if index % 50 == 0:
                await asyncio.sleep(0)

    async def _render_logs(self, generation: int | None = None) -> None:
        generation = self._log_filter_generation if generation is None else generation
        matcher = self._current_log_matcher()
        status = self.query_one_optional("#logs-search-status", Static)
        if matcher.error:
            if status is not None:
                status.update(
                    f"[red]{self._service.t('tui.invalid_regex')}: {escape(self._service.t('errors.invalid_value'))}[/red]"
                )
            await self._restore_rendered_logs()
            return
        self._log_matcher = matcher
        if status is not None:
            status.update("")
        log_view = self.query_one_optional("#log-view", RichLog)
        if log_view is None:
            return
        rendered: list[Any] = []
        for index, line in enumerate(self._buffer, start=1):
            text = self._line_to_text(line)
            if text is not None:
                rendered.append(text)
            if index % 50 == 0:
                await asyncio.sleep(0)
                if generation != self._log_filter_generation:
                    return
        if generation != self._log_filter_generation:
            return
        previous = list(self._rendered_logs)
        log_view.clear()
        for index, line in enumerate(rendered, start=1):
            log_view.write(line)
            if index % 50 == 0:
                await asyncio.sleep(0)
                if generation != self._log_filter_generation:
                    self._rendered_logs = previous
                    await self._restore_rendered_logs()
                    return
        self._rendered_logs = rendered

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
        if self._query and not self._log_matcher.matches(message):
            return None
        from rich.text import Text

        category = self._category_of(logger_name)
        cat_label = self._service.t(f"tui.logs_cat_{category}")
        cat_style = self._CATEGORY_STYLES.get(category, "")
        # chat category: append the concrete platform (napcat/openwechat/...)
        # with its own color, so mixed-platform deployments are readable
        platform_suffix = ""
        if category == "chat":
            for segment in logger_name.split("."):
                if segment in self._CHAT_PLATFORMS:
                    platform_suffix = f"·{self._CHAT_PLATFORMS[segment][0]}"
                    cat_style = self._CHAT_PLATFORMS[segment][1]
                    break
        # Layout: HH:MM:SS LEVEL [Category·platform]  message — the tag is
        # color-coded per platform, the message indented two spaces past
        # the bracket for a clear visual hierarchy.
        text = Text(f"{time_part} ")
        text.append(f"{level:<7}", style=self._LEVEL_STYLES.get(level, "bold"))
        text.append(f" [{cat_label}{platform_suffix}]", style=cat_style)
        text.append(f"  {message}")
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

    def _schedule_log_filter(self) -> None:
        self._log_filter_generation += 1
        generation = self._log_filter_generation
        matcher = self._current_log_matcher()
        if matcher.error:
            status = self.query_one_optional("#logs-search-status", Static)
            if status is not None:
                status.update(
                    f"[red]{self._service.t('tui.invalid_regex')}: {escape(self._service.t('errors.invalid_value'))}[/red]"
                )
            self.run_worker(
                cast(Any, self._restore_rendered_logs),
                exclusive=True,
                group="logs-render",
                exit_on_error=False,
            )
            return

        async def _filter() -> None:
            await asyncio.sleep(0.3)
            if generation == self._log_filter_generation:

                async def _render() -> None:
                    await self._render_logs(generation)

                self.run_worker(
                    cast(Any, _render),
                    exclusive=True,
                    group="logs-render",
                    exit_on_error=False,
                )

        self.run_worker(
            cast(Any, _filter), exclusive=True, group="logs-filter", exit_on_error=False
        )

    def on_search_toggle_toggled(self, event: SearchToggle.Toggled) -> None:
        if event.toggle.id in {"log-search-regex", "log-search-case"}:
            self._schedule_log_filter()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "log-search":
            self._query = str(event.input.value)
            self._schedule_log_filter()

    def on_select_changed(self, event: Any) -> None:
        if event.select.id == "log-level":
            self._min_level = _event_select_value(event)
        elif event.select.id == "log-source":
            self._source = _event_select_value(event)
        else:
            return
        generation = self._log_filter_generation

        async def _render() -> None:
            await self._render_logs(generation)

        self.run_worker(
            cast(Any, _render),
            exclusive=True,
            group="logs-render",
            exit_on_error=False,
        )


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

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService, plugin: MarketPlugin) -> None:
        super().__init__()
        self._service = service
        self._plugin = plugin
        self._content_loaded = False
        self._bindings.bind("escape", "dismiss", self._service.t("tui.btn_close"))

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
        try:
            result = self._service.open_url(href)
        except RuntimeError as exc:
            self._set_status(escape(error_message(self._service, exc)))
            return
        link = self.query_one_optional("#market-detail-link", Static)
        if link is not None:
            link.update(f"[cyan]{escape(result)}[/cyan]")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "detail-close":
            self.dismiss(None)
            return
        plugin = self._plugin
        try:
            if button_id == "detail-install":
                await self._service.plugin_install(plugin.id)
                message_key = "plugin.installed_ok"
            elif button_id == "detail-uninstall":
                await self._service.plugin_uninstall(plugin.id)
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
            self._set_status(escape(error_message(self._service, exc)))
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
        self._market_filter_generation = 0
        self._market_matcher = SearchMatcher("")
        self._market_stale_status = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="market-controls"):
            with Horizontal(id="market-search-row"):
                yield Input(
                    placeholder=self._service.t("tui.market_search_placeholder"),
                    id="market-search",
                )
                yield SearchToggle("regex", ident="market-search-regex")
                yield SearchToggle("case", ident="market-search-case")
            with Horizontal(id="market-controls-top"):
                yield SafeSelect([], id="market-category")
                yield SafeSelect(
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
        try:
            cached = await self._service.market_cache_load()
        except Exception:
            cached = []
        if cached:
            self._entries = list(cached)
            self._installed = {}
            stale_names = sorted(
                {
                    repo.name
                    for repo, _plugin in self._entries
                    if repo.name != "local" and (repo.stale or not repo.url)
                }
            )
            if stale_names:
                self._market_stale_status = f"[yellow]{self._service.t('tui.market_stale')}: {escape(', '.join(stale_names))}[/yellow]"
            await self._render_entries()
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
        await self._render_entries()

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
        if config_status == "error":
            return self._service.t("tui.plugin_status_error")
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
            report = await self._service.market_fetch_report()
            fresh = list(report.entries)
            failures = list(report.failures)
        except AttributeError:
            try:
                fresh = await asyncio.to_thread(market.list_plugins)
                failures = []
            except Exception as exc:
                self._loading = False
                self._set_status(escape(error_message(self._service, exc)))
                return
        except Exception as exc:
            self._loading = False
            self._set_status(escape(error_message(self._service, exc)))
            return
        app = cast(MailFlowApp, cast(Any, self).app)
        local_repo = Repository("local", "")
        local_entries: list[tuple[Repository, MarketPlugin]] = []
        seen = {plugin.id for _repo, plugin in fresh}
        for info in self._service.plugin_manager.enabled_infos():
            if info.plugin_id in seen:
                continue
            local = app.local_plugin_entry(info.plugin_id)
            if local is not None:
                local.readme = plugin_doc_readme(info)
                local_entries.append((local_repo, local))
                seen.add(info.plugin_id)
        self._entries = [*fresh, *local_entries]
        self._installed = {}
        self._loading = False
        app.set_plugin_entries(self._entries)
        await self._service.market_cache_save(self._entries)
        if failures:
            detail = "; ".join(f"{repo.name}: {reason}" for repo, reason in failures)
            self._market_stale_status = (
                f"[yellow]{self._service.t('tui.market_stale')}: {escape(detail)}[/yellow]"
            )
        else:
            self._market_stale_status = ""
        await self._render_entries()

    async def _render_entries(self) -> None:
        """Render the cached entries through the current search/category."""
        table = self._market_table()
        if table is None:
            return
        self._ensure_columns()
        category = self.query_one_optional("#market-category", Select)  # pyright: ignore[reportUnknownVariableType]
        filter_value = cast(str, category.value or "all") if category is not None else "all"  # pyright: ignore[reportUnknownMemberType]
        search = self.query_one_optional("#market-search", Input)
        regex = self.query_one_optional("#market-search-regex", SearchToggle)
        case = self.query_one_optional("#market-search-case", SearchToggle)
        matcher = build_search_matcher(
            search.value if search is not None else "",
            regex=bool(regex.value) if regex is not None else False,
            case_sensitive=bool(case.value) if case is not None else False,
        )
        if matcher.error:
            self._set_status(
                f"[red]{self._service.t('tui.invalid_regex')}: {escape(self._service.t('errors.invalid_value'))}[/red]"
            )
            return
        table.clear()
        self._market_matcher = matcher
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
        for position, (_repo, plugin) in enumerate(ordered, start=1):
            if filter_value and filter_value != "all" and filter_value not in plugin.categories:
                continue
            if matcher.query:
                blob = f"{plugin.id} {plugin.name} {plugin.description} {plugin.description_for(language)}"
                if not matcher.matches(blob):
                    continue
            table.add_row(
                escape(plugin.name or plugin.id),
                escape(plugin.description_for(language)[:44]),
                escape(plugin.version),
                self._market_status_of(plugin),
                key=plugin.id,
            )
            if position % 50 == 0:
                await asyncio.sleep(0)
        categories = sorted({c for _r, p in self._entries for c in p.categories})
        if categories != getattr(self, "_categories", None):
            select = self.query_one_optional("#market-category", Select)  # pyright: ignore[reportUnknownVariableType]
            if select is not None and _select_ready(select):
                try:
                    select.set_options(  # pyright: ignore[reportUnknownMemberType]
                        [("all", "all"), *[(self._category_label(c), c) for c in categories]]
                    )
                except QueryError:
                    return
                self._categories = categories
        select = self.query_one_optional("#market-category", Select)  # pyright: ignore[reportUnknownVariableType]
        desired = filter_value if filter_value in {*categories, "all"} else "all"
        if select is not None and select.value != desired:  # pyright: ignore[reportUnknownMemberType]
            _safe_set_value(select, desired)
        self._set_status(self._market_stale_status)
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

    def _schedule_market_filter(self) -> None:
        self._market_filter_generation += 1
        generation = self._market_filter_generation

        async def _filter() -> None:
            await asyncio.sleep(0.3)
            if generation == self._market_filter_generation:
                await self._render_entries()

        self.run_worker(
            cast(Any, _filter), exclusive=True, group="market-filter", exit_on_error=False
        )

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "market-search":
            self._schedule_market_filter()

    async def on_search_toggle_toggled(self, event: SearchToggle.Toggled) -> None:
        if event.toggle.id in {"market-search-regex", "market-search-case"}:
            self._schedule_market_filter()

    async def on_data_table_row_selected(self, event: TableRowEvent) -> None:
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

    async def on_data_table_row_highlighted(self, event: TableRowEvent) -> None:
        plugin = next((p for _r, p in self._entries if p.id == event.row_key.value), None)
        if plugin is not None and plugin is not self._selected:
            self._show_detail(plugin)

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in ("market-category", "market-sort"):
            await self._render_entries()

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
                result = await self._service.plugin_install(plugin.id)
                if "already" in result.lower():
                    status_node.update(escape(result))
                    return
                message_key = "plugin.installed_ok"
            elif button_id == "market-uninstall":
                await self._service.plugin_uninstall(plugin.id)
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
            status_node.update(escape(error_message(self._service, exc)))
            return
        await self._render_entries()
        status_node.update(
            self._service.t(message_key, plugin_id=plugin.id)
            + f" ({self._service.t('plugin.restart_note')})"
        )


async def pilot_pause_if_possible(app: Any, seconds: float = 0.05) -> None:
    """Yield control so remounted widgets finish composing; a no-op when the
    app is not currently running its test/message pump."""
    await asyncio.sleep(seconds)


class MailFlowApp(App[None]):
    """Eight-tab administration UI."""

    CSS_PATH = "app.tcss"
    BINDINGS: ClassVar[list[Any]] = []

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
        session: SessionHost | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._log_queue = log_queue
        self._remote = remote
        self._splash = splash
        self._session = session
        if session is not None:
            session.bind(self)
        self._log_timer: Any = None
        # shared plugin-entry cache: the single source of truth for the
        # detail dialog. MarketPane writes its fetched entries here, and
        # the Runtime tab reads from here, so both tabs show the exact
        # same detail (same readme, same translations).
        self._plugin_entries: dict[str, MarketPlugin] = {}

        self._reset_shortcuts()

    def _reset_shortcuts(self, *, refresh: bool = False) -> None:
        """Bind visible footer text from the active language pack."""
        self._bindings = BindingsMap()
        self._bindings.bind("f1", "shortcut_help", self._service.t("tui.help_shortcuts"))
        if self._session is not None:
            self._bindings.bind(
                "f8", "detach_session", self._service.t("sessions.detach"), priority=True
            )
        self._bindings.bind(
            "ctrl+q",
            "quit",
            self._service.t("sessions.quit" if self._session is not None else "tui.btn_quit"),
        )
        for key, tab_id in (
            ("ctrl+1", "tab-mail"),
            ("ctrl+2", "tab-actions"),
            ("ctrl+0", "tab-assistant"),
            ("ctrl+3", "tab-mailboxes"),
            ("ctrl+4", "tab-llms"),
            ("ctrl+5", "tab-runtime"),
            ("ctrl+6", "tab-market"),
            ("ctrl+7", "tab-notifications"),
            ("ctrl+8", "tab-settings"),
            ("ctrl+9", "tab-logs"),
        ):
            if self._remote and tab_id in {
                "tab-mailboxes",
                "tab-llms",
                "tab-market",
                "tab-notifications",
            }:
                continue
            self._bindings.bind(key, f"goto_tab('{tab_id}')", show=False)
        if refresh:
            self.refresh_bindings()

    def action_shortcut_help(self) -> None:
        if not isinstance(self.screen, ShortcutHelp):
            self.push_screen(ShortcutHelp(self._service.t))  # pyright: ignore[reportUnknownMemberType]

    def action_detach_session(self) -> None:
        if self._session is not None:
            self._session.detach()

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
        infos = (
            self._service.snapshot().plugins
            if self._remote
            else self._service.plugin_manager.enabled_infos()
        )
        for info in infos:
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

    def open_assistant(self, scope: dict[str, Any], name: str) -> None:
        self.query_one(TabbedContent).active = "tab-assistant"
        if not self.query_one(AssistantPane).set_scope(scope, name):
            self.notify(self._service.t("assistant.busy"))

    async def on_assistant_pane_open_result(self, event: AssistantPane.OpenResult) -> None:
        try:
            await self._open_assistant_result(event)
        except Exception as exc:
            self.notify(error_message(self._service, exc), severity="error")

    async def _open_assistant_result(self, event: AssistantPane.OpenResult) -> None:
        if event.kind == "model":
            self.query_one(TabbedContent).active = "tab-settings" if self._remote else "tab-llms"
            if self._remote:
                self.notify(self._service.t("assistant.remote_model_setup"))
        elif event.kind == "mail":
            record = await self._service.get_mail(event.identity)
            if record is not None:
                self.push_screen(MailReader(self._service, record))
            else:
                self.notify(self._service.t("mail.not_found", mail_id=event.identity))
        elif event.kind == "action":
            item = next(
                (a for a in await self._service.list_actions_all() if a.item_id == event.identity),
                None,
            )
            if item is not None:
                self.push_screen(TodoEditModal(self._service, item))
            else:
                self.notify(self._service.t("action.not_found", item_id=event.identity))
        elif event.kind == "plugin":
            self.query_one(TabbedContent).active = "tab-runtime"
            self.query_one(RuntimePane)._open_plugin_detail(event.identity)  # pyright: ignore[reportPrivateUsage]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="tab-mail"):
            with TabPane(self._service.t("tui.tab_mail"), id="tab-mail"):
                yield MailPane(self._service)
            with TabPane(self._service.t("tui.tab_actions"), id="tab-actions"):
                yield ActionsPane(self._service)
            with TabPane(self._service.t("assistant.tab"), id="tab-assistant"):
                yield AssistantPane(self._service)
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
            if not self._remote:
                with TabPane(self._service.t("tui.tab_notifications"), id="tab-notifications"):
                    yield NotificationsPane(self._service)
            with TabPane(self._service.t("tui.tab_settings"), id="tab-settings"):
                yield SettingsPane(self._service)
            with TabPane(self._service.t("tui.tab_logs"), id="tab-logs"):
                yield LogsPane(self._service, self._log_queue)
        yield Footer()

    async def on_mount(self) -> None:
        self.title = self._service.t("tui.title")
        self._sync_connection_status()
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
        self._subscriptions = [self._service.on("*", self._on_service_event)]
        if self._splash:
            from mailflow_tui.splash import SplashScreen

            self.push_screen(SplashScreen(self._service.t, self._version_string()))  # pyright: ignore[reportUnknownMemberType]
        if self._session is not None:
            self.call_after_refresh(self._session.mark_ready)

    def _version_string(self) -> str:
        """Both local and remote services expose the same snapshot type."""
        return f"v{self._service.snapshot().version}"

    def _sync_connection_status(self) -> None:
        status = remote_connection_status(self._service)
        name = f" | {self._session.info.name}" if self._session is not None else ""
        self.sub_title = self._version_string() + name + (f" | {status}" if status else "")
        for pane in self.query(RuntimePane):
            pane.update_connection_status()

    async def _preload_market_cache(self) -> None:
        """Load persisted marketplace entries into the shared cache."""
        if self._remote:
            return
        try:
            entries: list[tuple[Repository, MarketPlugin]] = await self._service.market_cache_load()
        except Exception:
            entries = []
        if entries:
            self.set_plugin_entries(entries)

    async def _eager_market_fetch(self) -> None:
        """Background marketplace fetch at startup: fills the shared cache
        (and the persisted cache) so the Runtime tab's plugin detail shows
        translated readmes without the user opening the Market tab."""
        if self._remote:
            return
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

    async def on_unmount(self) -> None:
        for unsubscribe in getattr(self, "_subscriptions", []):
            if callable(unsubscribe):
                unsubscribe()

    async def _on_service_event(self, event: str, **payload: Any) -> None:
        if event == "language.changed":
            await self._on_language_changed(event, **payload)
        elif event == "remote.connection.changed":
            self._sync_connection_status()
            if payload.get("state") == "connected":
                self._schedule_reload()
        elif event.startswith(
            (
                "mail.",
                "action.",
                "plugin.",
                "config.",
                "gateway.",
                "mailflow.mail.processed",
                "account.sync.changed",
                "backup.restored",
                "mailflow.runtime.",
                "mailflow.cleanup.",
                "mailflow.records.",
            )
        ):
            self._schedule_reload()

    def _pane_factories(self) -> dict[str, tuple[Callable[[Any], Any], str]]:
        return {
            "tab-mail": (MailPane, "tui.tab_mail"),
            "tab-mailboxes": (AccountsPane, "tui.tab_mailboxes"),
            "tab-actions": (ActionsPane, "tui.tab_actions"),
            "tab-assistant": (AssistantPane, "assistant.tab"),
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
        """Translate the current views without discarding their reading state."""
        self.title = self._service.t("tui.title")
        self._sync_connection_status()
        self._reset_shortcuts(refresh=True)
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
            if not container.is_attached or not container.children:
                continue
            content = container.children[0]
            if isinstance(
                content,
                (
                    MailPane,
                    ActionsPane,
                    AssistantPane,
                    RuntimePane,
                    AccountsPane,
                    LLMPane,
                    NotificationsPane,
                    SettingsPane,
                ),
            ):
                await content.relabel()
                continue
            await container.remove_children()
            if not container.is_attached:
                continue
            try:
                await container.mount(factory(self._service))
            except MountError:
                if container.is_attached:
                    raise
        # panes that survive remounting still need their statics retranslated
        for pane in self.query(LogsPane):
            pane.relabel()
        for table in self.query(SelectionTable):
            table.relabel()
        await pilot_pause_if_possible(self)

    async def _on_mail_processed(self, event: str, **payload: Any) -> None:
        # the service runs on the same loop as the app: schedule directly
        self._schedule_reload()

    def schedule_reload(self) -> None:
        """Coalesced refresh of the Mail/Actions/Runtime panes.

        Used after an operation changes stored state outside the
        mail-processed event (e.g. a smart action that scheduled entries).
        """
        self._schedule_reload()

    def _schedule_reload(self) -> None:
        # a bulk re-analysis emits one event per mail: coalesce the burst
        # into a single refresh instead of queueing a full reload per mail
        self._reload_requested = True
        if getattr(self, "_reload_scheduled", False):
            return
        self._reload_scheduled = True

        self.run_worker(cast(Any, self._reload_guarded))

    async def _reload_guarded(self) -> None:
        try:
            while self._reload_requested:
                self._reload_requested = False
                async with self._refresh_lock:
                    await self._reload_all()
        finally:
            self._reload_scheduled = False

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
        # The remote runtime pane updates the shared snapshot; reflect its
        # latest queue/paused state in the mail controls in this same refresh.
        for mail_pane in self.query(MailPane):
            mail_pane.sync_analysis_controls()

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
