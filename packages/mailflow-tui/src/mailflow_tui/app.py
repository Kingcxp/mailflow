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
    BatchResult,
    ComponentKind,
    MailRecord,
    PluginSnapshot,
    ReplyDraft,
    ReplyState,
    SmartActionResult,
    Urgency,
    looks_binary,
    to_utc,
)
from mailflow.errors import error_text
from mailflow.mail_body import mail_body_text
from mailflow.mail_query import MailQuery
from mailflow.plugin_market import MarketPlugin, Repository
from mailflow.service import MailFlowService
from mailflow.themes import DEFAULT_THEME, TERMINAL_THEME
from rich.console import Group as RichGroup
from rich.markdown import Markdown as RichMarkdown
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
    Collapsible,
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

from mailflow_tui.action_types import ActionTypePicker, ActionTypesModal
from mailflow_tui.confirm import ConfirmModal
from mailflow_tui.console import ConsolePane
from mailflow_tui.export import BotExportScreen
from mailflow_tui.install import InstallScreen
from mailflow_tui.labels import (
    error_detail,
    error_message,
    mail_status,
    plugin_text,
    remote_connection_status,
    urgency_label,
)
from mailflow_tui.labels import action_type_label as _action_type_label
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
from mailflow_tui.pickers import ChoicePicker
from mailflow_tui.profile import UserProfileModal
from mailflow_tui.repos import ReposScreen
from mailflow_tui.scaffold import PluginScaffoldScreen
from mailflow_tui.search import SearchMatcher, build_search_matcher
from mailflow_tui.settings import ConnectionsPane, MailHistoryScreen, SettingsPane
from mailflow_tui.smart_action_review import SmartActionReviewModal
from mailflow_tui.terminal_theme import terminal_background_mode
from mailflow_tui.theme_preferences import load_theme_preferences, save_theme_preferences
from mailflow_tui.theme_welcome import ThemeWelcomeScreen
from mailflow_tui.themes import apply_color_scheme, theme_catalog, urgency_style
from mailflow_tui.todo_create import TodoCreateModal, TodoEditModal
from mailflow_tui.view_state import TableRowEvent, TableView, update_table
from mailflow_tui.widgets import (
    Input,
    PromptInput,
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
_DETAIL_TITLE = "$mf-title"
_DETAIL_HEADING = "$mf-heading"
_DETAIL_MUTED = "$mf-muted"
_DETAIL_TIME = "$mf-time"
_DETAIL_TAG = "$mf-tag"
_DETAIL_LINK = "$mf-link"
_ACTION_OVERDUE_COLORS = ("$mf-overdue0", "$mf-overdue1", "$mf-overdue2")


def _load_error(service: MailFlowService, exc: Exception) -> str:
    message = service.t("tui.data_load_failed", error=error_detail(service, exc))
    return f"[$ctp-red]{escape(message)}[/$ctp-red]"


def _status_content(status: str | RichMarkdown, notes: list[str]) -> str | RichGroup:
    if isinstance(status, RichMarkdown):
        return RichGroup(status, *(RichText(note) for note in notes))
    return "\n".join(filter(None, [status, *notes]))


async def _run_batch(
    service: MailFlowService,
    targets: list[str],
    apply: Callable[[str], Awaitable[object]],
    stopped: Callable[[], bool],
    progress: Callable[[int, int], None],
) -> tuple[BatchResult, list[str]]:
    """Finish the current item before stopping; never retry successful items."""
    targets = list(dict.fromkeys(targets))
    result = BatchResult()
    remaining = set(targets)
    progress(0, len(targets))
    for index, identity in enumerate(targets, 1):
        if stopped():
            break
        try:
            outcome = await apply(identity)
            if isinstance(outcome, BatchResult):
                result.succeeded.extend(outcome.succeeded)
                result.failed.update(outcome.failed)
                result.skipped.extend(outcome.skipped)
                result.source_results.update(outcome.source_results)
            elif outcome is None or outcome is False:
                result.skipped.append(identity)
            else:
                result.succeeded.append(identity)
        except Exception as exc:
            result.failed[identity] = error_message(service, exc)
        remaining.discard(identity)
        progress(index, len(targets))
        await asyncio.sleep(0)
    return result, [identity for identity in targets if identity in remaining]


def _batch_feedback(service: MailFlowService, result: BatchResult, pending: list[str]) -> str:
    message = service.t(
        "ux.batch_result",
        success=len(result.succeeded),
        failed=len(result.failed),
        skipped=len(result.skipped),
    )
    if pending:
        message += "\n" + service.t("tui.batch_remaining", count=len(pending))
    if result.failed:
        message += "\n" + service.t(
            "tui.batch_failed", count=len(result.failed), error=next(iter(result.failed.values()))
        )
    for state in result.source_results.values():
        if state["state"] != "synced":
            message += "\n" + state["message"]
    if any(state["state"] != "synced" for state in result.source_results.values()):
        message += "\n" + service.t("tui.batch_source_check")
    return message


def _localize(service: MailFlowService, value: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    from zoneinfo import ZoneInfo

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(service.config.general.timezone)).strftime(fmt)


def _action_time_range(service: MailFlowService, item: ActionItem) -> str:
    """Show an action's UTC storage times in the configured local timezone."""
    return item.time_range_in(service.config.general.timezone) or service.t("ux.no_due")


def _detail_section(label: str, body: str | RichText, *, style: str = "") -> RichText:
    """Style the heading separately so long content keeps its normal weight."""
    text = RichText()
    text.append(label + "\n", style=_DETAIL_HEADING)
    text.append(body, style=style or None)
    return text


def _action_age_style(service: MailFlowService, item: ActionItem) -> str:
    days = service.action_overdue_days(item)
    if days is None:
        return ""
    color = _ACTION_OVERDUE_COLORS[min(days, len(_ACTION_OVERDUE_COLORS) - 1)]
    return color


def _action_time_style(service: MailFlowService, item: ActionItem) -> str:
    if style := _action_age_style(service, item):
        return style
    if item.due_at is None:
        return _DETAIL_MUTED
    return _DETAIL_TIME


def _action_detail_text(service: MailFlowService, item: ActionItem) -> RichText:
    """Keep task details readable without interpreting user text as markup."""
    detail = RichText()
    if item.preview:
        detail.append(service.t("recurrence.preview") + "\n", style=_DETAIL_TAG)
    detail.append(item.summary, style=_DETAIL_TITLE)
    detail.append("\n\n")
    detail.append(
        _detail_section(
            service.t("tui.action_time"),
            _action_time_range(service, item),
            style=_action_time_style(service, item),
        )
    )
    if item.due_at is not None:
        detail.append(
            "\n" + service.t("interface.timezone", zone=service.config.general.timezone),
            style=_DETAIL_MUTED,
        )
    detail.append("\n")
    fields = [
        ("tui.action_type", _action_type_label(service, item.action_type)),
        ("recurrence.category", item.category),
        ("tui.action_tags", ", ".join(item.tags)),
        ("tui.action_location", item.location),
    ]
    for label, value in fields:
        if value:
            detail.append("\n" + service.t(label) + ": ", style=_DETAIL_MUTED)
            detail.append(value, style=_DETAIL_TAG if label != "tui.action_location" else None)
    if item.notes:
        detail.append("\n\n")
        detail.append(_detail_section(service.t("tui.action_notes"), item.notes))
    if item.url:
        detail.append("\n\n")
        detail.append(_detail_section(service.t("tui.action_url"), item.url, style=_DETAIL_LINK))
    source = (
        (item.source_subject or service.t("tui.action_from_mail"))
        if item.mail_id
        else service.t("tui.action_custom_source")
    )
    detail.append("\n\n")
    detail.append(
        _detail_section(
            service.t("tui.action_source"), source, style="$ctp-sapphire" if item.mail_id else _DETAIL_MUTED
        )
    )
    if style := _action_age_style(service, item):
        detail.stylize(style)
    return detail


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
        self._preview = False
        self._bindings.bind("escape", "close_reply", self._t("tui.btn_close"))
        self._bindings.bind("ctrl+s", "save_reply", self._t("tui.reply_save"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        from mailflow_tui.mail_workflows import DraftPreview

        with Vertical(id="reply-dialog"):
            yield Static(self._t("tui.reply_title"), id="reply-title")
            with ScrollableContainer(id="reply-form"):
                yield Static(
                    f"{self._t('mail.field_account')}: {self._record.mail.account_id}",
                    id="reply-account",
                    markup=False,
                )
                yield Static(
                    f"{self._t('tui.reply_to_label')}: "
                    f"{(self._record.mail.reply_to or self._record.mail.sender).display}",
                    id="reply-recipient",
                    markup=False,
                )
                yield Static(
                    f"{self._t('tui.reply_subject_label')}: "
                    f"{self._service.t('tui.reply.subject_prefix')} {self._record.mail.subject}",
                    id="reply-subject",
                    markup=False,
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
            yield DraftPreview(self._service, id="reply-preview")
            with Horizontal(id="reply-actions"):
                yield Button(self._t("tui.reply_save"), id="reply-save", variant="primary")
                yield Button(self._t("tui.reply_prepare"), id="reply-prepare", variant="warning")
                yield Button(
                    self._t("tui.reply_confirm"),
                    id="reply-confirm",
                    variant="success",
                    disabled=True,
                )
                yield Button(self._t("experience.sent_check"), id="reply-check", disabled=True)
                yield Button(self._t("tui.reply_cancel"), id="reply-cancel", variant="error")
                yield Button(self._t("interface.recipients_files"), id="reply-files")
                yield Button(self._t("interface.saved_draft"), id="reply-back")
            yield Static(self._t("tui.reply_confirm_hint"), id="reply-status")

    def on_mount(self) -> None:
        self._sync_reply_controls()
        self._set_status(self._t("tui.loading"))
        self.run_worker(self._load_reply(), group="reply-load", exit_on_error=False)

    async def _load_reply(self) -> None:
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
        elif self._draft.sent_copy_error:
            self._set_status(self._t("experience.sent_copy_failed"))
        elif self._draft.state is ReplyState.PREPARED and not self._draft.is_confirmation_valid(
            self._draft.token or ""
        ):
            self._set_status(self._t("tui.reply_prepare_expired"))
        self._sync_reply_controls()

    async def _edit_reply_details(self) -> None:
        from mailflow_tui.workspace_screens import ComposeScreen

        try:
            if self._draft is None:
                return
            self._draft = await self._service.edit_draft(
                self._draft.draft_id,
                self._draft.subject,
                self.query_one("#reply-body", TextArea).text,
            )
            await cast(Any, self).app.push_screen_wait(
                ComposeScreen(self._service, draft=self._draft)
            )
            self._draft = await self._service.get_draft(self._draft.draft_id)
            if self._draft is not None:
                self.query_one("#reply-body", TextArea).load_text(self._draft.body)
        except Exception as exc:
            await self._reply_failed(exc)
        finally:
            self._reply_busy = False
            if self.is_mounted:
                self._sync_reply_controls()

    def _sync_reply_controls(self) -> None:
        draft = self._draft
        if draft:
            for selector, text in (
                ("#reply-account", self._t("mail.field_account") + ": " + draft.account_id),
                (
                    "#reply-recipient",
                    self._t("tui.reply_to_label")
                    + ": "
                    + ", ".join(a.display for a in draft.to_addresses),
                ),
                ("#reply-subject", self._t("tui.reply_subject_label") + ": " + draft.subject),
            ):
                self.query_one(selector, Static).update(text)
            if self.query_one("#reply-body", TextArea).text != draft.body:
                self._preview = False
        self.query_one("#reply-form").display = not self._preview
        self.query_one("#reply-preview").display = self._preview
        self.query_one("#reply-save", Button).label = self._t(
            "tui.btn_edit" if self._preview else "tui.reply_save"
        )
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
        self.query_one("#reply-back", Button).disabled = self._reply_busy
        check = self.query_one("#reply-check", Button)
        check.display = draft is not None and draft.state == ReplyState.SENDING
        check.disabled = self._reply_busy or not check.display
        if not self._reply_busy and (
            draft is None
            or draft.state in (ReplyState.SENDING, ReplyState.SENT, ReplyState.CANCELLED)
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
        if button_id == "reply-back":
            self.action_close_reply()
            return
        if button_id == "reply-files" and self._draft is not None:
            self._reply_busy = True
            self._sync_reply_controls()
            self.run_worker(self._edit_reply_details(), group="reply-details", exit_on_error=False)
            return
        if button_id == "reply-cancel":
            if self._draft is None or self._draft.state in (
                ReplyState.SENDING,
                ReplyState.SENT,
                ReplyState.CANCELLED,
            ):
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
        if button_id == "reply-check":
            self._reply_busy = True
            self._sync_reply_controls()
            self.run_worker(
                self._check_reply(self._draft.draft_id),
                exclusive=True,
                group="reply-check",
                exit_on_error=False,
            )
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
            if self._preview:
                self._preview = False
                self._sync_reply_controls()
                return
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
        from mailflow_tui.mail_workflows import DraftPreview

        try:
            draft = await self._service.get_draft(draft_id)
            if draft is None:
                raise ValueError(self._t("reply.draft_not_found", draft_id=draft_id))
            body = self.query_one("#reply-body", TextArea).text
            if body != draft.body:
                await self._service.edit_draft(draft_id, draft.subject, body)
            self._draft = await self._service.prepare_reply(draft_id)
            self.query_one(DraftPreview).show_draft(self._draft)
            self._preview = True
            self._set_status(self._t("tui.reply_confirm_hint"))
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
            self._set_status(
                self._t("experience.sent_copy_failed")
                if self._draft.sent_copy_error
                else self._t("tui.reply_sent")
            )
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

    async def _check_reply(self, draft_id: str) -> None:
        try:
            self._draft = ReplyDraft.model_validate(
                await self._service.workspace_call("reply_reconcile", {"draft_id": draft_id})
            )
            self._set_status(
                self._t("tui.reply_sent")
                if self._draft.state == ReplyState.SENT
                else self._t("reply.send_uncertain")
            )
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
            yield Static(_action_detail_text(self._service, item), id="action-detail-content")
            # the source mail's own details load asynchronously below
            yield Static("", id="action-mail-detail")
        # outside the scroll box: always visible, never scrolls away
        with Horizontal(id="action-footer"):
            yield Button(
                self._service.t("tui.action_open_source"),
                id="action-source-mail",
                variant="primary",
                disabled=True,
            )
            yield Button(self._service.t("tui.btn_edit"), id="action-edit", variant="primary")
            yield Button(self._service.t("tui.btn_close"), id="action-close", variant="default")

    def on_mount(self) -> None:
        """Load the source mail: a todo without its original message context
        (subject, sender, body) forces the user to hunt through the mail tab."""
        self.run_worker(self._load_source_mail(), group="action-source", exit_on_error=False)

    async def _load_source_mail(self) -> None:
        node = self.query_one_optional("#action-mail-detail", Static)
        if node is None:
            return
        if not self._item.mail_id:
            return
        node.update(self._service.t("tui.loading"))
        try:
            record = await self._service.get_mail(self._item.mail_id)
        except Exception as exc:
            node.update(_load_error(self._service, exc))
            return
        if record is None:
            node.update(f"[$mf-muted]{self._service.t('tui.action_mail_missing')}[/$mf-muted]")
            return
        mail = record.mail
        self.query_one("#action-source-mail", Button).disabled = False
        self.query_one("#action-detail-content", Static).update(
            _action_detail_text(
                self._service, self._item.model_copy(update={"source_subject": mail.subject})
            )
        )
        body = mail_body_text(mail)
        if not body and looks_binary(mail.body_text):
            body = self._service.t("tui.detail_binary_body")
        lines: RichText = RichText()
        status = mail_status(self._service, record)
        if status:
            lines.append(status)
            lines.append("\n\n")
        fields = [
            ("tui.detail_original_subject", mail.subject, _DETAIL_TITLE),
            ("tui.column_sender", mail.sender.display or mail.sender.address, "$ctp-sapphire"),
            ("tui.column_date", _localize(self._service, mail.date), _DETAIL_TIME),
        ]
        for label, value, style in fields:
            lines.append(_detail_section(self._service.t(label), value, style=style))
            lines.append("\n\n")
        summary_value = self._service.display_text(record.summary)
        summary = RichText(summary_value or self._service.t("tui.detail_analysis_unavailable"))
        if record.analysis_is_fallback and summary_value:
            summary.append(
                f" ({self._service.t('tui.detail_summary_fallback')})", style="bold $ctp-yellow"
            )
        lines.append(_detail_section(self._service.t("tui.detail_summary"), summary))
        reason = record.analysis.reason if record.analysis else ""
        if reason:
            lines.append("\n\n")
            lines.append(
                _detail_section(
                    self._service.t("tui.detail_reason"), self._service.display_text(reason)
                )
            )
        lines.append("\n\n")
        lines.append(
            _detail_section(
                self._service.t("tui.detail_body"),
                body[:800] or self._service.t("tui.mail_no_body"),
            )
        )
        if mail.image_text.strip():
            lines.append("\n\n")
            lines.append(
                _detail_section(self._service.t("mail.field_image_text"), mail.image_text)
            )
        node.update(lines)

    def _open_editor(self) -> None:
        def _after_edit(changed: bool | None) -> None:
            if changed:
                self.dismiss(True)

        cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
            TodoEditModal(self._service, self._item), callback=_after_edit
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "action-source-mail" and self._item.mail_id:
            app = cast(MailFlowApp, self.app)
            self.dismiss(None)
            app.call_after_refresh(app.open_mail, self._item.mail_id)
        elif event.button.id == "action-edit":
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
        self._operation_status: str | RichMarkdown = ""
        self._query = MailQuery()
        self._next_offset: int | None = None
        self._page_limit = 200
        self._page_total = 0
        self._trash_busy = False
        self._trash_stop = False
        self._undo_trash_ids: list[str] = []
        self._smart_action_result: SmartActionResult | None = None
        self._smart_action_running = False
        self._smart_query_text = ""
        self._smart_scope: dict[str, Any] = {}
        self._smart_scope_name = ""
        self._smart_history: list[dict[str, str]] = []

    def compose(self) -> ComposeResult:
        # the urgency Select auto-selects "auto" while mounting and fires
        # Changed; swallow that programmatic event (and the relabel restore)
        # so it never stamps an override onto the selected mail
        self._urgency_suppress = True
        with Horizontal(id="mail-search-row"):
            yield PromptInput(
                placeholder=self._service.t("tui.smart_action_placeholder"), id="mail-search"
            )
            yield SearchToggle("regex", ident="mail-search-regex")
            yield SearchToggle("case", ident="mail-search-case")
            yield Button(
                self._service.t("tui.smart_action"),
                id="smart-action",
                variant="primary",
                disabled=True,
            )
        with Horizontal():
            yield SelectionTable(t=self._service.t, explorer_selection=True, id="mail-table")
            with ScrollableContainer(id="mail-detail"):
                yield Static("", id="mail-title")
                yield Static("", id="mail-analysis-status")
                yield Static("", id="mail-operation-status")
                yield Static("", id="mail-notes")
                yield Static("", id="mail-summary")
                yield Static("", id="mail-reason")
                yield Static("", id="mail-actions")
                yield Static("", id="mail-attachments")
                yield Static("", id="mail-subject")
                yield Static("", id="mail-body")
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
                with Horizontal(id="mail-actions-row2"):
                    yield Button(
                        self._service.t("tui.btn_reply"), id="btn-reply", variant="success"
                    )
                    yield Button(
                        self._service.t("tui.btn_reparse"),
                        id="btn-reparse",
                        variant="primary",
                    )
                    yield Button(
                        self._service.t("tui.btn_reparse_failed"),
                        id="btn-reparse-failed",
                        variant="warning",
                    )
                with Horizontal(id="mail-actions-row3"):
                    yield Button(
                        self._service.t("tui.btn_purge_expired"),
                        id="btn-purge-expired",
                        variant="warning",
                    )
                    yield Button(
                        self._service.t("tui.btn_reparse_all"),
                        id="btn-reparse-all",
                        variant="primary",
                    )
                    yield Button(
                        self._service.t("tui.btn_profile"),
                        id="btn-profile",
                        variant="success",
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
                ("btn-reparse-failed", "tui.btn_reparse_failed", "warning"),
            ),
            (
                ("btn-purge-expired", "tui.btn_purge_expired", "warning"),
                ("btn-reparse-all", "tui.btn_reparse_all", "primary"),
                ("btn-profile", "tui.btn_profile", "success"),
            ),
        )

    async def on_mount(self) -> None:
        self._urgency_suppress = False
        self._sync_selection_controls()
        self._sync_smart_button()
        # never block startup on storage reads + row rendering: the pane
        # paints empty and fills in as the exclusive worker delivers
        self.run_worker(
            self.refresh_mail(), exclusive=True, group="mail-refresh", exit_on_error=False
        )

    async def on_unmount(self) -> None:
        self._stop_reparse()

    def _sync_smart_button(self) -> None:
        button = self.query_one_optional("#smart-action", Button)
        if button is not None:
            button.label = self._service.t("tui.smart_action")
            search = self.query_one_optional("#mail-search", PromptInput)
            button.disabled = (
                self._smart_action_running
                or self._trash_busy
                or search is None
                or not search.value.strip()
            )

    def set_smart_scope(self, scope: dict[str, Any], name: str) -> bool:
        if self._smart_action_running:
            return False
        self._smart_scope = {
            key: list(cast(list[Any], value)) if isinstance(value, list) else value
            for key, value in scope.items()
        }
        self._smart_scope_name = name
        self._smart_history.clear()
        self._smart_action_result = None
        self._set_operation_status(escape(self._service.t("assistant.scope", scope=name)))
        self.query_one("#mail-search", PromptInput).focus()
        return True

    async def _render_smart_action_result(self, result: SmartActionResult) -> None:
        self._records = list(result.records)
        hints = list(dict.fromkeys(filter(None, [result.final_text, *result.failure_reasons])))
        await self._render_records(self._records)
        answer = "\n\n".join(hints)
        self._set_operation_status(
            RichMarkdown(answer) if answer else self._service.t("tui.smart_action_done")
        )

    async def _toggle_smart_action(self, instruction: str | None = None) -> None:
        if self._smart_action_running or self._trash_busy:
            return
        text = (
            instruction
            if instruction is not None
            else self.query_one("#mail-search", PromptInput).value
        ).strip()
        if not text:
            return
        self._smart_action_running = True
        self._mail_filter_generation += 1
        self._sync_smart_button()
        panel = SmartActionReviewModal(
            self._service,
            text,
            scope=self._smart_scope,
            scope_name=self._smart_scope_name,
            conversation=self._smart_history[-12:],
        )
        try:
            result = await self._ask_screen(panel)
            if result is None:
                self._set_operation_status(escape(panel.cancel_message))
            else:
                self._smart_history.extend(
                    [
                        {"role": "user", "content": text},
                        {
                            "role": "assistant",
                            "content": result.final_text
                            + "\n"
                            + "\n".join(
                                [f"mail {r.record_id}: {r.mail.subject}" for r in result.records]
                                + [f"task {a.item_id}: {a.summary}" for a in result.actions]
                            ),
                        },
                    ]
                )
                self._smart_query_text = text
                self._smart_action_result = result
                await self._render_smart_action_result(result)
        except Exception as exc:
            self._set_operation_status(escape(error_message(self._service, exc)))
        finally:
            self._smart_action_running = False
            self._sync_smart_button()

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
        table.add_column(self._service.t("tui.column_date"), key="date")
        table.add_column(self._service.t("tui.column_urgency"), key="urgency")
        table.add_column(self._service.t("tui.column_subject"), key="subject", width=36)
        table.add_column(self._service.t("tui.column_sender"), key="sender", width=32)
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        table.tooltip = None
        urgency = self._urgency_select()
        if urgency is not None:
            urgency.tooltip = self._service.t("tui.urgency_help")
        self._columns_done = True

    async def relabel(self) -> None:
        self._columns_done = False
        _relabel_buttons(
            self,
            {
                "smart-action": "tui.smart_action",
                **{
                    ident: key
                    for row in self._button_rows()
                    for ident, key, _variant in row
                    if not (
                        ident.startswith("btn-reparse")
                        and self._reparse_worker is not None
                        and self._reparse_worker.is_running
                    )
                },
            },
        )
        search = self.query_one_optional("#mail-search", PromptInput)
        if search is not None:
            search.placeholder = self._service.t("tui.smart_action_placeholder")
        self._sync_smart_button()
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
                self._set_static("#mail-analysis-status", _load_error(self._service, exc))

    async def _render_records(self, records: list[MailRecord]) -> None:
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
                self._ensure_columns()
                self._refresh_view_options()
                reordered = update_table(
                    table, [(record.record_id, self._mail_cells(record)) for record in records]
                )
                if reordered or view.row_key != TableView.capture(table).row_key:
                    self._selected_id = view.restore(table)
                table.restore_selection(select_initial=view.row_key is None)
        finally:
            self._rendering = False
        selected = next((r for r in records if r.record_id == self._selected_id), None)
        if selected is not None:
            self._sync_urgency_select(selected)
        await self._show_selected(cached=True)
        self._sync_selection_controls()

    def _mail_cells(self, record: MailRecord) -> tuple[Any, ...]:
        urgency = record.effective_urgency
        title = RichText(record.display_title or self._service.t("tui.mail_no_subject"))
        title.truncate(36, overflow="ellipsis")
        sender = RichText(record.mail.sender.address)
        sender.truncate(32, overflow="ellipsis")
        return (
            _localize(self._service, record.mail.received_at, "%m-%d %H:%M"),
            RichText(f"■ {urgency_label(self._service, urgency)}", style=urgency_style(urgency)),
            title,
            sender,
        )

    async def _refresh_mail_unlocked(self, *, reload: bool = True) -> None:
        if self._mail_table() is None:
            return
        if self._smart_action_running:
            return
        search_input = self.query_one_optional("#mail-search", PromptInput)
        result = self._smart_action_result
        if (
            result is not None
            and search_input is not None
            and search_input.value.strip() == self._smart_query_text
        ):
            if reload and result.records:
                current = {r.record_id: r for r in await self._service.list_mails()}
                result.records = [
                    current[r.record_id] for r in result.records if r.record_id in current
                ]
            await self._render_smart_action_result(result)
            return
        if reload or self._mail_cache is None:
            search_input = self.query_one_optional("#mail-search", PromptInput)
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
            # Keep a linked mail visible even when it is older than the loaded page.
            if self._selected_id and not any(
                row.record_id == self._selected_id for row in self._mail_cache
            ):
                selected = await self._service.get_mail(self._selected_id)
                if selected is not None and query.matches(selected):
                    self._mail_cache.append(selected)
            self._expired_cache = None
        records = [record for record in self._mail_cache or [] if self._query.matches(record)]
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
        search = self.query_one_optional("#mail-search", PromptInput)
        regex = self.query_one_optional("#mail-search-regex", SearchToggle)
        case = self.query_one_optional("#mail-search-case", SearchToggle)
        matcher = build_search_matcher(
            search.value if search is not None else "",
            regex=bool(regex.value) if regex is not None else False,
            case_sensitive=bool(case.value) if case is not None else False,
        )
        if matcher.error:
            self._set_static(
                "#mail-analysis-status",
                f"[$ctp-red]{self._service.t('tui.invalid_regex')}: {escape(self._service.t('errors.invalid_value'))}[/$ctp-red]",
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
        self._records = records
        await self._render_records(records)

    def on_selection_table_selection_changed(self, event: SelectionTable.SelectionChanged) -> None:
        if event.table.id == "mail-table":
            self._sync_selection_controls()

    def on_selection_table_menu_requested(self, event: SelectionTable.MenuRequested) -> None:
        if event.table.id == "mail-table" and not self._trash_busy and not self._reparse_active():
            event.stop()
            self.run_worker(
                self._mail_tools(), group="mail-tools", exclusive=True, exit_on_error=False
            )

    def on_selection_table_delete_requested(self, event: SelectionTable.DeleteRequested) -> None:
        if event.table.id == "mail-table":
            event.stop()
            targets = self._mail_targets()
            if targets and not self._trash_busy and not self._reparse_active():
                self._start_trash(lambda: self._trash_selected(targets))

    def _sync_selection_controls(self, *, reparse_idle: bool = False) -> None:
        self._sync_smart_button()
        table = self._mail_table()
        if table is None:
            return
        count = len(table.checked_keys)
        analysing = self._reparse_active() and not reparse_idle
        has_selected = any(r.record_id in table.checked_keys for r in self._records)
        trash = self.query_one_optional("#btn-trash", Button)
        if trash is not None:
            trash.label = (
                self._service.t("tui.batch_stopping" if self._trash_stop else "tui.batch_stop")
                if self._trash_busy
                else self._service.t("tui.btn_delete_selected", count=count)
                if count
                else self._service.t("tui.btn_trash")
            )
            trash.disabled = self._trash_stop if self._trash_busy else not count
            if analysing:
                trash.disabled = True
            trash.tooltip = None
        purge = self.query_one_optional("#btn-purge-expired", Button)
        if purge is not None:
            purge.disabled = self._trash_busy or analysing or not bool(self._mail_cache)
        for ident in ("#btn-reply", "#btn-ask-correct"):
            button = self.query_one_optional(ident, Button)
            if button is not None:
                button.tooltip = None
                button.disabled = self._trash_busy or analysing or not has_selected or count > 1
        table.disabled = self._trash_busy
        reparse = self.query_one_optional("#btn-reparse", Button)
        if reparse is not None and (reparse_idle or not self._reparse_active()):
            reparse.disabled = (
                self._trash_busy
                or getattr(self._service, "remote", False)
                or not count
            )
            reparse.label = (
                self._service.t("tui.btn_reparse_selected", count=count)
                if count
                else self._service.t("tui.btn_reparse")
            )
        if reparse_idle or not self._reparse_active():
            for ident in ("#btn-reparse-all", "#btn-reparse-failed"):
                button = self.query_one_optional(ident, Button)
                if button is not None:
                    button.disabled = (
                        self._trash_busy
                        or getattr(self._service, "remote", False)
                        or not bool(self._mail_cache)
                    )
        urgency = self._urgency_select()
        if urgency is not None:
            urgency.disabled = not count or self._trash_busy or analysing
        self._show_operation_status()

    def _mail_targets(self) -> list[str]:
        table = self._mail_table()
        if table is not None:
            visible = table.visible_keys()
            return [key for key in visible if key in table.checked_keys] + sorted(
                table.checked_keys - set(visible)
            )
        return []

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

    async def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "mail-search":
            self._sync_smart_button()
            if self._smart_action_running:
                return
            self._page_limit = 200
            self._schedule_mail_filter()

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        if event.input.id == "mail-search":
            event.stop()
            self.run_worker(
                self._toggle_smart_action(event.value), group="smart-action", exit_on_error=False
            )

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
            message = f"[$ctp-red]{escape(error_message(self._service, exc))}[/$ctp-red]"
            self._set_operation_status(message)

    async def _handle_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in ("mail-urgency-filter", "mail-sort"):
            if event.select.id == "mail-urgency-filter":
                self._expired_cache = None
            await self.refresh_mail(reload=event.select.id == "mail-urgency-filter")
        elif (
            event.select.id == "urgency-select"
            and bool(self._mail_targets())
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
            if not self._trash_busy:
                targets = self._mail_targets()
                self._start_trash(
                    lambda: self._update_mails(
                        targets, lambda ident: self._service.set_mail_urgency(ident, urgency)
                    )
                )

    def _clear_mail_detail(self) -> None:
        """Remove detail from a record no longer visible in the table."""
        self._detail_record_id = None
        for selector in (
            "#mail-title",
            "#mail-analysis-status",
            "#mail-operation-status",
            "#mail-subject",
            "#mail-summary",
            "#mail-reason",
            "#mail-actions",
            "#mail-body",
            "#mail-attachments",
            "#mail-notes",
        ):
            self._set_static(selector, "")
        self._show_operation_status()

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
                RichText(self._service.t("mail.not_found", mail_id=selected_id), style="$ctp-yellow"),
            )
            return
        same_record = self._detail_record_id == selected_id
        self._detail_record_id = selected_id
        service = self._service
        self._set_static_text(
            "#mail-title",
            RichText(record.display_title or service.t("tui.mail_no_subject"), style=_DETAIL_TITLE),
        )
        status = mail_status(service, record)
        self._set_static_text("#mail-analysis-status", status)
        self._set_static("#mail-operation-status", self._operation_status)
        subject = (
            _detail_section(
                service.t("tui.detail_original_subject"),
                record.mail.subject or service.t("tui.mail_no_subject"),
                style=_DETAIL_MUTED,
            )
            if record.display_title != " ".join(record.mail.subject.split())
            else RichText("")
        )
        self._set_static_text("#mail-subject", subject)
        # The stored summary and reason are the mail's content: they stay
        # visible even when the pipeline had to fall back, because hiding them
        # left the user with no content at all. A fallback is labelled, never
        # presented as a generated analysis.
        summary_value = service.display_text(record.summary).strip()
        summary_line = _detail_section(service.t("tui.detail_summary"), summary_value)
        if not summary_value:
            summary_line.append(service.t("tui.detail_analysis_unavailable"), style="$ctp-yellow")
        elif record.analysis_is_fallback:
            summary_line.append(
                f" ({service.t('tui.detail_summary_fallback')})", style="bold $ctp-yellow"
            )
        self._set_static_text("#mail-summary", summary_line)
        reason = service.display_text(record.analysis.reason if record.analysis else "").strip()
        reason_line = RichText()
        if reason:
            reason_line = _detail_section(service.t("tui.detail_reason"), reason)
        elif record.analysis_is_fallback:
            reason_line = _detail_section(
                service.t("tui.detail_reason"),
                service.t("tui.detail_reason_unavailable"),
                style="$ctp-yellow",
            )
        self._set_static_text("#mail-reason", reason_line)
        actions_text: RichText | None = None
        if record.action_items:
            actions_text = _detail_section(service.t("tui.detail_actions"), "")
            for index, item in enumerate(record.action_items):
                if index:
                    actions_text.append("\n\n")
                item_text = RichText()
                item_text.append("• ", style=_DETAIL_HEADING)
                item_text.append(item.summary, style=_DETAIL_TITLE)
                item_text.append("\n")
                item_text.append(
                    _action_time_range(service, item), style=_action_time_style(service, item)
                )
                item_text.append(
                    "\n" + _action_type_label(service, item.action_type), style=_DETAIL_TAG
                )
                if item.notes:
                    item_text.append("\n" + item.notes, style=_DETAIL_MUTED)
                if style := _action_age_style(service, item):
                    item_text.stylize(style)
                actions_text.append(item_text)
        self._set_static_text("#mail-actions", actions_text or RichText(""))
        body = mail_body_text(record.mail)
        if not body and looks_binary(record.mail.body_text):
            body = service.t("tui.detail_binary_body")
        if len(body) > 4000:
            # a 200 KB dump into one Static is unusable and slow to render
            body = body[:4000] + "…\n" + service.t("ux.read_full_hint")
        body_line = _detail_section(
            service.t("tui.detail_body"), body or service.t("tui.mail_no_body")
        )
        if record.mail.image_text.strip():
            body_line.append("\n\n")
            body_line.append(
                _detail_section(service.t("mail.field_image_text"), record.mail.image_text)
            )
        self._set_static_text("#mail-body", body_line)
        attachments_line: RichText | None = None
        if record.mail.attachments:
            attachments_line = _detail_section(service.t("tui.detail_attachments"), "")
            for index, attachment in enumerate(record.mail.attachments):
                if index:
                    attachments_line.append("\n\n")
                attachments_line.append("• ", style=_DETAIL_HEADING)
                attachments_line.append(attachment.filename, style="bold $ctp-blue")
                attachments_line.append(
                    f"\n{attachment.content_type} · {attachment.size} B", style=_DETAIL_MUTED
                )
        self._set_static_text("#mail-attachments", attachments_line or RichText(""))
        reply_line = RichText()
        reply_line.append(service.t("tui.urgency_label") + ": ", style=_DETAIL_MUTED)
        reply_line.append(
            urgency_label(service, record.effective_urgency),
            style="bold " + urgency_style(record.effective_urgency),
        )
        marker = service.t(
            "tui.detail_manual_marker"
            if record.manual_urgency is not None
            else "tui.detail_auto_marker"
        )
        reply_line.append(f" ({marker})", style=_DETAIL_MUTED)
        if record.analysis and record.analysis.reply_required:
            reply_line.append(
                "\n" + service.t("tui.detail_reply_required", answer=service.t("common.yes")),
                style="bold $ctp-yellow",
            )
        if existing:
            reply_line.append(
                "\n" + service.t("tui.feedback_marker") + ": ", style=_DETAIL_HEADING
            )
            reply_line.append(existing)
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
        if node.content != content:
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

    def _set_static(self, selector: str, content: str | RichGroup) -> None:
        node = self.query_one_optional(selector, Static)
        if node is None:
            return
        node.update(content)
        # an empty Static still occupies a row: hiding it keeps the detail pane
        # flush at the top instead of leaving blank lines above the summary
        node.display = bool(content)

    def _set_operation_status(self, content: str | RichMarkdown) -> None:
        """Keep operation results and errors in the existing detail area."""
        self._operation_status = content
        self._show_operation_status()

    def _show_operation_status(self) -> None:
        table = self._mail_table()
        messages: list[str] = []
        if table is not None and table.selection_mode:
            messages.append(self._service.t("tui.selection_mode_active"))
        self._set_static("#mail-operation-status", _status_content(self._operation_status, messages))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        try:
            await self._handle_button_pressed(event)
        except Exception as exc:
            message = f"[$ctp-red]{escape(error_message(self._service, exc))}[/$ctp-red]"
            self._set_operation_status(message)

    async def _handle_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "smart-action":
            self.run_worker(self._toggle_smart_action(), group="smart-action", exit_on_error=False)
            return
        if button_id == "btn-more":
            self.run_worker(
                self._mail_tools(), exclusive=True, group="mail-tools", exit_on_error=False
            )
            return
        if button_id == "btn-refresh":
            self._smart_action_result = None
            self._smart_scope.clear()
            self._smart_scope_name = ""
            self._smart_history.clear()
            await self.refresh_mail()
            return
        if button_id == "btn-purge-expired":
            # app-owned worker: a pane remount (language switch) cancels the
            # pane's own workers, which would stop a half-finished purge
            if not self._trash_busy:
                self._start_trash(self._purge_expired)
            return
        if button_id == "btn-trash":
            if self._trash_busy:
                self._trash_stop = True
                self._sync_selection_controls()
            else:
                targets = self._mail_targets()
                if targets:
                    self._start_trash(lambda: self._trash_selected(targets))
            return
        if button_id in self._REPARSE_BUTTONS:
            if self._reparse_active():
                self._stop_reparse()
                return
            if self._trash_busy:
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
        targets = self._mail_targets()
        if len(targets) != 1:
            return
        selected_id = targets[0]
        if button_id == "btn-ask-correct":
            record = await self._service.get_mail(selected_id)
            if record is None:
                return
            from mailflow_tui.ask_correct import AskCorrectModal

            cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                AskCorrectModal(self._service, record)
            )
            return
        if button_id == "btn-reply":
            record = await self._service.get_mail(selected_id)
            if record is not None:
                # textual types Widget.app loosely; the cast pins the concrete app
                cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
                    ReplyModal(self._service, record)
                )

    async def _pick_priority(self, targets: list[str]) -> None:
        from mailflow_tui.pickers import ChoicePicker

        value = await self._ask_screen(ChoicePicker(self._service, self._urgency_options()))
        if value is not None and targets:
            urgency = None if value == "auto" else Urgency(value)
            self._start_trash(
                lambda: self._update_mails(
                    targets, lambda ident: self._service.set_mail_urgency(ident, urgency)
                )
            )

    async def _mail_tools(self, choice: str | None = None) -> None:
        from mailflow_tui.mail_workflows import TrashScreen

        if self._trash_busy or self._reparse_active():
            return
        targets = self._mail_targets()
        selected_id = targets[0] if len(targets) == 1 else ""
        table = self._mail_table()
        scope = self._service.t(
            "tui.batch_scope",
            count=len(targets),
            hidden=len(set(targets) - set(table.visible_keys())) if table is not None else 0,
        )
        choice = choice or await self._ask_screen(
            MailMenu(self._service, scope=scope, count=len(targets))
        )
        if not choice:
            return
        try:
            if choice == "priority":
                await self._pick_priority(targets)
                return
            elif choice == "delete_selected":
                self._start_trash(lambda: self._trash_selected(targets))
                return
            elif choice == "reparse_selected":
                if not getattr(self._service, "remote", False) and not self._reparse_active():
                    self._start_reparse("btn-reparse", self._reparse_one(targets))
                return
            elif choice == "undo_delete":
                self._start_trash(self._undo_trash)
                return
            elif choice == "sync_mail":
                from mailflow_tui.mail_workflows import SyncScreen

                await self._ask_screen(SyncScreen(self._service, self._query.account_id))
            elif choice == "attachment_library":
                from mailflow_tui.workspace_screens import AttachmentLibraryScreen

                chosen = await self._ask_screen(
                    AttachmentLibraryScreen(self._service, self._query.account_id)
                )
                if chosen:
                    self.select_mail(str(chosen))
            elif choice == "source_sync":
                from mailflow_tui.workspace_screens import SourceSyncScreen

                await self._ask_screen(SourceSyncScreen(self._service, targets))
                await self.refresh_mail()
            elif choice in {"compose", "forward", "reply_all"}:
                from mailflow_tui.workspace_screens import ComposeScreen

                await self._ask_screen(
                    ComposeScreen(
                        self._service,
                        kind="new" if choice == "compose" else choice,
                        mail_id=selected_id,
                    )
                )
            elif choice == "thread":
                from mailflow_tui.workspace_screens import ThreadScreen

                chosen = await self._ask_screen(
                    ThreadScreen(self._service, selected_id)
                )
                if chosen:
                    self.select_mail(chosen)
            elif choice == "saved_filters":
                from mailflow_tui.workspace_screens import SavedFiltersScreen

                current = self._query.model_copy(
                    update={
                        "text": self.query_one("#mail-search", PromptInput).value,
                        "urgency": self._select_value("#mail-urgency-filter")
                        if self._select_value("#mail-urgency-filter") not in {"all", "expired"}
                        else "",
                    }
                )
                query = await self._ask_screen(SavedFiltersScreen(self._service, current))
                if query is not None:
                    self._query = query
                    self.query_one("#mail-search", PromptInput).value = query.text
                    self.query_one("#mail-urgency-filter", Select).value = query.urgency or "all"
                    self._page_limit = 200
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
                                else self.query_one("#mail-search", PromptInput).value,
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
                    if (
                        draft.kind != "reply"
                        or draft.recipients
                        or draft.cc
                        or draft.bcc
                        or draft.attachments
                    ):
                        from mailflow_tui.workspace_screens import ComposeScreen

                        await self._ask_screen(ComposeScreen(self._service, draft=draft))
                    elif record is not None:
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
                record = await self._service.get_mail(selected_id)
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
                self._start_trash(
                    lambda: self._update_mails(
                        targets,
                        lambda ident: self._service.set_mail_state([ident], **{field: value}),
                    )
                )
                return
            await self.refresh_mail()
        except Exception as exc:
            self._set_operation_status(escape(error_text(exc, self._service.t)))

    _REPARSE_CONCURRENCY = 3
    """Mails analyzed at once during a bulk run: a slow endpoint makes a strictly
    sequential sweep feel endless, while a small window keeps rate limits sane."""

    async def _reparse_batch(self, mails: list[Any], record_ids: list[str]) -> None:
        """Force re-analysis for the given messages, with per-mail progress.

        Runs a small window of mails concurrently, stops when the user asks to
        (the worker is cancelled, so an in-flight request ends at once instead
        of holding the UI until its timeout) and reports how many were really
        analyzed. Whatever finished stays analyzed.
        """
        total = len(mails)
        done = 0
        failed: list[str] = []
        succeeded: set[str] = set()
        completed = 0
        lock = asyncio.Lock()
        window = asyncio.Semaphore(self._REPARSE_CONCURRENCY)

        async def _analyze(mail: Any, identity: str) -> None:
            nonlocal done, completed
            async with window:
                if self._reparse_stop or not self.is_mounted:
                    return
                async with lock:
                    position = completed + 1
                    completed += 1
                subject_short = escape((mail.subject or "")[:36])
                self._set_operation_status(
                    f"[$ctp-sapphire]{self._service.t('tui.history_progress', position=position, total=total)} "
                    f"{subject_short}[/$ctp-sapphire]"
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
                    succeeded.add(identity)

        if self.is_mounted:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(
                    *(
                        _analyze(mail, identity)
                        for mail, identity in zip(mails, record_ids, strict=True)
                    )
                )
        table = self._mail_table()
        if table is not None:
            table.set_checks((table.checked_keys | set(record_ids)) - succeeded)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.refresh_mail()
        if self._reparse_stop:
            self._set_operation_status(
                f"[$ctp-yellow]{self._service.t('tui.reparse_stopped', count=done, total=total)}[/$ctp-yellow]"
            )
        elif failed:
            detail = "; ".join(failed[:3])
            more = f" (+{len(failed) - 3})" if len(failed) > 3 else ""
            self._set_operation_status(
                f"[$ctp-red]{self._service.t('tui.history_failed', count=len(failed))}: "
                f"{escape(detail)}{more}[/$ctp-red]"
            )
        else:
            self._set_operation_status(
                f"[$ctp-green]{self._service.t('tui.history_reanalyzed', count=done)}[/$ctp-green]"
            )

    async def _reparse_failed(self) -> None:
        try:
            failed_records = await self._service.list_failed_mails()
            if not failed_records:
                self._set_operation_status(
                    f"[$ctp-green]{self._service.t('tui.reparse_none_failed')}[/$ctp-green]"
                )
                return
            # immediate feedback before the first LLM call: each re-analysis can
            # take seconds (and may rate-limit), so without this the bulk
            # action looks like it never started
            self._set_operation_status(
                f"[$ctp-sapphire]{self._service.t('tui.reparse_failed_start', count=len(failed_records))}[/$ctp-sapphire]"
            )
            await self._reparse_batch(
                [record.mail for record in failed_records],
                [record.record_id for record in failed_records],
            )
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
        self._reparse_worker = self.run_worker(
            _run(), exclusive=True, group="mail-reparse", exit_on_error=False
        )
        self._set_reparse_buttons(button_id)
        self._sync_selection_controls()

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
        self._set_operation_status(f"[$ctp-yellow]{self._service.t('tui.reparse_stopping')}[/$ctp-yellow]")

    async def _reparse_one(self, targets: list[str] | None = None) -> None:
        """Re-analyze the selected mail, with the same run/cancel controls."""
        try:
            targets = self._mail_targets() if targets is None else targets
            if not targets:
                self._set_operation_status(
                    f"[$ctp-yellow]{self._service.t('tui.history_none_selected')}[/$ctp-yellow]"
                )
                return
            records = [
                record
                for ident in targets
                if (record := await self._service.get_mail(ident)) is not None
            ]
            if not records:
                self._set_operation_status(
                    f"[$ctp-yellow]{self._service.t('tui.mail_no_match')}[/$ctp-yellow]"
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
                f"[$ctp-sapphire]{self._service.t('tui.reparse_one_start', subject=subject_short)}[/$ctp-sapphire]"
            )
            await self._reparse_batch(
                [record.mail for record in records], [r.record_id for r in records]
            )
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
                self._set_operation_status(f"[$ctp-yellow]{self._service.t('tui.mail_empty')}[/$ctp-yellow]")
                return
            confirmed = await self._confirm(
                title=self._service.t("tui.reparse_all_title"),
                body=self._service.t("tui.reparse_all_body", count=len(records)),
                confirm_label=self._service.t("tui.btn_reparse_all"),
            )
            if not confirmed:
                return
            self._set_operation_status(
                f"[$ctp-sapphire]{self._service.t('tui.reparse_all_start', count=len(records))}[/$ctp-sapphire]"
            )
            await self._reparse_batch(
                [record.mail for record in records], [r.record_id for r in records]
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # remote mode, provider failures
            self._report_bulk_failure(exc)

    def _start_trash(self, operation: Callable[[], Awaitable[None]]) -> None:
        if self._trash_busy or self._reparse_active():
            return
        self._trash_busy = True
        self._trash_stop = False
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
                self._trash_stop = False
                if self.is_mounted:
                    self._sync_selection_controls()

        cast(MailFlowApp, self.app).run_worker(  # pyright: ignore[reportUnknownMemberType]
            run(), exclusive=True, group="mail-trash", exit_on_error=False
        )

    def _mail_batch_progress(self, done: int, total: int) -> None:
        self._set_operation_status(self._service.t("tui.batch_progress", done=done, total=total))

    async def _update_mails(
        self, targets: list[str], apply: Callable[[str], Awaitable[object]]
    ) -> None:
        result, pending = await _run_batch(
            self._service, targets, apply, lambda: self._trash_stop, self._mail_batch_progress
        )
        table = self._mail_table()
        if table is not None:
            unsynced = {
                identity
                for identity, state in result.source_results.items()
                if state["state"] != "synced"
            }
            table.set_checks(set(result.failed) | set(pending) | unsynced)
        await self.refresh_mail()
        self._set_operation_status(escape(_batch_feedback(self._service, result, pending)))

    def _mail_preview(self, records: list[MailRecord]) -> str:
        lines = [
            f"• {r.mail.subject or self._service.t('tui.mail_no_subject')} — {r.mail.sender.display} ({r.mail.account_id})"
            for r in records[:20]
        ]
        if len(records) > 20:
            lines.append(self._service.t("tui.selection_more", count=len(records) - 20))
        return "\n".join(lines)

    async def _trash_selected(self, targets: list[str] | None = None) -> None:
        from mailflow_tui.mail_workflows import TrashChoiceScreen

        targets = self._mail_targets() if targets is None else targets
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
        async def move(ident: str) -> object:
            if expired:
                return bool(await self._service.purge_expired_mails([ident]))
            return await self._service.batch_mail([ident], delete_actions=delete_actions)

        result, pending = await _run_batch(
            self._service, targets, move, lambda: self._trash_stop, self._mail_batch_progress
        )
        if result.succeeded:
            self._undo_trash_ids = list(result.succeeded)
        table = self._mail_table()
        if table is not None:
            table.set_checks(set(result.failed) | set(pending))
        await self.refresh_mail()
        message = self._service.t("tui.mail_delete_done", count=len(result.succeeded))
        if result.failed or result.skipped or pending or result.source_results:
            message += "\n" + _batch_feedback(self._service, result, pending)
        self._set_operation_status(escape(message))

    def action_undo_trash(self) -> None:
        if self._trash_busy or not self._undo_trash_ids:
            return
        self._start_trash(self._undo_trash)

    async def _undo_trash(self) -> None:
        result, pending = await _run_batch(
            self._service,
            list(self._undo_trash_ids),
            self._service.restore_mail,
            lambda: self._trash_stop,
            self._mail_batch_progress,
        )
        self._undo_trash_ids = [*result.failed, *pending]
        await self.refresh_mail()
        message = self._service.t("tui.mail_restore_done", count=len(result.succeeded))
        if result.failed or result.skipped or pending:
            message += "\n" + _batch_feedback(self._service, result, pending)
        self._set_operation_status(escape(message))

    async def _purge_expired(self) -> None:
        """Move mail that has nothing left to act on to the trash."""
        try:
            expired = await self._service.list_expired_mails()
            if not expired:
                self._set_operation_status(
                    f"[$ctp-green]{self._service.t('tui.purge_expired_none')}[/$ctp-green]"
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
        self._set_operation_status(f"[$ctp-red]{escape(error_message(self._service, exc))}[/$ctp-red]")

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
        self._set_operation_status(f"[$ctp-green]{self._service.t('tui.profile_saved')}[/$ctp-green]")

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
        self.run_worker(
            self._focus_mail(mail_id), exclusive=True, group="mail-navigation", exit_on_error=False
        )

    async def _focus_mail(self, mail_id: str) -> None:
        try:
            async with self._refresh_lock:
                record = await self._service.get_mail(mail_id)
                table = self._mail_table()
                if table is None:
                    return
                if record is None:
                    self._selected_id = None
                    table.set_checks(set())
                    table.show_cursor = False
                    self._clear_mail_detail()
                    self._set_static_text(
                        "#mail-analysis-status",
                        RichText(self._service.t("tui.action_mail_missing"), style="$ctp-yellow"),
                    )
                    self._sync_selection_controls()
                    return
                self._mail_filter_generation += 1
                search = self.query_one_optional("#mail-search", PromptInput)
                if search is not None:
                    with search.prevent(TextArea.Changed):
                        search.value = ""
                _safe_set_value(_typed_select(self, "#mail-urgency-filter"), "all")
                self._query = MailQuery(state="all")
                self._smart_action_result = None
                self._smart_query_text = ""
                self._selected_id = mail_id
                table.set_checks({mail_id})
                await self._refresh_mail_unlocked()

                def focus() -> None:
                    if self._selected_id == mail_id and mail_id in table.visible_keys():
                        table.focus()
                        table.move_cursor(row=table.get_row_index(mail_id), animate=False)

                table.call_after_refresh(focus)
        except Exception as exc:
            self._set_static("#mail-analysis-status", _load_error(self._service, exc))


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
        self._delete_stop = False
        self._action_status: str | RichMarkdown = ""
        self._category_filter = ""
        self._tag_filter = ""
        self._type_filters: set[str] = set()
        self._detail_item_id: str | None = None
        self._smart_action_running = False
        self._smart_action_result: SmartActionResult | None = None
        self._smart_query_text = ""
        self._smart_history: list[dict[str, str]] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="actions-search-row"):
            yield PromptInput(
                placeholder=self._service.t("tui.actions_search_placeholder"), id="actions-search"
            )
            yield SearchToggle("regex", ident="actions-search-regex")
            yield SearchToggle("case", ident="actions-search-case")
            yield Button(
                self._service.t("tui.smart_action"),
                id="actions-smart-action",
                variant="primary",
                disabled=True,
            )
        with Horizontal(id="actions-body"):
            yield SelectionTable(t=self._service.t, explorer_selection=True, id="actions-table")
            with Vertical(id="actions-detail"):
                with ScrollableContainer(id="actions-detail-scroll"):
                    yield Static("", id="actions-detail-content")
                yield Button(
                    self._service.t("tui.action_open_source"),
                    id="actions-source-mail",
                    variant="primary",
                    disabled=True,
                )
        with Horizontal(id="actions-buttons"):
            yield Button(
                self._service.t("tui.btn_add_todo"),
                id="actions-add",
                variant="success",
            )
            yield Button(
                self._service.t("tui.btn_edit"),
                id="actions-edit",
                variant="primary",
            )
            yield Button(
                self._service.t("tui.btn_delete_todo"),
                id="actions-delete",
                variant="error",
            )
            yield Button(
                self._service.t("tui.todo_types_filter"),
                id="actions-type-filter",
                variant="success",
            )
        yield Static(self._service.t("tui.empty"), id="actions-hint")

    async def on_mount(self) -> None:
        self.set_interval(60, self._refresh_action_ages)
        self.run_worker(
            self.refresh_actions(),
            exclusive=True,
            group="actions-refresh",
            exit_on_error=False,
        )

    async def _refresh_action_ages(self) -> None:
        if self.is_on_screen and self._actions_loaded and not self._delete_busy:
            await self.refresh_actions(reload=False)

    async def select_action(self, item_id: str) -> None:
        self._actions_filter_generation += 1
        self._category_filter = self._tag_filter = ""
        self._type_filters.clear()
        self._smart_action_result = None
        search = self.query_one("#actions-search", PromptInput)
        with search.prevent(TextArea.Changed):
            search.value = ""
        try:
            await self.refresh_actions()
            table = self._actions_table()
            if table is not None and item_id in table.visible_keys():
                table.set_checks({item_id})
                table.move_cursor(row=table.get_row_index(item_id), animate=False)
                table.focus()
                self._sync_action_controls()
            else:
                self.notify(self._service.t("console.source_missing"), severity="warning")
        except Exception as exc:
            self.notify(error_message(self._service, exc), severity="error")

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
        table.add_column(self._service.t("tui.action_time"), key="time", width=11)
        table.add_column(self._service.t("tui.action_type"), key="type", width=10)
        table.add_column(self._service.t("tui.action_content"), key="content")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        table.tooltip = None
        self._columns_done = True

    async def relabel(self) -> None:
        self._columns_done = False
        _relabel_buttons(
            self,
            {
                "actions-add": "tui.btn_add_todo",
                "actions-edit": "tui.btn_edit",
                "actions-delete": "tui.btn_delete_todo",
                "actions-status": "interface.action_status",
                "actions-more": "interface.more",
                "actions-smart-action": "tui.smart_action",
                "actions-source-mail": "tui.action_open_source",
            },
        )
        search = self.query_one_optional("#actions-search", PromptInput)
        if search is not None:
            search.placeholder = self._service.t("tui.actions_search_placeholder")
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
        search = self.query_one_optional("#actions-search", PromptInput)
        regex = self.query_one_optional("#actions-search-regex", SearchToggle)
        case = self.query_one_optional("#actions-search-case", SearchToggle)
        smart_result = (
            self._smart_action_result
            if search is not None and search.value.strip() == self._smart_query_text
            else None
        )
        matcher = build_search_matcher(
            search.value if search is not None and smart_result is None else "",
            regex=bool(regex.value) if regex is not None else False,
            case_sensitive=bool(case.value) if case is not None else False,
        )
        if matcher.error:
            hint = self.query_one_optional("#actions-hint", Static)
            if hint is not None:
                hint.update(
                    f"[$ctp-red]{self._service.t('tui.invalid_regex')}: {escape(self._service.t('errors.invalid_value'))}[/$ctp-red]"
                )
            return
        self._actions_matcher = matcher
        # a refresh must not throw the user out of the list they were reading:
        # remember the cursor row and scroll offset, restore them below when
        # the row still exists
        view = TableView.capture(table)
        self._ensure_columns()
        table.unselectable_keys = {item.item_id for item in loaded if item.preview}
        table.retain_checks({item.item_id for item in loaded if not item.preview})
        items = list(self._items)
        items = [
            item
            for item in items
            if (not self._category_filter or item.category == self._category_filter)
            and (not self._tag_filter or self._tag_filter in item.tags)
        ]
        if self._type_filters:
            items = [item for item in items if item.action_type in self._type_filters]
        if smart_result is not None:
            found = {item.item_id for item in smart_result.actions}
            items = [item for item in items if item.item_id in found]
        if matcher.query:
            items = [
                item
                for item in items
                if matcher.matches(
                    f"{item.summary} {item.action_type} {item.notes} {item.location} {item.url} {item.category} {' '.join(item.tags)}"
                )
            ]
        hint = self.query_one_optional("#actions-hint", Static)
        if hint is not None:
            hint.update(self._service.t("tui.empty") if not items else _BLANK)
        rows: list[tuple[str, tuple[Any, ...]]] = []
        for item in items:
            style = "$mf-muted" if item.preview else _action_age_style(self._service, item)
            rows.append(
                (
                    item.item_id,
                    (
                        RichText(
                            _localize(self._service, item.due_at, "%m-%d %H:%M")
                            if item.due_at is not None
                            else self._service.t("ux.no_due"),
                            style=style,
                        ),
                        RichText(_action_type_label(self._service, item.action_type), style=style),
                        RichText(
                            (self._service.t("recurrence.preview") + " · " if item.preview else "")
                            + " ".join(item.summary.split()),
                            style=style,
                        ),
                    ),
                )
            )
        if update_table(table, rows):
            view.restore(table)
        table.restore_selection(select_initial=view.row_key is None)
        self._sync_action_controls()

    def on_selection_table_selection_changed(self, event: SelectionTable.SelectionChanged) -> None:
        if event.table.id == "actions-table":
            self._sync_action_controls()

    def on_selection_table_menu_requested(self, event: SelectionTable.MenuRequested) -> None:
        if event.table.id == "actions-table" and not self._delete_busy:
            event.stop()
            if not self._task_targets():
                self.run_worker(self._more_actions(), group="actions-menu", exit_on_error=False)
                return
            self._delete_busy = True
            self._delete_stop = False
            self._sync_action_controls()
            self.run_worker(
                self._run_task_status(), group="actions-status", exclusive=True, exit_on_error=False
            )

    async def on_selection_table_delete_requested(
        self, event: SelectionTable.DeleteRequested
    ) -> None:
        if event.table.id == "actions-table":
            event.stop()
            if not self._delete_busy:
                await self._handle_action("actions-delete")

    def on_data_table_row_highlighted(self, event: TableRowEvent) -> None:
        if event.data_table.id == "actions-table":
            self._sync_action_controls()

    def _show_action_detail(self) -> None:
        table = self._actions_table()
        node = self.query_one_optional("#actions-detail-content", Static)
        button = self.query_one_optional("#actions-source-mail", Button)
        if table is None or node is None or button is None:
            return
        key = (
            table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
            if 0 <= table.cursor_row < table.row_count
            else None
        )
        item = next((item for item in self._items if item.item_id == key), None)
        item_id = item.item_id if item is not None else None
        content = _action_detail_text(self._service, item) if item is not None else ""
        if node.content != content:
            node.update(content)
        button.disabled = self._delete_busy or item is None or not item.mail_id
        if item_id != self._detail_item_id:
            detail = self.query_one("#actions-detail-scroll", ScrollableContainer)
            detail.scroll_home(animate=False)
        self._detail_item_id = item_id

    def _sync_action_controls(self) -> None:
        self._sync_smart_button()
        self._show_action_detail()
        table = self._actions_table()
        if table is None:
            return
        count = len(table.checked_keys)
        current = self._selected_item()
        button = self.query_one_optional("#actions-delete", Button)
        if button is not None:
            button.label = (
                self._service.t("tui.batch_stopping" if self._delete_stop else "tui.batch_stop")
                if self._delete_busy
                else self._service.t("tui.btn_delete_selected", count=count)
                if count
                else self._service.t("tui.btn_delete_todo")
            )
            button.disabled = self._delete_stop if self._delete_busy else not count
            button.tooltip = None
        table.disabled = self._delete_busy
        edit = self.query_one_optional("#actions-edit", Button)
        if edit is not None:
            edit.tooltip = None
            edit.disabled = self._delete_busy or current is None or count != 1
        status = self.query_one_optional("#actions-status", Button)
        if status is not None:
            status.disabled = self._delete_busy or not count
        type_filter = self.query_one_optional("#actions-type-filter", Button)
        if type_filter is not None:
            type_filter.label = self._service.t(
                "tui.todo_types_filter_cancel" if self._type_filters else "tui.todo_types_filter"
            )
            type_filter.variant = "error" if self._type_filters else "success"
            type_filter.disabled = self._delete_busy
        add = self.query_one_optional("#actions-add", Button)
        if add is not None:
            add.disabled = self._delete_busy
        hint = self.query_one_optional("#actions-hint", Static)
        if hint is not None:
            messages: list[str] = []
            if not count and not table.row_count and not self._action_status:
                messages.append(self._service.t("tui.empty"))
            if table.selection_mode:
                messages.append(self._service.t("tui.selection_mode_active"))
            hint.update(_status_content(self._action_status, messages))

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

    async def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "actions-search":
            self._sync_smart_button()
            if self._smart_action_running:
                return
            self._schedule_actions_filter()

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        if event.input.id == "actions-search":
            event.stop()
            self.run_worker(
                self._open_smart_action(event.value), group="actions-smart-action", exit_on_error=False
            )

    def _sync_smart_button(self) -> None:
        button = self.query_one_optional("#actions-smart-action", Button)
        search = self.query_one_optional("#actions-search", PromptInput)
        if button is not None:
            button.disabled = (
                self._smart_action_running
                or self._delete_busy
                or search is None
                or not search.value.strip()
            )

    async def _open_smart_action(self, instruction: str | None = None) -> None:
        text = (
            instruction
            if instruction is not None
            else self.query_one("#actions-search", PromptInput).value
        ).strip()
        if self._smart_action_running or self._delete_busy or not text:
            return
        self._smart_action_running = True
        self._actions_filter_generation += 1
        self._sync_smart_button()
        panel = SmartActionReviewModal(self._service, text, conversation=self._smart_history[-12:])
        try:
            result = await cast(Any, self.app).push_screen_wait(panel)
            if result is None:
                self._action_status = escape(panel.cancel_message)
            else:
                self._smart_action_result = result
                self._smart_query_text = text
                self._smart_history.extend(
                    [
                        {"role": "user", "content": text},
                        {
                            "role": "assistant",
                            "content": result.final_text
                            + "\n"
                            + "\n".join(
                                [f"task {a.item_id}: {a.summary}" for a in result.actions]
                                + [f"mail {r.record_id}: {r.mail.subject}" for r in result.records]
                            ),
                        },
                    ]
                )
                answer = "\n\n".join(filter(None, [result.final_text, *result.failure_reasons]))
                self._action_status = (
                    RichMarkdown(answer) if answer else self._service.t("tui.smart_action_done")
                )
            await self.refresh_actions()
        except Exception as exc:
            self._action_status = escape(error_message(self._service, exc))
        finally:
            self._smart_action_running = False
            self._sync_action_controls()

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
        """Resolve the sole selected item, independent of the keyboard cursor."""
        table = self._actions_table()
        if table is None or len(table.checked_keys) != 1:
            return None
        selected = table.checked_keys & set(table.visible_keys())
        return next((item for item in self._items if item.item_id in selected), None)

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
        table = self._actions_table()
        if self._delete_busy or (table is not None and len(table.checked_keys) > 1):
            return
        item = self._selected_item()
        if item is None:
            return
        cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
            TodoEditModal(self._service, item), callback=self._refresh_after_edit
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "actions-smart-action":
            event.stop()
            self.run_worker(
                self._open_smart_action(), group="actions-smart-action", exit_on_error=False
            )
            return
        try:
            await self._handle_action(event.button.id)
        except Exception as exc:
            message = f"[$ctp-red]{escape(error_message(self._service, exc))}[/$ctp-red]"
            self.query_one("#actions-hint", Static).update(message)

    async def _more_actions(self) -> None:
        from mailflow_tui.pickers import ChoicePicker

        choice = await cast(Any, self).app.push_screen_wait(
            ChoicePicker(
                self._service,
                [
                    (self._service.t(key), ident)
                    for key, ident in (
                        ("experience.action_filters", "actions-filters"),
                        ("experience.plans", "actions-plans"),
                        ("assistant.handoff", "actions-assistant"),
                    )
                ],
            )
        )
        if choice is not None:
            try:
                await self._handle_action(choice)
            except Exception as exc:
                hint = self.query_one("#actions-hint", Static)
                hint.update(escape(error_message(self._service, exc)))
                hint.display = True

    async def _handle_action(self, button_id: str | None) -> None:
        if button_id == "actions-source-mail":
            item = next((i for i in self._items if i.item_id == self._detail_item_id), None)
            if not self._delete_busy and item is not None and item.mail_id:
                cast(MailFlowApp, self.app).open_mail(item.mail_id)
            return
        if button_id == "actions-type-filter":
            if self._delete_busy:
                return
            if self._type_filters:
                self._type_filters.clear()
                await self.refresh_actions(reload=False)
            else:
                modal = ActionTypesModal(self._service)

                def closed(selected: set[str] | None) -> None:
                    if selected is not None:
                        self._type_filters = selected
                    self.run_worker(
                        self.refresh_actions(), group="actions-refresh", exit_on_error=False
                    )

                cast(Any, self.app).push_screen(modal, closed)
            return
        if button_id == "actions-more":
            self.run_worker(
                self._more_actions(), group="actions-menu", exclusive=True, exit_on_error=False
            )
            return
        if button_id in {"actions-filters", "actions-plans"}:
            from mailflow_tui.workspace_screens import ActionFiltersScreen, PlansScreen

            def closed(value: Any) -> None:
                if value is not None and button_id == "actions-filters":
                    self._category_filter, self._tag_filter = value
                self.run_worker(
                    self.refresh_actions(), group="actions-refresh", exit_on_error=False
                )

            cast(Any, self).app.push_screen(
                ActionFiltersScreen(self._service, self._category_filter, self._tag_filter)
                if button_id == "actions-filters"
                else PlansScreen(self._service),
                closed,
            )
            return
        if button_id == "actions-assistant":
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

        if button_id == "actions-status":
            if self._delete_busy:
                return
            self._delete_busy = True
            self._delete_stop = False
            self._sync_action_controls()
            self.run_worker(
                self._run_task_status(),
                exclusive=True,
                group="actions-status",
                exit_on_error=False,
            )
            return
        if button_id == "actions-edit":
            self.edit_selected()
            return
        if button_id == "actions-add":

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
        if button_id != "actions-delete":
            return
        if self._delete_busy:
            self._delete_stop = True
            self._sync_action_controls()
            return
        targets = self._task_targets()
        if not targets:
            return
        self._delete_busy = True
        self._delete_stop = False
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
        except Exception as exc:
            self._action_status = escape(error_message(self._service, exc))
        finally:
            self._delete_busy = False
            self._delete_stop = False
            if self.is_mounted:
                self._sync_action_controls()

    def _task_targets(self) -> list[str]:
        table = self._actions_table()
        ids = (
            [key for key in table.visible_keys() if key in table.checked_keys]
            + sorted(table.checked_keys - set(table.visible_keys()))
            if table is not None
            else []
        )
        real_ids = {item.item_id for item in self._items if not item.preview}
        return [identity for identity in ids if identity in real_ids]

    def _task_batch_progress(self, done: int, total: int) -> None:
        self._action_status = self._service.t("tui.batch_progress", done=done, total=total)
        self._sync_action_controls()

    async def _confirm_task_batch(self, targets: list[str], *, complete: bool = False) -> bool:
        table = self._actions_table()
        hidden = table is not None and bool(set(targets) - set(table.visible_keys()))
        if len(targets) == 1 and not hidden and not complete:
            return True
        preview = "\n".join("• " + item.summary for item in self._items if item.item_id in targets)
        return bool(
            await cast(Any, self.app).push_screen_wait(
                ConfirmModal(
                    self._service,
                    title=self._service.t(
                        "ux.complete_tasks" if complete else "tui.btn_delete_selected",
                        count=len(targets),
                    ),
                    body=(
                        self._service.t(
                            "tui.complete_tasks_body" if complete else "tui.actions_delete_body",
                            count=len(targets),
                        )
                        + "\n\n"
                        + preview
                    ),
                    confirm_label=self._service.t(
                        "ux.complete_tasks" if complete else "tui.btn_delete"
                    ),
                    focus_confirm=False,
                )
            )
        )

    async def _change_task_status(self) -> None:
        from datetime import timedelta

        table = self._actions_table()
        ids = self._task_targets()
        if not ids:
            return
        app = cast("Any", self).app
        scope = self._service.t(
            "tui.batch_scope",
            count=len(ids),
            hidden=len(set(ids) - set(table.visible_keys())) if table is not None else 0,
        )
        choice = await app.push_screen_wait(TaskMenu(self._service, scope=scope))
        if not choice:
            return
        values: dict[str, Any] = {}
        if choice == "delete_tasks":
            await self._delete_actions(ids)
            return
        if choice == "change_type":
            value = await app.push_screen_wait(ActionTypePicker(self._service))
            if value is None:
                return
            values["action_type"] = value
        elif choice == "complete_tasks":
            if not await self._confirm_task_batch(ids, complete=True):
                return
            values["completed"] = True
        elif choice in {"mute_tasks", "enable_tasks"}:
            values["reminder_enabled"] = choice == "enable_tasks"
            if choice == "enable_tasks":
                values["clear_snooze"] = True
        else:
            values.update(
                snoozed_until=datetime.now(UTC)
                + timedelta(hours=1 if choice == "snooze_hour" else 24),
                reminder_enabled=True,
            )
        result, pending = await _run_batch(
            self._service,
            ids,
            lambda item_id: self._service.edit_action(item_id, **values),
            lambda: self._delete_stop,
            self._task_batch_progress,
        )
        if table is not None:
            table.set_checks(set(result.failed) | set(pending))
        self._action_status = escape(_batch_feedback(self._service, result, pending))
        await self.refresh_actions()

    async def _delete_actions(self, targets: list[str]) -> None:
        try:
            if not await self._confirm_task_batch(targets):
                return
            result, pending = await _run_batch(
                self._service,
                targets,
                self._service.delete_action,
                lambda: self._delete_stop,
                self._task_batch_progress,
            )
            table = self._actions_table()
            if table is not None:
                table.set_checks(set(result.failed) | set(pending))
            self._action_status = escape(_batch_feedback(self._service, result, pending))
            await self.refresh_actions()
        except Exception as exc:
            self._action_status = escape(error_message(self._service, exc))
        finally:
            self._delete_busy = False
            self._delete_stop = False
            if self.is_mounted:
                self._sync_action_controls()


class RuntimePane(Vertical):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._selected_plugin: str | None = None
        self._detail_open_for: str | None = None
        self._refresh_lock = asyncio.Lock()
        self._load_failed = False
        self._plugins: dict[str, PluginSnapshot] = {}
        self._document_signature: tuple[str, str, str] | None = None
        self._plugin_busy = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="runtime-body"):
            with Vertical(id="runtime-list"):
                yield Static(self._service.t("tui.runtime_plugins"), id="runtime-plugins")
                yield _RuntimePluginTable(
                    self._open_plugin_detail,
                    id="runtime-plugins-table",
                    cursor_type="row",
                )  # pyright: ignore[reportUnknownMemberType]
                yield Static(self._service.t("tui.runtime_plugins_help"), id="runtime-plugins-hint")
            with Vertical(id="runtime-detail"):
                yield Static(self._service.t("tui.runtime_select_plugin"), id="runtime-selection")
                with ScrollableContainer(id="runtime-scroll"):
                    yield Static("", id="runtime-description", markup=False)
                    yield Static("", id="runtime-plugin-meta", markup=False)
                    with Collapsible(
                        title=self._service.t("tui.runtime_overview"),
                        collapsed=True,
                        id="runtime-overview",
                    ):
                        yield Static("", id="runtime-adapters")
                        yield Static("", id="runtime-accounts")
                        yield Static("", id="runtime-llms")
                        yield Static("", id="runtime-bindings")
                        yield Static("", id="runtime-storage")
                    yield Markdown("", id="runtime-readme", open_links=False)
        with Horizontal(id="runtime-plugin-actions"):
            yield Button(
                self._service.t("tui.market_view_detail"),
                id="runtime-plugin-detail",
                variant="primary",
                disabled=True,
            )
            yield Button(
                self._service.t("tui.btn_disable"),
                id="runtime-plugin-disable",
                variant="warning",
                disabled=True,
            )
            yield Button(
                self._service.t("tui.btn_enable"),
                id="runtime-plugin-enable",
                variant="success",
                disabled=True,
            )
            yield Button(
                self._service.t("tui.btn_uninstall"),
                id="runtime-plugin-uninstall",
                variant="error",
                disabled=True,
            )
        yield Static("", id="runtime-status")

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
                "runtime-plugin-detail": "tui.market_view_detail",
                "runtime-plugin-disable": "tui.btn_disable",
                "runtime-plugin-enable": "tui.btn_enable",
                "runtime-plugin-uninstall": "tui.btn_uninstall",
            },
        )
        self.update_connection_status()
        self._set_static("#runtime-plugins-hint", self._service.t("tui.runtime_plugins_help"))
        self.query_one("#runtime-overview", Collapsible).title = self._service.t(
            "tui.runtime_overview"
        )
        await self.refresh_runtime()

    def _runtime_title(self) -> str:
        title = self._service.t("tui.runtime_plugins")
        status = remote_connection_status(self._service)
        return f"{title}\n{status}" if status else title

    def update_connection_status(self) -> None:
        self._set_static("#runtime-plugins", self._runtime_title())

    def _set_static(self, selector: str, content: str | RichText) -> None:
        node = self.query_one_optional(selector, Static)
        if node is not None and node.content != content:
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
        table.add_column(self._service.t("plugin.header_name"), key="name", width=26)
        table.add_column(self._service.t("plugin.market_status"), key="status")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        self._columns_done = True

    def _plugin_entry(self, plugin_id: str) -> MarketPlugin | None:
        app = cast(MailFlowApp, self.app)
        cached = app.plugin_entry(plugin_id)
        if cached is not None:
            plugin = self._plugins.get(plugin_id)
            return cached.model_copy(
                update={
                    "version": plugin.version if plugin else cached.version,
                    "categories": [kind.value for kind in plugin.kinds]
                    if plugin
                    else cached.categories,
                    "source": "local",
                }
            )
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return None
        return MarketPlugin(
            id=plugin.plugin_id,
            name=plugin.name,
            version=plugin.version,
            description=plugin.description,
            categories=[kind.value for kind in plugin.kinds],
            package=plugin.plugin_id,
            source="local",
        )

    def _status_text(self, status: str) -> str:
        return self._service.t(f"tui.plugin_status_{status}")

    @staticmethod
    def _status_style(status: str) -> str:
        return {
            "enabled": "$ctp-green",
            "disabled": "$ctp-yellow",
            "error": "$ctp-red",
        }.get(status, "$ctp-subtext0")

    def _sync_plugin_controls(self) -> None:
        plugin_id = self._selected_plugin or ""
        selected = plugin_id in self._plugins
        status = self._service.plugin_status(plugin_id) if selected else ""
        manager = getattr(self._service, "plugin_manager", None)
        bundled = self._selected_plugin in getattr(manager, "bundled_plugin_ids", ())
        plugin = self._plugins.get(self._selected_plugin or "")
        active_storage = (
            plugin is not None
            and ComponentKind.STORAGE in plugin.kinds
            and self._service.config.storage.provider in plugin.components
        )
        for ident, disabled in (
            ("detail", False),
            ("enable", status == "enabled"),
            ("disable", status == "disabled" or active_storage),
            ("uninstall", bundled or active_storage),
        ):
            self.query_one(f"#runtime-plugin-{ident}", Button).disabled = (
                not selected or self._plugin_busy or disabled
            )

    def show_plugin_description(self) -> None:
        """Update the selected plugin without replacing the list or its reading state."""
        plugin_id = self._selected_plugin
        plugin = self._plugin_entry(plugin_id) if plugin_id else None
        self._sync_plugin_controls()
        if plugin is None:
            self._document_signature = None
            self._set_static(
                "#runtime-selection",
                self._service.t(
                    "tui.runtime_select_plugin" if self._plugins else "tui.runtime_no_plugins"
                ),
            )
            self._set_static("#runtime-description", "")
            self._set_static("#runtime-plugin-meta", "")
            self.query_one("#runtime-readme", Markdown).update("")
            return
        status = self._service.plugin_status(plugin.id)
        title = RichText(
            plugin_text(self._service, plugin, "name") or plugin.id, style="bold $ctp-blue"
        )
        if plugin.version:
            title.append(f"  v{plugin.version}", style="$ctp-subtext0")
        title.append("  " + self._status_text(status), style=self._status_style(status))
        self._set_static("#runtime-selection", title)
        description = plugin_text(self._service, plugin, "description")
        self._set_static(
            "#runtime-description", description or self._service.t("tui.runtime_no_description")
        )
        capabilities = ", ".join(
            self._service.i18n.component_label(kind) for kind in plugin.categories
        )
        self._set_static(
            "#runtime-plugin-meta",
            plugin.id + (f"\n{capabilities}" if capabilities else ""),
        )
        signature = (plugin.id, self._service.i18n.language, plugin.model_dump_json())
        if signature == self._document_signature:
            return
        previous = self._document_signature
        self._document_signature = signature
        if previous is None or previous[0] != plugin.id:
            self.query_one("#runtime-readme", Markdown).update(self._service.t("tui.loading"))
            self.query_one("#runtime-scroll", ScrollableContainer).scroll_home(animate=False)
        self.run_worker(
            self._load_plugin_document(plugin, signature),
            exclusive=True,
            group="runtime-plugin-document",
            exit_on_error=False,
        )

    async def _load_plugin_document(
        self, plugin: MarketPlugin, signature: tuple[str, str, str]
    ) -> None:
        try:
            readme = await asyncio.to_thread(plugin_readme, self._service, plugin, signature[1])
            if not self.is_mounted or signature != self._document_signature:
                return
            if readme.startswith("# "):
                readme = readme.partition("\n")[2].lstrip()
            if (
                not readme.strip()
                or readme.strip() == plugin_text(self._service, plugin, "description").strip()
            ):
                readme = self._service.t("tui.runtime_no_readme")
            scroll = self.query_one("#runtime-scroll", ScrollableContainer)
            offset = (scroll.scroll_x, scroll.scroll_y)
            await self.query_one("#runtime-readme", Markdown).update(readme)
            if signature == self._document_signature:
                scroll.scroll_to(x=offset[0], y=offset[1], animate=False, force=True)
        except Exception as exc:
            if self.is_mounted and signature == self._document_signature:
                self._document_signature = None
                self.query_one("#runtime-readme", Markdown).update(_load_error(self._service, exc))

    async def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        try:
            self._service.open_url(event.href)
        except Exception as exc:
            self._set_static("#runtime-status", escape(error_message(self._service, exc)))

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
        self._plugins = {plugin.plugin_id: plugin for plugin in snapshot.plugins}
        previous_selection = self._selected_plugin
        scroll = self.query_one_optional("#runtime-scroll", ScrollableContainer)
        offset = (scroll.scroll_x, scroll.scroll_y) if scroll is not None else (0.0, 0.0)
        table = self._plugins_table()
        if table is None:
            return
        view = TableView.capture(table)
        self._ensure_columns()
        rows: list[tuple[str, tuple[Any, ...]]] = []
        for plugin in snapshot.plugins:
            status_key = service.plugin_status(plugin.plugin_id)
            entry = self._plugin_entry(plugin.plugin_id)
            name = (
                plugin_text(service, entry, "name")
                if entry is not None
                else service.i18n.plugin_text(plugin.plugin_id, "name", plugin.name)
            )
            description = (
                plugin_text(service, entry, "description")
                if entry is not None
                else service.i18n.plugin_text(plugin.plugin_id, "description", plugin.description)
            )
            label = RichText(overflow="ellipsis", no_wrap=True)
            label.append(name, style="bold $ctp-blue")
            label.append("\n" + " ".join(description.split()), style="$ctp-subtext0")
            rows.append(
                (
                    plugin.plugin_id,
                    (
                        label,
                        RichText(
                            self._status_text(status_key),
                            style=self._status_style(status_key),
                        ),
                    ),
                )
            )
        if update_table(table, rows, row_height=2) or self._selected_plugin not in self._plugins:
            self._selected_plugin = view.restore(table)
        self.show_plugin_description()
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
            f"[bold $ctp-blue]{service.t('tui.runtime_adapters')}[/bold $ctp-blue]\n{adapters or '  -'}",
        )
        self._set_static(
            "#runtime-accounts",
            f"[bold $ctp-teal]{service.t('tui.runtime_accounts')}[/bold $ctp-teal]\n{accounts or '  -'}",
        )
        self._set_static(
            "#runtime-llms",
            f"[bold $ctp-mauve]{service.t('tui.runtime_llms')}[/bold $ctp-mauve]\n{llms or '  -'}",
        )
        self._set_static(
            "#runtime-bindings",
            f"[bold $ctp-blue]{service.t('tui.runtime_bindings')}[/bold $ctp-blue]\n{bindings or '  -'}",
        )
        self._set_static(
            "#runtime-storage",
            f"[bold $ctp-green]{service.t('tui.runtime_storage')}[/bold $ctp-green]\n{snapshot.storage or '-'}",
        )
        if scroll is not None:
            scroll.call_after_refresh(
                lambda: (
                    scroll.scroll_to(x=offset[0], y=offset[1], animate=False, force=True)
                    if self._selected_plugin == previous_selection
                    else None
                )
            )

    async def on_data_table_row_selected(self, event: TableRowEvent) -> None:
        if event.data_table.id != "runtime-plugins-table":
            return
        plugin_id = cast(str, event.row_key.value)
        self._selected_plugin = plugin_id
        self.show_plugin_description()
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
            self.show_plugin_description()

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
            self._detail_open_for = None
            self.query_one("#runtime-status", Static).update(
                f"[$ctp-yellow]{self._service.t('plugin.unknown_plugin', plugin_id=plugin_id)}[/$ctp-yellow]"
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
        """Include installed plugins even when they are disabled or failed to load."""
        return self._plugin_entry(plugin_id)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if not button_id.startswith("runtime-plugin-"):
            return
        event.stop()
        if self._selected_plugin is None or self._plugin_busy:
            return
        if button_id == "runtime-plugin-detail":
            self._open_plugin_detail(self._selected_plugin)
            return
        # enable/disable rebuilds the runtime (bounded), uninstall runs pip:
        # neither belongs on the UI handler
        status = self.query_one("#runtime-status", Static)
        status.update(self._service.t("tui.loading"))
        self._plugin_busy = True
        self._sync_plugin_controls()
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
            status.update(f"[$ctp-red]{escape(error_message(self._service, exc))}[/$ctp-red]")
            return
        finally:
            self._plugin_busy = False
            if self.is_mounted:
                self._sync_plugin_controls()
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
        "napcat": ("napcat", "$ctp-blue"),
        "openwechat": ("openwechat", "$ctp-teal"),
        "wechatpadpro": ("wechatpadpro", "$ctp-mauve"),
        "telegram": ("telegram", "$ctp-sky"),
        "whatsapp": ("whatsapp", "$ctp-green"),
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

    # Categories use quiet accents; red is reserved for failures.
    _CATEGORY_STYLES: ClassVar[dict[str, str]] = {
        "chat": "$ctp-blue",
        "mail": "$ctp-teal",
        "llm": "$ctp-mauve",
        "parse": "$ctp-lavender",
        "notify": "$ctp-green",
        "storage": "$ctp-subtext0",
        "system": "$ctp-subtext0",
    }
    _LEVEL_STYLES: ClassVar[dict[str, str]] = {
        "ERROR": "bold $ctp-red",
        "CRITICAL": "bold $ctp-red",
        "WARNING": "$ctp-yellow",
        "INFO": "$ctp-blue",
        "DEBUG": "$ctp-subtext0",
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
        self._follow_logs = True
        self._source_signature = ""
        self._query = ""
        self._log_filter_generation = 0
        self._log_matcher = SearchMatcher("")
        self._rendered_logs: list[Any] = []
        self._pending_log_lines: list[str] | None = None
        self._log_render_lock = asyncio.Lock()
        # incremental rendering: newly pulled lines are appended to the
        # RichLog (capped by its own max_lines) instead of re-rendering the
        # whole 2000-line buffer every second — on slow terminals full
        # rebuilds stall the event loop. A full rebuild happens only when a
        # filter changes or the pane (re)mounts.

    def compose(self) -> ComposeResult:
        with Vertical(id="log-controls"):
            with Horizontal(id="log-search-row"):
                yield Input(
                    placeholder=self._service.t("tui.logs_search_placeholder"),
                    id="log-search",
                )
                yield SearchToggle("regex", ident="log-search-regex")
                yield SearchToggle("case", ident="log-search-case")
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
                yield Button(
                    self._service.t("tui.logs_export"),
                    id="logs-export",
                    variant="primary",
                )
        yield Static("", id="logs-search-status")
        yield RichLog(id="log-view", wrap=True, highlight=False, max_lines=self._MAX_LINES)

    async def on_mount(self) -> None:
        self.run_worker(
            cast(Any, self._render_logs), exclusive=True, group="logs-render", exit_on_error=False
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
        _relabel_buttons(self, {"logs-export": "tui.logs_export"})
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
            self._seen_sources.add(self._category_of("mailflow.tui"))
            self._refresh_source_options()
            self._append_new_lines([self._buffer[-1]])

        cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
            LogExportScreen(
                self._service,
                list(self._buffer),
                filtered=[line for line in self._buffer if self._line_to_text(line) is not None],
            ),
            callback=_on_saved,
        )

    def drain(self) -> None:
        # remounts (language switch, tab re-compose) can tick the interval
        # while the widget tree is detached — never crash the app for it
        pulled: list[str] = []
        for _ in range(200):
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
        if not self._log_queue.empty():
            self.call_after_refresh(self.drain)

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
        self._pending_log_lines = []
        try:
            log_view.clear()
            for index, line in enumerate(list(self._rendered_logs), start=1):
                log_view.write(line)
                if index % 50 == 0:
                    await asyncio.sleep(0)
        finally:
            pending, self._pending_log_lines = self._pending_log_lines, None
            self._append_new_lines(pending or [], preserve_follow=True)

    async def _render_logs(self, generation: int | None = None) -> None:
        async with self._log_render_lock:
            await self._rebuild_logs(generation)

    async def _rebuild_logs(self, generation: int | None = None) -> None:
        generation = self._log_filter_generation if generation is None else generation
        matcher = self._current_log_matcher()
        status = self.query_one_optional("#logs-search-status", Static)
        if matcher.error:
            if status is not None:
                status.update(
                    f"[$ctp-red]{self._service.t('tui.invalid_regex')}: {escape(self._service.t('errors.invalid_value'))}[/$ctp-red]"
                )
            await self._restore_rendered_logs()
            return
        self._log_matcher = matcher
        if status is not None:
            status.update("")
        log_view = self.query_one_optional("#log-view", RichLog)
        if log_view is None:
            return
        offset = log_view.scroll_y
        follow = self._follow_logs
        log_view.auto_scroll = follow
        self._pending_log_lines = []
        try:
            rendered: list[Any] = []
            for index, line in enumerate(list(self._buffer), start=1):
                text = self._line_to_text(line)
                if text is not None:
                    rendered.append(text)
                if index % 50 == 0:
                    await asyncio.sleep(0)
                    if generation != self._log_filter_generation:
                        return
            if generation != self._log_filter_generation:
                return
            log_view.clear()
            for index, line in enumerate(rendered, start=1):
                log_view.write(line)
                if index % 50 == 0:
                    await asyncio.sleep(0)
                    if generation != self._log_filter_generation:
                        return
            self._rendered_logs = rendered
        finally:
            pending, self._pending_log_lines = self._pending_log_lines, None
            self._append_new_lines(pending or [], preserve_follow=True)
        if not follow:
            log_view.call_after_refresh(
                lambda: log_view.scroll_to(y=offset, animate=False, force=True)
            )

    def _line_to_text(self, line: str) -> Any:
        """Format one buffered line under the current filters; '' means the
        line is filtered out (or is a non-log line that passes through)."""
        parts = line.split("|", 3)
        if len(parts) != 4:
            return (
                line
                if not self._source and (not self._query or self._log_matcher.matches(line))
                else None
            )
        time_part, level, logger_name, message = parts
        if self._LEVEL_RANK.get(level, 0) < self._LEVEL_RANK.get(
            self._min_level, self._LEVEL_RANK[self._DEFAULT_MIN_LEVEL]
        ):
            return None
        if self._query and not self._log_matcher.matches(message):
            return None
        from rich.text import Text

        category = self._category_of(logger_name)
        if self._source and category != self._source:
            return None
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
        text = Text(f"{time_part} ", style="$ctp-overlay1")
        text.append(f"{level:<7}", style=self._LEVEL_STYLES.get(level, "bold"))
        text.append(f" [{cat_label}{platform_suffix}]", style=cat_style)
        text.append(f"  {message}", style="$ctp-text")
        return text

    def _append_new_lines(self, new_lines: list[str], *, preserve_follow: bool = False) -> None:
        """Incremental path: write only rows added since the last drain."""
        if not new_lines:
            return
        if self._pending_log_lines is not None:
            self._pending_log_lines.extend(new_lines)
            self._pending_log_lines = self._pending_log_lines[-self._MAX_LINES :]
            return
        log_view = self.query_one_optional("#log-view", RichLog)
        if log_view is None:
            return  # detached: on remount, _render_logs rebuilds cleanly
        if not preserve_follow and log_view.lines:
            self._follow_logs = log_view.is_vertical_scroll_end
        log_view.auto_scroll = self._follow_logs
        for line in new_lines:
            text = self._line_to_text(line)
            if text is None:
                continue
            log_view.write(text)
            self._rendered_logs.append(text)
        self._rendered_logs = self._rendered_logs[-self._MAX_LINES :]

    def _schedule_log_filter(self) -> None:
        self._log_filter_generation += 1
        generation = self._log_filter_generation
        matcher = self._current_log_matcher()
        if matcher.error:
            status = self.query_one_optional("#logs-search-status", Static)
            if status is not None:
                status.update(
                    f"[$ctp-red]{self._service.t('tui.invalid_regex')}: {escape(self._service.t('errors.invalid_value'))}[/$ctp-red]"
                )
            self.run_worker(
                cast(Any, self._render_logs),
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


def plugin_readme(service: Any, plugin: MarketPlugin, language: str) -> str:
    """Use the same offline documentation in the inline panel and expanded reader."""
    readme = plugin_text(service, plugin, "readme", language=language)
    if readme:
        return readme
    name = plugin_text(service, plugin, "name", language=language) or plugin.id
    if plugin.source == "local" and not getattr(service, "remote", False):
        from mailflow.plugins import PluginInfo

        doc = plugin_doc_readme(PluginInfo(plugin_id=plugin.id))
        if doc:
            return f"# {name}\n\n{doc}"
    description = plugin_text(service, plugin, "description", language=language)
    return f"# {name}\n\n{description}" if description else ""


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
                    variant="error",
                )
                yield Button(
                    self._service.t("tui.btn_enable"), id="detail-enable", variant="success"
                )
                yield Button(
                    self._service.t("tui.btn_disable"), id="detail-disable", variant="warning"
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

    async def relabel(self) -> None:
        title = self.query_one_optional("#market-detail-title", Static)
        if title is not None:
            title.update(self._t("tui.market_detail"))
        _relabel_buttons(
            self,
            {
                "detail-install": "tui.btn_install",
                "detail-uninstall": "tui.btn_uninstall",
                "detail-enable": "tui.btn_enable",
                "detail-disable": "tui.btn_disable",
                "detail-close": "tui.btn_close",
            },
        )
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
            if cached is not None and plugin_text(
                self._service, cached, "readme", language=language
            ):
                plugin = cached
        readme = await asyncio.to_thread(self._readme_for, plugin, language)
        if language != self._service.i18n.language:
            return
        name = plugin_text(self._service, plugin, "name", language=language) or plugin.id
        meta = (
            f"**{name}** v{plugin.version} — `{plugin.id}`\n\n"
            f"{self._service.t('plugin.field_author')}: {plugin.author or '-'} · "
            f"{self._service.t('plugin.field_updated')}: {plugin.updated or '-'}\n"
            f"{self._service.t('plugin.field_homepage')}: {plugin.homepage or '-'}\n"
        )
        node = self.query_one_optional("#market-detail-readme", Markdown)
        if node is not None:
            node.update(meta + "\n---\n\n" + readme)
        self._content_loaded = True

    def _readme_for(self, plugin: MarketPlugin, language: str) -> str:
        return plugin_readme(self._service, plugin, language)

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
            link.update(f"[$ctp-sapphire]{escape(result)}[/$ctp-sapphire]")

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
        note = "plugin.applies_now" if plugin.categories == ["theme"] else "plugin.restart_note"
        self._set_status(
            self._service.t(message_key, plugin_id=plugin.id)
            + f" ({self._service.t(note)})"
        )
        await asyncio.sleep(0.4)
        self.dismiss(button_id)


class MarketPane(Vertical):
    """VS Code-style marketplace: search, category filter, list, markdown
    detail with install/uninstall/enable/disable."""

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._categories: list[str] | None = None
        self._entries: list[tuple[Repository, MarketPlugin]] = []
        self._selected: MarketPlugin | None = None
        self._installed: dict[str, bool] = {}
        self._loading = False
        self._detail_open_for: str | None = None
        self._market_filter_generation = 0
        self._market_matcher = SearchMatcher("")
        self._market_stale_status = ""
        self._plugin_busy = False
        self._market_render_lock = asyncio.Lock()
        self._preview_signature: tuple[str, str, str] | None = None

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
                yield SafeSelect(
                    [(self._service.t("tui.market_all_categories"), "all")],
                    id="market-category",
                    value="all",
                    allow_blank=False,
                )
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
                yield Button(
                    self._service.t("tui.btn_refresh"), id="market-refresh", variant="primary"
                )
        with Horizontal(id="market-body"):
            yield DataTable(id="market-table")
            with Vertical(id="market-detail"):
                yield Static("", id="market-selection")
                with ScrollableContainer(id="market-preview-scroll"):
                    yield Markdown("", id="market-readme")
        with Horizontal(id="market-actions"):
            yield Button(
                self._service.t("tui.market_view_detail"),
                id="market-open-detail",
                variant="primary",
                disabled=True,
            )
            yield Button(
                self._service.t("tui.btn_install"),
                id="market-install",
                variant="success",
                disabled=True,
            )
            yield Button(self._service.t("tui.notifications_toggle"), id="market-toggle", disabled=True)
            yield Button(self._service.t("tui.market_tools"), id="market-tools")
        yield Static("", id="market-status")

    async def on_mount(self) -> None:
        self._sync_market_controls()
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
                self._market_stale_status = f"[$ctp-yellow]{self._service.t('tui.market_stale')}: {escape(', '.join(stale_names))}[/$ctp-yellow]"
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
        table.add_column(self._service.t("plugin.header_name"), key="plugin", width=26)
        table.add_column(self._service.t("plugin.market_status"), key="status")
        table.cursor_type = "row"  # pyright: ignore[reportUnknownMemberType]
        self._columns_done = True

    async def relabel(self) -> None:
        self._columns_done = False
        self._categories = None
        search = self.query_one_optional("#market-search", Input)
        if search is not None:
            search.placeholder = self._service.t("tui.market_search_placeholder")
        _relabel_buttons(
            self,
            {
                "market-refresh": "tui.btn_refresh",
                "market-open-detail": "tui.market_view_detail",
                "market-install": "tui.btn_install",
                "market-tools": "tui.market_tools",
            },
        )
        sort = _typed_select(self, "#market-sort")
        if sort is not None and _select_ready(sort):
            value = sort.value
            with sort.prevent(Select.Changed):
                sort.set_options(
                    [
                        (self._service.t("tui.market_sort_" + key), mode)
                        for key, mode in (
                            ("name", "name"),
                            ("status", "status"),
                            ("category", "category"),
                            ("installed", "installed"),
                            ("enabled", "enabled"),
                            ("not_installed", "not-installed"),
                        )
                    ]
                )
                _safe_set_value(sort, value)
        await self._render_entries()

    def _category_label(self, category: str) -> str:
        return self._service.i18n.component_label(category)

    def _is_installed(self, plugin: MarketPlugin) -> bool:
        installed = self._installed.get(plugin.id)
        if installed is None:
            installed = self._service.market.is_installed(plugin.id, package=plugin.package)
            self._installed[plugin.id] = installed
        return installed

    def _market_status_of(self, plugin: MarketPlugin) -> str:
        if not self._is_installed(plugin):
            return self._service.t("plugin.not_installed_yet")
        config_status = self._service.plugin_status(plugin.id)
        if config_status == "disabled":
            return self._service.t("tui.plugin_status_disabled")
        if config_status == "enabled":
            return self._service.t("tui.plugin_status_enabled")
        if config_status == "error":
            return self._service.t("tui.plugin_status_error")
        return self._service.t("plugin.installed")

    def _market_status_style(self, plugin: MarketPlugin) -> str:
        if not self._is_installed(plugin):
            return "$ctp-subtext0"
        return {
            "enabled": "$ctp-green",
            "disabled": "$ctp-yellow",
            "error": "$ctp-red",
        }.get(self._service.plugin_status(plugin.id), "$ctp-blue")

    def _sync_market_controls(self) -> None:
        plugin = self._selected
        installed = plugin is not None and self._is_installed(plugin)
        for ident in ("market-open-detail", "market-install", "market-toggle", "market-tools"):
            button = self.query_one_optional("#" + ident, Button)
            if button is not None:
                button.disabled = self._plugin_busy or (ident != "market-tools" and plugin is None)
                if ident == "market-install":
                    button.disabled = button.disabled or installed
                elif ident == "market-toggle":
                    button.disabled = button.disabled or not installed
                    enabled = (
                        plugin is not None and self._service.plugin_status(plugin.id) == "enabled"
                    )
                    button.label = (
                        self._service.t(
                            "tui.plugin_status_enabled" if enabled else "tui.plugin_status_disabled"
                        )
                        if installed
                        else self._service.t("tui.notifications_toggle")
                    )
                    button.variant = "success" if enabled else "error" if installed else "default"
        refresh = self.query_one_optional("#market-refresh", Button)
        if refresh is not None:
            refresh.disabled = self._loading or self._plugin_busy

    async def refresh_market(self) -> None:
        """Fetch the marketplace in a worker; the UI stays interactive.

        Only the network fetch is deferred — filtering and rendering run from
        the cached entries, so typing in the search box never re-fetches.
        """
        if self._loading:
            return  # a fetch is already in flight; its result will render
        self._loading = True
        self._sync_market_controls()
        self._set_status(self._service.t("tui.loading"))
        self.run_worker(self._fetch_entries(), exclusive=True, group="market-fetch")

    async def _fetch_entries(self) -> None:
        market = self._service.market
        try:
            report = await self._service.market_fetch_report()
            fresh = list(report.entries)
            failures = [
                (status.repository, self._service.t(status.error_code))
                for status in report.repositories
                if not status.ok
            ]
        except AttributeError:
            try:
                fresh = await asyncio.to_thread(market.list_plugins)
                failures = []
            except Exception as exc:
                self._loading = False
                self._sync_market_controls()
                self._set_status(escape(error_message(self._service, exc)))
                return
        except Exception as exc:
            self._loading = False
            self._sync_market_controls()
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
                local.readme = await asyncio.to_thread(plugin_doc_readme, info)
                local_entries.append((local_repo, local))
                seen.add(info.plugin_id)
        self._entries = [*fresh, *local_entries]
        self._installed = {}
        self._loading = False
        self._sync_market_controls()
        app.set_plugin_entries(self._entries)
        await self._service.market_cache_save(self._entries)
        if failures:
            detail = "; ".join(f"{repo.name}: {reason}" for repo, reason in failures)
            self._market_stale_status = (
                f"[$ctp-yellow]{self._service.t('tui.market_stale')}: {escape(detail)}[/$ctp-yellow]"
            )
        else:
            self._market_stale_status = ""
        await self._render_entries()

    async def _render_entries(self) -> None:
        """Render the cached entries through the current search/category."""
        async with self._market_render_lock:
            await self._render_entries_unlocked()

    async def _render_entries_unlocked(self) -> None:
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
                f"[$ctp-red]{self._service.t('tui.invalid_regex')}: {escape(self._service.t('errors.invalid_value'))}[/$ctp-red]"
            )
            return
        view = TableView.capture(table, self._selected.id if self._selected is not None else None)
        self._market_matcher = matcher
        sort_mode = self._select_value("#market-sort")
        ordered = list(self._entries)
        if sort_mode == "name":
            ordered.sort(key=lambda item: plugin_text(self._service, item[1], "name").lower())
        elif sort_mode == "status":
            ordered.sort(
                key=lambda item: (
                    self._market_status_of(item[1]),
                    plugin_text(self._service, item[1], "name").lower(),
                )
            )
        elif sort_mode == "category":
            ordered.sort(key=lambda item: sorted(item[1].categories)[:1])
        elif sort_mode == "installed":
            ordered.sort(
                key=lambda item: (
                    not self._is_installed(item[1]),
                    plugin_text(self._service, item[1], "name").lower(),
                )
            )
        elif sort_mode == "enabled":
            ordered.sort(
                key=lambda item: (
                    self._market_status_of(item[1]) != self._service.t("tui.plugin_status_enabled"),
                    plugin_text(self._service, item[1], "name").lower(),
                )
            )
        elif sort_mode == "not-installed":
            ordered.sort(
                key=lambda item: (
                    self._is_installed(item[1]),
                    plugin_text(self._service, item[1], "name").lower(),
                )
            )
        rows: list[tuple[str, tuple[Any, ...]]] = []
        visible: dict[str, MarketPlugin] = {}
        for position, (_repo, plugin) in enumerate(ordered, start=1):
            if filter_value and filter_value != "all" and filter_value not in plugin.categories:
                continue
            name = plugin_text(self._service, plugin, "name") or plugin.id
            description = plugin_text(self._service, plugin, "description")
            if matcher.query:
                blob = f"{plugin.id} {plugin.name} {name} {plugin.description} {description}"
                if not matcher.matches(blob):
                    continue
            visible[plugin.id] = plugin
            rows.append(
                (
                    plugin.id,
                    (
                        RichText(name, style="bold $ctp-mauve", overflow="ellipsis", no_wrap=True),
                        RichText(
                            self._market_status_of(plugin), style=self._market_status_style(plugin)
                        ),
                    ),
                )
            )
            if position % 50 == 0:
                await asyncio.sleep(0)
        update_table(table, rows)
        selected_id = view.restore(table)
        self._selected = visible.get(selected_id or "")
        categories = sorted({c for _r, p in self._entries for c in p.categories})
        if categories != getattr(self, "_categories", None):
            select = self.query_one_optional("#market-category", Select)  # pyright: ignore[reportUnknownVariableType]
            if select is not None and _select_ready(select):
                try:
                    with select.prevent(Select.Changed):
                        select.set_options(  # pyright: ignore[reportUnknownMemberType]
                            [
                                (self._service.t("tui.market_all_categories"), "all"),
                                *[(self._category_label(c), c) for c in categories],
                            ]
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
        else:
            self.query_one("#market-selection", Static).update(
                self._service.t("tui.market_no_results")
            )
            self.query_one("#market-readme", Markdown).update("")
            self._preview_signature = None
        self._sync_market_controls()

    def _show_detail(self, plugin: MarketPlugin) -> None:
        self._selected = plugin
        name = plugin_text(self._service, plugin, "name") or plugin.id
        summary = RichText(name, style="bold $ctp-mauve")
        summary.append(f"  v{plugin.version}", style="$ctp-subtext0")
        summary.append("  " + self._market_status_of(plugin), style=self._market_status_style(plugin))
        self.query_one("#market-selection", Static).update(summary)
        readme = plugin_text(self._service, plugin, "readme")
        if readme.startswith("# "):
            readme = readme.partition("\n")[2].lstrip()
        readme = readme or plugin_text(self._service, plugin, "description")
        signature = (plugin.id, self._service.i18n.language, readme)
        if signature != self._preview_signature:
            previous = self._preview_signature
            self._preview_signature = signature
            self.query_one("#market-readme", Markdown).update(readme)
            if previous is None or previous[0] != plugin.id:
                self.query_one("#market-preview-scroll", ScrollableContainer).scroll_home(
                    animate=False
                )
        self._sync_market_controls()

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
        self._open_detail(plugin)

    def _open_detail(self, plugin: MarketPlugin) -> None:
        if self._plugin_busy:
            return
        if self._detail_open_for == plugin.id:
            return  # a double-click fires RowSelected twice: one dialog
        self._detail_open_for = plugin.id
        self._show_detail(plugin)
        cast(MailFlowApp, self.app).push_screen(  # pyright: ignore[reportUnknownMemberType]
            MarketDetailScreen(self._service, plugin),
            callback=lambda result: self._detail_closed(plugin.id, result),
        )

    def _detail_closed(self, plugin_id: str, result: Any = None) -> None:
        """Release the double-open guard when the detail dialog closes."""
        if self._detail_open_for == plugin_id:
            self._detail_open_for = None
        if result is not None and self.is_mounted:
            self._installed.pop(plugin_id, None)
            self.run_worker(self._render_entries(), group="market-filter", exit_on_error=False)

    async def on_data_table_row_highlighted(self, event: TableRowEvent) -> None:
        plugin = next((p for _r, p in self._entries if p.id == event.row_key.value), None)
        if plugin is not None and plugin is not self._selected:
            self._show_detail(plugin)

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in ("market-category", "market-sort"):
            await self._render_entries()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.disabled or self._plugin_busy:
            return
        button_id = event.button.id
        if button_id == "market-refresh":
            await self.refresh_market()
            return
        if button_id == "market-tools":
            self._open_tools()
            return
        plugin = self._selected
        if plugin is None:
            return
        if button_id == "market-open-detail":
            self._open_detail(plugin)
            return
        if button_id == "market-toggle":
            button_id = (
                "market-disable"
                if self._service.plugin_status(plugin.id) == "enabled"
                else "market-enable"
            )
        if button_id in {"market-install", "market-enable", "market-disable"}:
            self._start_plugin_action(button_id, plugin)

    def _open_tools(self) -> None:
        plugin = self._selected
        choices = [
            (self._service.t("tui.btn_install_local"), "market-install-local"),
            (self._service.t("tui.btn_repos"), "market-repos"),
            (self._service.t("tui.market_develop"), "market-create"),
            (self._service.t("tui.market_export_bot"), "market-export"),
        ]
        if plugin is not None and self._is_installed(plugin):
            choices.append((self._service.t("tui.btn_uninstall"), "market-uninstall"))

        async def chosen(action: str | None) -> None:
            if action not in {value for _, value in choices} or self._plugin_busy:
                return
            if action == "market-uninstall" and plugin is not None:
                self.app.push_screen(
                    ConfirmModal(
                        self._service,
                        title=self._service.t("tui.btn_uninstall"),
                        body=self._service.t("tui.market_uninstall_confirm", plugin=plugin.id),
                        confirm_label=self._service.t("tui.btn_uninstall"),
                        variant="error",
                        focus_confirm=False,
                    ),
                    callback=lambda confirmed: (
                        self._start_plugin_action("market-uninstall", plugin) if confirmed else None
                    ),
                )
            else:
                self._open_tool(action)

        self.app.push_screen(
            ChoicePicker(self._service, choices, title=self._service.t("tui.market_tools")),
            chosen,
        )

    def _open_tool(self, button_id: str | None) -> None:
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

    def _start_plugin_action(self, button_id: str, plugin: MarketPlugin) -> None:
        if self._plugin_busy:
            return
        self._plugin_busy = True
        self._sync_market_controls()
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
        finally:
            self._plugin_busy = False
            if self.is_mounted:
                self._sync_market_controls()
        self._installed.pop(plugin.id, None)
        await self._render_entries()
        note = "plugin.applies_now" if plugin.categories == ["theme"] else "plugin.restart_note"
        status_node.update(
            self._service.t(message_key, plugin_id=plugin.id)
            + f" ({self._service.t(note)})"
        )


async def pilot_pause_if_possible(app: Any, seconds: float = 0.05) -> None:
    """Yield control so remounted widgets finish composing; a no-op when the
    app is not currently running its test/message pump."""
    await asyncio.sleep(seconds)


class MailFlowApp(App[None]):
    """Eight-tab administration UI."""

    CSS_PATH = "app.tcss"
    CSS = "Tooltip { visibility: hidden; }"
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
        theme_welcome: bool = False,
        terminal_background: str | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._theme_welcome = theme_welcome
        self._theme_preview: str | None = None
        self._theme_preference = load_theme_preferences() if theme_welcome else {}
        self._terminal_background = terminal_background
        if self._terminal_background is None and (
            theme_welcome or self._service.config.general.theme == TERMINAL_THEME
        ):
            self._terminal_background = terminal_background_mode()
        self._theme_signature: tuple[str, str, str] | None = None
        self._missing_theme_notice = ""
        self._apply_color_scheme()
        self._log_queue = log_queue
        self._remote = remote
        self._splash = splash
        self._session = session
        if session is not None:
            session.bind(self)
        self._log_timer: Any = None
        self._refresh_lock = asyncio.Lock()
        self._dirty_tabs: set[str] = set()
        self._reload_requested = False
        self._reload_scheduled = False
        # shared plugin-entry cache: the single source of truth for the
        # detail dialog. MarketPane writes its fetched entries here, and
        # the Runtime tab reads from here, so both tabs show the exact
        # same detail (same readme, same translations).
        self._plugin_entries: dict[str, MarketPlugin] = {}

        self._reset_shortcuts()

    def _apply_color_scheme(self) -> None:
        configured = self._service.config.general.theme
        schemes = theme_catalog(self._service)
        preference = self._theme_preference.get("theme")
        selected = (
            preference
            if configured == DEFAULT_THEME and preference in schemes
            else configured
            if configured in schemes
            else DEFAULT_THEME
        )
        if self._theme_preview is not None and self._theme_preview in schemes:
            selected = self._theme_preview
        if selected == TERMINAL_THEME or (
            self._theme_preview is None
            and self._theme_welcome
            and not self._theme_preference.get("theme_onboarding_complete")
            and configured == DEFAULT_THEME
            and not preference
        ):
            selected = "catppuccin-latte" if self._terminal_background == "light" else DEFAULT_THEME
        scheme = schemes[selected]
        signature = (configured, selected, scheme.model_dump_json())
        if signature != self._theme_signature:
            apply_color_scheme(
                self, selected, scheme, replace_rich=self._theme_signature is not None
            )
            self._theme_signature = signature
            if self.is_running:
                self.call_after_refresh(self._refresh_color_content)
        if (
            self.is_running
            and configured not in schemes
            and self._missing_theme_notice != configured
        ):
            self._missing_theme_notice = configured
            self.notify(
                self._service.t("tui.theme_unavailable", theme=configured),
                severity="warning",
                timeout=12,
            )
        elif configured in schemes:
            self._missing_theme_notice = ""

    def preview_color_scheme(self, theme: str | None) -> None:
        """Apply a temporary palette without saving or reloading the service."""
        if theme != self._theme_preview:
            self._theme_preview = theme
            self._apply_color_scheme()

    def _refresh_color_content(self) -> None:
        # Textual restyles widgets when the theme changes. Only render caches
        # that are explicitly theme-aware need to be invalidated here.
        for screen in self.screen_stack:
            for table in screen.query(DataTable):
                table.notify_style_update()
        for pane in self.query(LogsPane):
            pane.run_worker(
                cast(Any, pane._render_logs),
                exclusive=True,
                group="logs-render",
                exit_on_error=False,
            )

    async def _refresh_color_scheme(self) -> None:
        if self._remote:
            try:
                await self._service.refresh_snapshot()
            except Exception as exc:
                logger.exception("could not refresh remote color scheme")
                self.notify(
                    self._service.t("tui.data_load_failed", error=error_detail(self._service, exc)),
                    severity="warning",
                )
                return
        self._apply_color_scheme()

    def _reset_shortcuts(self, *, refresh: bool = False) -> None:
        """Bind visible footer text from the active language pack."""
        self._bindings = BindingsMap()
        self._bindings.bind("f1", "shortcut_help", self._service.t("tui.help_shortcuts"))
        if self._session is not None:
            self._bindings.bind(
                "f8",
                "detach_session",
                self._service.t("sessions.detach"),
                priority=True,
            )
        self._bindings.bind(
            "ctrl+q",
            "quit",
            self._service.t("sessions.quit" if self._session is not None else "tui.btn_quit"),
        )
        for key, tab_id in (
            ("ctrl+1", "tab-mail"),
            ("ctrl+2", "tab-actions"),
            ("ctrl+3", "tab-mailboxes"),
            ("ctrl+4", "tab-runtime"),
            ("ctrl+5", "tab-market"),
            ("ctrl+6", "tab-notifications"),
            ("ctrl+7", "tab-settings"),
            ("ctrl+8", "tab-logs"),
            ("ctrl+9", "tab-console"),
        ):
            if self._remote and tab_id in {
                "tab-mailboxes",
                "tab-market",
            }:
                continue
            self._bindings.bind(key, f"goto_tab('{tab_id}')", show=False)
        if refresh:
            self.refresh_bindings()

    def action_shortcut_help(self) -> None:
        if not isinstance(self.screen, ShortcutHelp):
            self.push_screen(ShortcutHelp(self._service.t))  # pyright: ignore[reportUnknownMemberType]

    def action_change_theme(self) -> None:
        # Use the persisted setting rather than Textual's temporary preview.
        self.action_goto_tab("tab-settings")

    def action_detach_session(self) -> None:
        if self._session is not None:
            self._session.detach()

    def set_terminal_background(self, mode: str | None) -> None:
        if mode not in {"dark", "light"} or mode == self._terminal_background:
            return
        self._terminal_background = mode
        self._apply_color_scheme()

    def _show_splash(self) -> None:
        if self._splash:
            from mailflow_tui.splash import SplashScreen

            self.push_screen(SplashScreen(self._service.t, self._version_string()))

    def _theme_welcome_selected(self, theme: str | None) -> None:
        self.run_worker(
            self._complete_theme_welcome(theme),
            exclusive=True,
            group="theme-welcome",
            exit_on_error=False,
        )

    async def _complete_theme_welcome(self, theme: str | None) -> None:
        saved = save_theme_preferences(theme, completed=True)
        self._theme_preference = load_theme_preferences()
        if theme is not None:
            self._theme_preference["theme"] = theme
            if not self._remote and getattr(self._service, "config_path", None) is not None:
                try:
                    await self._service.set_config_value("general.theme", theme)
                except Exception:
                    logger.warning("could not save initial theme to config", exc_info=True)
                    self.notify(
                        self._service.t("tui.theme_save_failed"),
                        severity="warning",
                    )
        self._theme_preview = None
        self._apply_color_scheme()
        if not saved:
            self.notify(self._service.t("tui.theme_preference_failed"), severity="warning")
        self._show_splash()

    def set_plugin_entries(self, entries: list[tuple[Repository, MarketPlugin]]) -> None:
        """Record the market entries (called by MarketPane after a fetch)."""
        for _repo, plugin in entries:
            self._plugin_entries[plugin.id] = plugin
        if self.is_running:
            for pane in self.query(RuntimePane):
                if pane.is_mounted:
                    pane.run_worker(
                        pane.refresh_runtime(),
                        exclusive=True,
                        group="runtime-refresh",
                        exit_on_error=False,
                    )

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
        pane = self.query_one(MailPane)
        if not pane.set_smart_scope(scope, name):
            self.notify(self._service.t("assistant.busy"))
            return
        self.query_one(TabbedContent).active = "tab-mail"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="tab-mail"):
            with TabPane(self._service.t("tui.tab_mail"), id="tab-mail"):
                yield MailPane(self._service)
            with TabPane(self._service.t("tui.tab_actions"), id="tab-actions"):
                yield ActionsPane(self._service)
            if not self._remote:
                # mailbox forms and history browsing need the local service
                with TabPane(self._service.t("tui.tab_connections"), id="tab-mailboxes"):
                    yield ConnectionsPane(self._service)
            with TabPane(self._service.t("tui.tab_runtime"), id="tab-runtime"):
                yield RuntimePane(self._service)
            if not self._remote:
                # marketplace installs run uv against the local environment
                with TabPane(self._service.t("tui.tab_market"), id="tab-market"):
                    yield MarketPane(self._service)
            with TabPane(self._service.t("tui.tab_notifications"), id="tab-notifications"):
                yield NotificationsPane(self._service)
            with TabPane(self._service.t("tui.tab_console"), id="tab-console"):
                yield ConsolePane(self._service)
            with TabPane(self._service.t("tui.tab_settings"), id="tab-settings"):
                yield SettingsPane(self._service)
            with TabPane(self._service.t("tui.tab_logs"), id="tab-logs"):
                yield LogsPane(self._service, self._log_queue)
        yield Footer()

    async def on_mount(self) -> None:
        self.title = self._service.t("tui.title")
        self._apply_color_scheme()
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
        self._subscriptions = [self._service.on("*", self._on_service_event)]
        if self._theme_welcome and not self._theme_preference.get("theme_onboarding_complete"):
            configured_theme = self._service.config.general.theme
            if configured_theme != DEFAULT_THEME:
                save_theme_preferences(configured_theme, completed=True)
                self._theme_preference = load_theme_preferences()
                self._show_splash()
            else:
                self.push_screen(
                    ThemeWelcomeScreen(self._service.t, on_preview=self.preview_color_scheme),
                    self._theme_welcome_selected,
                )
        else:
            self._show_splash()
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
        self._reload_requested = False
        for unsubscribe in getattr(self, "_subscriptions", []):
            if callable(unsubscribe):
                unsubscribe()

    async def _on_service_event(self, event: str, **payload: Any) -> None:
        if event == "config.changed" and "general.theme" in {
            *payload.get("keys", ()),
            payload.get("key"),
        }:
            theme = self._service.config.general.theme
            save_theme_preferences(theme, completed=True)
            self._theme_preference = load_theme_preferences()
        if event.startswith(("config.", "plugin.", "backup.restored")):
            self.run_worker(
                self._refresh_color_scheme(),
                exclusive=True,
                group="theme-refresh",
                exit_on_error=False,
            )
        if event == "language.changed":
            await self._on_language_changed(event, **payload)
        elif event == "remote.connection.changed":
            self._sync_connection_status()
            if payload.get("state") == "connected":
                await self._refresh_color_scheme()
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
            if event.startswith(("config.", "plugin.", "backup.restored")):
                affected = {
                    "tab-mail",
                    "tab-actions",
                    "tab-runtime",
                    "tab-mailboxes",
                    "tab-notifications",
                    "tab-settings",
                }
            elif event.startswith("gateway."):
                affected = {"tab-runtime", "tab-notifications"}
            elif event.startswith("action."):
                affected = {"tab-mail", "tab-actions"}
            else:
                affected = {"tab-mail", "tab-actions", "tab-runtime", "tab-mailboxes"}
            self._schedule_reload(affected)

    def _pane_factories(self) -> dict[str, tuple[Callable[[Any], Any], str]]:
        return {
            "tab-mail": (MailPane, "tui.tab_mail"),
            "tab-mailboxes": (ConnectionsPane, "tui.tab_connections"),
            "tab-actions": (ActionsPane, "tui.tab_actions"),
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
        keys["tab-console"] = "tui.tab_console"
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
                    RuntimePane,
                    MarketPane,
                    ConnectionsPane,
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
        for pane in self.query(ConsolePane):
            pane.relabel()
        for screen in self.screen_stack:
            if isinstance(screen, (MailHistoryScreen, MarketDetailScreen)):
                await screen.relabel()
        for table in self.query(SelectionTable):
            table.relabel()
        for screen in self.screen_stack:
            if isinstance(screen, SmartActionReviewModal) and screen.is_mounted:
                await screen.relabel()
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

    def _schedule_reload(self, affected: set[str] | None = None) -> None:
        if not self.is_running:
            return
        # a bulk re-analysis emits one event per mail: coalesce the burst
        # into a single refresh instead of queueing a full reload per mail
        self._dirty_tabs.update(
            affected if affected is not None else {"tab-mail", "tab-actions", "tab-runtime"}
        )
        self._reload_requested = True
        if getattr(self, "_reload_scheduled", False):
            return
        self._reload_scheduled = True

        self.run_worker(cast(Any, self._reload_guarded))

    async def _reload_guarded(self) -> None:
        try:
            while self._reload_requested:
                self._reload_requested = False
                await asyncio.sleep(0.15)
                async with self._refresh_lock:
                    await self._reload_all()
        finally:
            self._reload_scheduled = False

    async def _reload_all(self) -> None:
        tabs = self.query_one_optional(TabbedContent)
        if tabs is None or not self.is_running:
            return
        active = tabs.active
        if active not in self._dirty_tabs:
            return
        self._dirty_tabs.discard(active)
        # Hidden panes catch up on activation. Receiving a mail must not rebuild
        # tables and forms behind the page the user is currently editing.
        if self._remote and active in {"tab-mail", "tab-actions"}:
            try:
                await self._service.refresh_snapshot()
            except Exception as exc:
                self._dirty_tabs.add(active)
                status = self.query_one_optional(
                    "#mail-operation-status" if active == "tab-mail" else "#actions-hint", Static
                )
                if status is not None:
                    status.update(_load_error(self._service, exc))
                return
        for pane_type in (
            MailPane,
            ActionsPane,
            RuntimePane,
            ConnectionsPane,
            NotificationsPane,
            SettingsPane,
        ):
            panes = self.query(f"#{active} {pane_type.__name__}")
            if not panes:
                continue
            pane = panes.first()
            if isinstance(pane, MailPane):
                await pane.refresh_mail()
            elif isinstance(pane, ActionsPane):
                await pane.refresh_actions()
            elif isinstance(pane, RuntimePane):
                await pane.refresh_runtime()
            elif isinstance(pane, NotificationsPane):
                pane.refresh_data()
            elif isinstance(pane, (ConnectionsPane, SettingsPane)):
                await pane.reload()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        active = event.tabbed_content.active
        if active in self._dirty_tabs:
            self._schedule_reload(set())
        if active == "tab-notifications":
            for pane in self.query(NotificationsPane):
                pane.check_if_visible()
        elif active == "tab-console":
            for pane in self.query(ConsolePane):
                pane.activate()

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

    def open_action(self, item_id: str) -> None:
        self.query_one(TabbedContent).active = "tab-actions"
        pane = self.query_one(ActionsPane)
        pane.run_worker(
            pane.select_action(item_id),
            group="action-navigation",
            exclusive=True,
            exit_on_error=False,
        )
