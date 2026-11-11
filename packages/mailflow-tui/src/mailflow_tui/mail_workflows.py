"""Mail operations that share the same local and remote service methods."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast
from zoneinfo import ZoneInfo

from mailflow.domain import ActionItem, MailRecord, ReplyDraft, TrashRecord, looks_binary
from mailflow.errors import error_text
from mailflow.mail_body import mail_body_text
from mailflow.mail_query import MailQuery
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Select, Static

from mailflow_tui.confirm import ConfirmModal
from mailflow_tui.view_state import TableView
from mailflow_tui.widgets import Input, SafeSelect, SelectionTable, TextArea


def _save_attachment(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class WorkflowScreen(ModalScreen[Any]):
    BINDINGS: ClassVar[list[Any]] = []
    DEFAULT_CSS = """
    WorkflowScreen { align: center middle; }
    WorkflowScreen > Vertical { width: 85%; height: 85%; background: $surface; border: round $accent; padding: 1 2; }
    WorkflowScreen ScrollableContainer { height: 1fr; }
    WorkflowScreen DataTable { height: 1fr; }
    WorkflowScreen TextArea { height: 1fr; }
    WorkflowScreen Horizontal { height: auto; }
    WorkflowScreen Button { margin: 0 1 0 0; }
    """

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self.service = service
        self._bindings.bind("escape", "close", self.t("tui.btn_back"))

    def t(self, key: str, **params: Any) -> str:
        return self.service.t(key, **params)

    def action_close(self) -> None:
        self.dismiss(None)

    async def ask(self, screen: ModalScreen[Any]) -> Any:
        app = cast("Any", self).app
        return await app.push_screen_wait(screen)

    def confirmation(self, text: str) -> ConfirmModal:
        return ConfirmModal(
            self.service,
            title=self.t("ux.confirm"),
            body=text,
            confirm_label=self.t("tui.btn_apply"),
            focus_confirm=False,
        )

    def status(self, text: str) -> None:
        self.query_one("#workflow-status", Static).update(text)


class TrashChoiceScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService, preview: str) -> None:
        super().__init__(service)
        self.preview = preview

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("ux.local_trash"))
            with ScrollableContainer():
                yield Static(self.preview, markup=False)
                yield Checkbox(self.t("ux.delete_linked_tasks"), id="trash-linked-actions")
            with Horizontal():
                yield Button(self.t("tui.btn_trash"), id="trash-confirm", variant="error")
                yield Button(self.t("tui.btn_cancel"), id="trash-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "trash-confirm":
            self.dismiss(
                {"delete_actions": self.query_one("#trash-linked-actions", Checkbox).value}
            )
        else:
            self.dismiss(None)


class MailMenu(WorkflowScreen):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("ux.mail_tools"))
            yield Static(self.t("ux.local_trash"))
            yield SafeSelect(
                [
                    (self.t("ux." + key), key)
                    for key in (
                        "undo_delete",
                        "filter",
                        "load_more",
                        "read",
                        "unread",
                        "star",
                        "unstar",
                        "archive",
                        "unarchive",
                        "full_mail",
                        "attachments",
                        "task_from_mail",
                        "trash",
                        "drafts",
                        "cleanup_preview",
                    )
                ],
                id="workflow-choice",
                allow_blank=False,
            )
            with Horizontal():
                yield Button(self.t("tui.btn_apply"), id="workflow-apply", variant="primary")
                yield Button(self.t("tui.btn_back"), id="workflow-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(
            str(cast(Select[str], self.query_one("#workflow-choice", Select)).value)
            if event.button.id == "workflow-apply"
            else None
        )


class MailFilter(WorkflowScreen):
    def __init__(self, service: MailFlowService, query: MailQuery) -> None:
        super().__init__(service)
        self.mail_query = query

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("ux.filter"))
            with ScrollableContainer():
                yield Static(self.t("ux.account"))
                yield SafeSelect(
                    [(self.t("ux.all_accounts"), "")]
                    + [
                        (a.email or a.account_id, a.account_id)
                        for a in self.service.snapshot().accounts
                    ],
                    value=self.mail_query.account_id,
                    id="filter-account",
                    allow_blank=False,
                )
                for field, value in (
                    ("sender", self.mail_query.sender),
                    (
                        "after",
                        self.mail_query.after.strftime("%Y-%m-%d") if self.mail_query.after else "",
                    ),
                    (
                        "before",
                        (self.mail_query.before - timedelta(days=1)).strftime("%Y-%m-%d")
                        if self.mail_query.before
                        else "",
                    ),
                ):
                    yield Static(self.t("ux." + field))
                    yield Input(value=value, id="filter-" + field)
                yield SafeSelect(
                    [
                        (self.t("ux.view_" + key), key)
                        for key in (
                            "inbox",
                            "all",
                            "unread",
                            "starred",
                            "archived",
                            "pending",
                            "failed",
                        )
                    ],
                    value=self.mail_query.state,
                    id="filter-state",
                    allow_blank=False,
                )
                yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(self.t("tui.btn_apply"), id="filter-apply", variant="primary")
                yield Button(self.t("ux.reset_filter"), id="filter-reset")
                yield Button(self.t("tui.btn_cancel"), id="filter-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "filter-reset":
            self.dismiss(MailQuery())
        elif event.button.id == "filter-cancel":
            self.dismiss(None)
        elif event.button.id == "filter-apply":
            try:
                zone = ZoneInfo(self.service.config.general.timezone)
                dates: dict[str, datetime | None] = {}
                for field in ("after", "before"):
                    raw = self.query_one("#filter-" + field, Input).value.strip()
                    dates[field] = (
                        datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=zone) if raw else None
                    )
                if dates["before"] is not None:
                    dates["before"] += timedelta(days=1)
                if dates["after"] and dates["before"] and dates["after"] >= dates["before"]:
                    raise ValueError
                self.dismiss(
                    MailQuery(
                        account_id=str(
                            cast(Select[str], self.query_one("#filter-account", Select)).value
                        ),
                        sender=self.query_one("#filter-sender", Input).value.strip(),
                        state=str(cast(Select[str], self.query_one("#filter-state", Select)).value),
                        after=dates["after"],
                        before=dates["before"],
                    )
                )
            except ValueError:
                self.status(self.t("ux.invalid_dates"))


class MailReader(WorkflowScreen):
    def __init__(self, service: MailFlowService, record: MailRecord) -> None:
        super().__init__(service)
        self.record = record
        self.body = mail_body_text(record.mail)
        if not self.body and looks_binary(record.mail.body_text):
            self.body = self.t("tui.detail_binary_body")
        if record.mail.image_text.strip():
            self.body += f"\n\n{self.t('mail.field_image_text')}:\n{record.mail.image_text}"
        self.body_offset = 0

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"{self.record.mail.subject}\n{self.record.mail.sender.display} · {self.record.mail.account_id}",
                markup=False,
            )
            yield TextArea(self.body[:20000], read_only=True, id="reader-body")
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(self.t("ux.previous"), id="reader-prev")
                yield Button(self.t("ux.next"), id="reader-next")
                yield Button(self.t("tui.btn_back"), id="reader-close")

    def on_mount(self) -> None:
        self._render_body()

    def _render_body(self) -> None:
        self.query_one("#reader-body", TextArea).load_text(
            self.body[self.body_offset : self.body_offset + 20000]
        )
        self.status(
            self.t(
                "ux.body_page",
                start=self.body_offset + 1 if self.body else 0,
                end=min(self.body_offset + 20000, len(self.body)),
                total=len(self.body),
            )
        )
        self.query_one("#reader-prev", Button).disabled = self.body_offset == 0
        self.query_one("#reader-next", Button).disabled = self.body_offset + 20000 >= len(self.body)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reader-close":
            self.dismiss(None)
        else:
            self.body_offset = max(
                0, self.body_offset + (20000 if event.button.id == "reader-next" else -20000)
            )
            self._render_body()


class AttachmentsScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService, record: MailRecord) -> None:
        super().__init__(service)
        self.record = record
        self.saved: Path | None = None
        self.busy = False
        self._opener: str | None = None
        if sys.platform == "win32":
            self._opener = "startfile"
        elif sys.platform == "darwin":
            self._opener = shutil.which("open")
        elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            self._opener = shutil.which("xdg-open")

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("ux.attachments"))
            if self.record.mail.attachments:
                yield SafeSelect(
                    [
                        (f"{a.filename} ({a.size} B)", str(i))
                        for i, a in enumerate(self.record.mail.attachments)
                    ],
                    id="attachment-choice",
                    allow_blank=False,
                )
                yield Static(self.t("ux.save_path"))
                yield Input(id="attachment-path")
                yield Static("", id="workflow-status", markup=False)
                with Horizontal():
                    yield Button(self.t("ux.download"), id="attachment-download", variant="primary")
                    yield Button(
                        self.t("ux.open_saved"),
                        id="attachment-open",
                        disabled=True,
                        tooltip=self.t("ux.attachment_open_unavailable")
                        if self._opener is None
                        else None,
                    )
                    yield Button(self.t("tui.btn_back"), id="attachment-close")
            else:
                yield Static(self.t("ux.no_attachments"))
                yield Button(self.t("tui.btn_back"), id="attachment-close")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "attachment-choice" and event.value is not Select.NULL:
            name = re.sub(
                r'[<>:"/\\|?*\x00-\x1f]',
                "_",
                self.record.mail.attachments[int(str(event.value))].filename,
            )
            self.query_one("#attachment-path", Input).value = str(
                Path.home() / "Downloads" / (name or "attachment")
            )
            self.saved = None
            self._sync_controls()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "attachment-path":
            self.saved = None
            self._sync_controls()

    def _sync_controls(self) -> None:
        self.query_one("#attachment-download", Button).disabled = self.busy
        self.query_one("#attachment-choice", Select).disabled = self.busy
        self.query_one("#attachment-path", Input).disabled = self.busy
        self.query_one("#attachment-open", Button).disabled = (
            self.busy or self.saved is None or self._opener is None
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "attachment-close":
            self.dismiss(None)
        elif self.busy or event.button.disabled:
            return
        elif event.button.id == "attachment-download":
            self.busy = True
            self.saved = None
            self._sync_controls()
            self.run_worker(self._download(), exclusive=True, exit_on_error=False)
        elif event.button.id == "attachment-open":
            self.run_worker(self._open(), exclusive=True, exit_on_error=False)

    async def _open(self) -> None:
        try:
            if self.saved is not None and self._opener:
                if sys.platform == "win32":
                    await asyncio.to_thread(os.startfile, str(self.saved))
                else:
                    process = await asyncio.create_subprocess_exec(
                        self._opener,
                        str(self.saved),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    if await process.wait():
                        raise OSError("file opener failed")
        except Exception as exc:
            self.status(error_text(exc, self.service.t))

    async def _download(self) -> None:
        self.status(self.t("ux.downloading"))
        try:
            target_raw = self.query_one("#attachment-path", Input).value.strip()
            index = int(str(cast(Select[str], self.query_one("#attachment-choice", Select)).value))
            if not target_raw:
                raise ValueError
            target = await asyncio.to_thread(lambda: Path(target_raw).expanduser().resolve())
            if await asyncio.to_thread(target.exists) and not await self.ask(
                self.confirmation(self.t("ux.overwrite", path=str(target)))
            ):
                self.status(self.t("ux.cancelled"))
                return
            data = await self.service.fetch_attachment(self.record.record_id, index)
            await asyncio.to_thread(_save_attachment, target, data)
            self.saved = target
            self.status(self.t("ux.downloaded", path=str(target)))
        except Exception as exc:
            self.status(error_text(exc, self.service.t))
        finally:
            self.busy = False
            if self.is_mounted:
                self._sync_controls()


class TrashScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__(service)
        self.rows: list[TrashRecord] = []
        self.busy = False

    def action_close(self) -> None:
        if not self.busy:
            super().action_close()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("ux.local_trash"))
            yield Input(placeholder=self.t("tui.mail_search_placeholder"), id="trash-search")
            yield SelectionTable(t=self.t, id="trash-table")
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(
                    self.t("ux.restore"), id="trash-restore", variant="primary", disabled=True
                )
                yield Button(
                    self.t("ux.permanent_delete"), id="trash-purge", variant="error", disabled=True
                )
                yield Button(self.t("tui.btn_refresh"), id="trash-refresh")
                yield Button(self.t("tui.btn_back"), id="trash-close")

    async def on_mount(self) -> None:
        table = self.query_one("#trash-table", SelectionTable)
        for column in ("selected", "subject", "account", "deleted", "expires"):
            table.add_column(self.t("ux." + column), key=column)
        table.cursor_type = "row"
        await self.refresh_rows()

    async def refresh_rows(self) -> None:
        try:
            self.rows = await self.service.list_trash()
            self.render_rows()
        except Exception as exc:
            self.status(error_text(exc, self.service.t))

    def render_rows(self) -> None:
        table = self.query_one("#trash-table", SelectionTable)
        view = TableView.capture(table)
        table.clear()
        table.retain_checks({r.record_id for r in self.rows})
        query = self.query_one("#trash-search", Input).value.casefold().strip()
        from mailflow.ocr import searchable_record_text

        zone = ZoneInfo(self.service.config.general.timezone)
        for row in self.rows:
            if query and query not in searchable_record_text(row.to_mail_record()).casefold():
                continue
            table.add_row(
                table.check_label(row.record_id),
                row.mail.subject,
                row.mail.account_id,
                row.deleted_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
                row.expires_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
                key=row.record_id,
            )
        view.restore(table)
        self._sync_controls()

    def _sync_controls(self) -> None:
        table = self.query_one("#trash-table", SelectionTable)
        available = bool(table.checked_keys or table.row_count)
        for ident in ("trash-restore", "trash-purge", "trash-refresh", "trash-close"):
            self.query_one(f"#{ident}", Button).disabled = self.busy or (
                ident in {"trash-restore", "trash-purge"} and not available
            )
        table.disabled = self.busy
        self.query_one("#trash-search", Input).disabled = self.busy

    def on_selection_table_selection_changed(self, event: SelectionTable.SelectionChanged) -> None:
        self._sync_controls()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "trash-search":
            self.render_rows()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.disabled:
            return
        if event.button.id == "trash-close" and not self.busy:
            self.dismiss(True)
        elif not self.busy:
            self.busy = True
            self._sync_controls()
            self.run_worker(
                self._run_operation(event.button.id or ""), exclusive=True, exit_on_error=False
            )

    async def _run_operation(self, ident: str) -> None:
        try:
            await self._operate(ident)
        finally:
            self.busy = False
            if self.is_mounted:
                self._sync_controls()

    async def _operate(self, ident: str) -> None:
        if ident == "trash-refresh":
            await self.refresh_rows()
            return
        table = self.query_one("#trash-table", SelectionTable)
        ids = sorted(table.checked_keys)
        if not ids and table.row_count:
            ids = [str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)]
        if not ids:
            return
        try:
            operation = "restore" if ident == "trash-restore" else "purge"
            if operation == "purge" and not await self.ask(
                self.confirmation(self.t("ux.permanent_confirm", count=len(ids)))
            ):
                return
            result = await self.service.batch_mail(ids, operation=operation)
            table.set_checks(set(result.failed))
            self.status(
                self.t(
                    "ux.batch_result",
                    success=len(result.succeeded),
                    failed=len(result.failed),
                    skipped=len(result.skipped),
                )
                + ("\n" + "\n".join(result.failed.values()) if result.failed else "")
            )
            await self.refresh_rows()
        except Exception as exc:
            self.status(error_text(exc, self.service.t))


class DraftsScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__(service)
        self.rows: dict[str, ReplyDraft] = {}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("ux.drafts"))
            yield Input(placeholder=self.t("tui.mail_search_placeholder"), id="draft-search")
            yield SafeSelect(
                [
                    (self.t("ux.all_drafts"), ""),
                    (self.t("ux.unsent"), "unsent"),
                    (self.t("ux.sent"), "sent"),
                ],
                id="draft-filter",
                allow_blank=False,
            )
            yield DataTable(id="draft-table")
            yield TextArea(read_only=True, id="draft-body")
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(
                    self.t("ux.continue_reply"),
                    id="draft-continue",
                    variant="primary",
                    disabled=True,
                )
                yield Button(self.t("tui.btn_back"), id="draft-close")

    async def on_mount(self) -> None:
        table = cast(DataTable[Any], self.query_one("#draft-table", DataTable))
        for key in ("subject", "account", "state", "updated"):
            table.add_column(self.t("ux." + key))
        table.cursor_type = "row"
        await self.refresh_rows()

    async def refresh_rows(self) -> None:
        try:
            self.rows = {r.draft_id: r for r in await self.service.list_drafts()}
            self.render_rows()
        except Exception as exc:
            self.status(error_text(exc, self.service.t))

    def render_rows(self) -> None:
        table = cast(DataTable[Any], self.query_one("#draft-table", DataTable))
        view = TableView.capture(table)
        table.clear()
        mode = str(cast(Select[str], self.query_one("#draft-filter", Select)).value)
        query = self.query_one("#draft-search", Input).value.strip().casefold()
        zone = ZoneInfo(self.service.config.general.timezone)
        for row in self.rows.values():
            if (
                query
                and query
                not in f"{row.subject} {row.body} {row.to.display} {row.account_id}".casefold()
            ):
                continue
            if (mode == "sent" and row.state.value != "sent") or (
                mode == "unsent" and row.state.value in {"sent", "cancelled"}
            ):
                continue
            table.add_row(
                row.subject,
                row.account_id,
                self.t("reply.state_" + row.state.value),
                row.updated_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
                key=row.draft_id,
            )
        view.restore(table)
        self._show_selected()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "draft-search":
            self.render_rows()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "draft-filter" and self.is_mounted:
            self.render_rows()

    def on_data_table_row_highlighted(self, event: Any) -> None:
        if event.data_table.id == "draft-table":
            self._show_selected()

    def _show_selected(self) -> None:
        table = cast(DataTable[Any], self.query_one("#draft-table", DataTable))
        row = (
            self.rows.get(str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value))
            if table.row_count
            else None
        )
        self.query_one("#draft-body", TextArea).load_text(
            f"{row.to.display}\n\n{row.body}" if row else ""
        )
        self.query_one("#draft-continue", Button).disabled = row is None or row.state.value in {
            "sent",
            "cancelled",
            "sending",
        }

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "draft-close":
            self.dismiss(None)
        elif event.button.id == "draft-continue":
            table = cast(DataTable[Any], self.query_one("#draft-table", DataTable))
            if table.row_count:
                row = self.rows.get(
                    str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
                )
                if row and row.state.value not in {"sent", "cancelled", "sending"}:
                    self.dismiss(row)


class TaskMenu(MailMenu):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("ux.task_operations"))
            yield SafeSelect(
                [
                    (self.t("ux." + key), key)
                    for key in (
                        "complete_tasks",
                        "reopen_tasks",
                        "mute_tasks",
                        "enable_tasks",
                        "snooze_hour",
                        "snooze_day",
                    )
                ],
                id="workflow-choice",
                allow_blank=False,
            )
            with Horizontal():
                yield Button(self.t("tui.btn_apply"), id="workflow-apply", variant="primary")
                yield Button(self.t("tui.btn_back"), id="workflow-close")


class SmartScopeScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService, mode: str) -> None:
        super().__init__(service)
        self.mode = mode

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("ux.smart_scope"))
            yield SafeSelect(
                [
                    (self.t("ux.scope_" + key), key)
                    for key in ("auto", "all", "account", "results", "selected")
                ],
                value=self.mode,
                id="scope-choice",
                allow_blank=False,
            )
            yield Button(self.t("tui.btn_apply"), id="scope-apply", variant="primary")
            yield Button(self.t("tui.btn_cancel"), id="scope-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(
            str(cast(Select[str], self.query_one("#scope-choice", Select)).value)
            if event.button.id == "scope-apply"
            else None
        )


class QueryTasksScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService, items: list[ActionItem]) -> None:
        super().__init__(service)
        self.items = {item.item_id: item for item in items}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("ux.query_tasks"))
            yield DataTable(id="query-tasks-table")
            yield Button(
                self.t("tui.btn_edit"),
                id="query-task-open",
                variant="primary",
                disabled=not self.items,
            )
            yield Button(self.t("tui.btn_back"), id="query-task-close")

    def on_mount(self) -> None:
        table = cast(DataTable[Any], self.query_one("#query-tasks-table", DataTable))
        table.cursor_type = "row"
        for key in ("subject", "due", "source"):
            table.add_column(self.t("ux." + key))
        for item in self.items.values():
            table.add_row(
                item.summary,
                item.time_range_in(self.service.config.general.timezone) or self.t("ux.no_due"),
                item.source_subject or item.mail_id,
                key=item.item_id,
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        table = cast(DataTable[Any], self.query_one("#query-tasks-table", DataTable))
        item = (
            self.items.get(str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value))
            if table.row_count
            else None
        )
        self.dismiss(item if event.button.id == "query-task-open" else None)


class DeliveriesScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__(service)
        self.rows: list[dict[str, Any]] = []
        self._retrying = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("ux.failed_deliveries"))
            yield SelectionTable(t=self.t, id="delivery-table")
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(self.t("ux.retry_delivery"), id="delivery-retry", disabled=True)
                yield Button(self.t("tui.btn_refresh"), id="delivery-refresh")
                yield Button(self.t("tui.btn_back"), id="delivery-close")

    async def on_mount(self) -> None:
        table = self.query_one("#delivery-table", SelectionTable)
        table.add_column("[ ]", key="selected", width=3)
        for key in ("kind", "channel", "target", "reason", "updated", "source"):
            table.add_column(self.t("ux." + key))
        await self.reload_rows()

    async def reload_rows(self) -> None:
        try:
            self.rows = await self.service.failed_deliveries()
            table = self.query_one("#delivery-table", SelectionTable)
            table.retain_checks({str(row["delivery_id"]) for row in self.rows})
            table.clear()
            for row in self.rows:
                table.add_row(
                    table.check_label(str(row["delivery_id"])),
                    self.t("ux.delivery_" + str(row.get("kind", "text"))),
                    str(row.get("channel", "")),
                    str(row.get("target", "")),
                    self.t(str(row.get("error_code") or "errors.operation")),
                    str(row.get("at", "")),
                    str(row.get("record_id") or row.get("item_id") or ""),
                    key=str(row["delivery_id"]),
                )
            if not self.rows:
                self.status(self.t("ux.no_failed_deliveries"))
        except Exception as exc:
            self.status(error_text(exc, self.service.t))
        finally:
            self._sync_controls()

    def _sync_controls(self) -> None:
        self.query_one("#delivery-retry", Button).disabled = self._retrying or not self.rows
        self.query_one("#delivery-refresh", Button).disabled = self._retrying

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.disabled:
            return
        if event.button.id == "delivery-close":
            self.dismiss(None)
        else:
            self.run_worker(
                self.operate(event.button.id or ""), exclusive=True, exit_on_error=False
            )

    async def operate(self, ident: str) -> None:
        if ident == "delivery-refresh":
            await self.reload_rows()
            return
        table = self.query_one("#delivery-table", SelectionTable)
        ids = sorted(table.checked_keys) or [str(row["delivery_id"]) for row in self.rows]
        self._retrying = True
        self._sync_controls()
        self.status(self.t("ux.retrying_delivery"))
        try:
            count = await self.service.retry_deliveries(ids)
            await self.reload_rows()
            self.status(self.t("ux.delivery_result", success=count, failed=len(ids) - count))
        except Exception as exc:
            self.status(error_text(exc, self.service.t))
        finally:
            self._retrying = False
            if self.is_mounted:
                self._sync_controls()


class DataManagementScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__(service)
        self.busy = False

    def action_close(self) -> None:
        if not self.busy:
            super().action_close()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("ux.backup_title"))
            yield Static(self.t("ux.backup_hint"), markup=False)
            yield Input(
                str(Path.home() / "Downloads" / f"mailflow-{datetime.now():%Y%m%d-%H%M%S}.zip"),
                id="backup-path",
            )
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(self.t("ux.create_backup"), id="backup-create")
                yield Button(self.t("ux.restore_backup"), id="backup-restore", variant="warning")
                yield Button(self.t("tui.btn_back"), id="backup-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.busy:
            return
        if event.button.id == "backup-close":
            self.dismiss(None)
        else:
            self.busy = True
            self.run_worker(
                self.operate_once(event.button.id or ""),
                exclusive=True,
                group="backup",
                exit_on_error=False,
            )

    async def operate_once(self, ident: str) -> None:
        for button in self.query(Button):
            button.disabled = True
        try:
            await self.operate(ident)
        finally:
            self.busy = False
            if self.is_mounted:
                for button in self.query(Button):
                    button.disabled = False

    async def operate(self, ident: str) -> None:
        path = self.query_one("#backup-path", Input).value.strip()
        if not path:
            self.status(self.t("ux.backup_zip_required"))
            return
        try:
            if ident == "backup-create":
                exists = await asyncio.to_thread(lambda: Path(path).expanduser().exists())
                if exists and not await self.ask(
                    self.confirmation(self.t("ux.overwrite_file", path=path))
                ):
                    return
                self.status(self.t("ux.backup_running"))
                saved = await self.service.create_backup(path, overwrite=exists)
                self.status(self.t("ux.backup_saved", path=saved))
            else:
                if not await self.ask(self.confirmation(self.t("ux.restore_confirm"))):
                    return
                self.status(self.t("ux.restore_running"))
                recovery = await self.service.restore_backup(path)
                self.status(self.t("ux.restore_done", path=recovery))
        except Exception as exc:
            self.status(error_text(exc, self.service.t))


class RemoteConnectionScreen(WorkflowScreen):
    def compose(self) -> ComposeResult:
        server = self.service.config.server
        with Vertical():
            yield Static(self.t("ux.remote_connection"))
            yield Static(self.t("ux.remote_connection_hint"), markup=False)
            yield Static(f"{server.host}:{server.port} · {server.username}", markup=False)
            yield Input(server.password, password=True, id="connection-password")
            yield Checkbox(self.t("ux.show_password"), id="connection-show-password")
            yield Button(self.t("tui.btn_back"), id="connection-close")

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "connection-show-password":
            self.query_one("#connection-password", Input).password = not event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)
