"""Small workflow dialogs shared by local and remote terminals."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import mimetypes
from pathlib import Path
from typing import Any, cast

from mailflow.domain import ReplyDraft, ReplyState
from mailflow.errors import error_text
from mailflow.mail_query import MailQuery
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Button, Checkbox, Select, Static

from mailflow_tui.mail_workflows import WorkflowScreen, save_attachment
from mailflow_tui.widgets import Input, SafeSelect, SelectionTable, TextArea


class SourceSyncScreen(WorkflowScreen):
    DEFAULT_CSS = "SourceSyncScreen #source-status { height: 10; min-height: 6; }"

    def __init__(self, service: MailFlowService, record_ids: list[str] | None = None) -> None:
        super().__init__(service)
        self.record_ids = list(record_ids or [])
        self.busy = False
        self.source_state: dict[str, Any] = {"accounts": [], "operations": []}
        self.capabilities: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("experience.source_sync"))
            with ScrollableContainer():
                yield Static(self.t("experience.source_confirmation"), markup=False)
                yield SafeSelect(
                    [(a.account_id, a.account_id) for a in self.service.config.accounts],
                    id="source-account",
                    prompt=self.t("tui.entry_pick_first"),
                )
                yield Checkbox(self.t("experience.source_enabled"), id="source-enabled")
                for role in ("archive", "trash", "sent"):
                    yield Static(self.t("experience.folder_" + role))
                    yield Input(id="source-" + role)
                yield TextArea("", id="source-status", read_only=True)
            yield SafeSelect([], id="source-pending", prompt=self.t("experience.source_pending"))
            yield Static("", id="workflow-status", markup=False)
            for row in [
                [("save", "saved"), ("detect", "source_detect"), ("refresh", "source_refresh")],
                [
                    ("retry", "source_retry"),
                    ("delete", "source_delete"),
                    ("restore", "source_restore"),
                    ("back", "back"),
                ],
            ]:
                with Horizontal():
                    for op, key in row:
                        yield Button(
                            self.t("experience." + key) if op != "save" else self.t("tui.btn_save"),
                            id="source-" + op,
                            disabled=op != "back",
                        )

    async def load_data(self) -> None:
        await self.render_state(await self.service.workspace_call("source_status"))

    async def render_state(self, state: dict[str, Any]) -> None:
        self.source_state = state
        text: list[str] = []
        for row in state["accounts"]:
            if row["capabilities"]:
                self.capabilities[row["account_id"]] = row["capabilities"]
            text.append(
                row["account_id"]
                + ": "
                + self.t("experience.source_enabled" if row["enabled"] else "experience.source_off")
            )
            for key, value in row["capabilities"].items():
                if key == "folders":
                    text.extend(
                        self.t("experience.folder_" + role) + ": " + name
                        for role, name in value.items()
                    )
                else:
                    text.append(
                        self.t("experience.capability_" + key)
                        + ": "
                        + (
                            self.t("common.yes" if value else "common.no")
                            if isinstance(value, bool)
                            else str(value)
                        )
                    )
        self.query_one("#source-status", TextArea).load_text("\n".join(text))
        cast(Select[str], self.query_one("#source-pending", Select)).set_options(
            [(r["account_id"] + " · " + r["record_id"], r["id"]) for r in state["operations"]]
        )
        self.update_buttons()

    def update_buttons(self) -> None:
        if not self.is_attached:
            return
        account_id = cast(Select[str], self.query_one("#source-account", Select)).value
        selected = next(
            (r for r in self.source_state["accounts"] if r["account_id"] == account_id), None
        )
        supported = bool(selected and selected["supported"])
        for op in ("save", "detect", "refresh", "retry", "delete", "restore"):
            available = supported
            if op == "detect":
                available = any(r["supported"] for r in self.source_state["accounts"])
            elif op == "refresh":
                available = any(
                    r["supported"] and r["enabled"] for r in self.source_state["accounts"]
                )
            elif op == "retry":
                available = isinstance(
                    cast(Select[str], self.query_one("#source-pending", Select)).value, str
                )
            elif op in {"delete", "restore"}:
                available = bool(
                    self.record_ids
                    and selected
                    and selected["enabled"]
                    and self.capabilities.get(str(account_id), {}).get(
                        "trash" if op == "delete" else "restore"
                    )
                )
            self.query_one("#source-" + op, Button).disabled = self.busy or not available
        self.query_one("#source-enabled", Checkbox).disabled = self.busy or not supported

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "source-account":
            account = next(
                (a for a in self.service.config.accounts if a.account_id == event.value), None
            )
            if account:
                self.query_one("#source-enabled", Checkbox).value = account.state_sync
                for role in ("archive", "trash", "sent"):
                    self.query_one("#source-" + role, Input).value = str(
                        account.options.get(role + "_folder", "")
                    )
        self.update_buttons()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.busy:
            return
        if event.button.id == "source-back":
            self.dismiss(None)
            return
        self.busy = True
        self.update_buttons()
        self.run_worker(self.operate(str(event.button.id)), exclusive=True, exit_on_error=False)

    async def operate(self, ident: str) -> None:
        try:
            if ident == "source-save":
                account_id = cast(Select[str], self.query_one("#source-account", Select)).value
                index = next(
                    i
                    for i, a in enumerate(self.service.config.accounts)
                    if a.account_id == account_id
                )
                account = self.service.config.accounts[index].model_copy(deep=True)
                account.state_sync = self.query_one("#source-enabled", Checkbox).value
                for role in ("archive", "trash", "sent"):
                    raw = self.query_one("#source-" + role, Input).value.strip()
                    if raw:
                        account.options[role + "_folder"] = raw
                    else:
                        account.options.pop(role + "_folder", None)
                await self.service.update_config_entry("accounts", index, account.model_dump())
                self.status(self.t("experience.saved"))
            elif ident in {"source-delete", "source-restore"}:
                pending = await self.service.workspace_call(
                    "source_prepare",
                    {
                        "operation": "delete" if ident == "source-delete" else "restore",
                        "record_ids": self.record_ids,
                    },
                )
                preview = pending["pending"][0]["summary_params"]["preview"]
                if not await self.ask(self.confirmation(preview)):
                    await self.service.workspace_call(
                        "assistant_cancel", {"plan_id": pending["plan_id"]}
                    )
                    return
                outcome = await self.service.workspace_call(
                    "source_confirm", {"plan_id": pending["plan_id"]}
                )
                self.status(
                    "\n".join(r["message"] for r in outcome.get("source_results", {}).values())
                )
            elif ident == "source-retry":
                if not await self.ask(self.confirmation(self.t("experience.source_confirmation"))):
                    return
                result = await self.service.workspace_call(
                    "source_retry",
                    {"id": str(cast(Select[str], self.query_one("#source-pending", Select)).value)},
                )
                self.status(result["message"])
            state = await self.service.workspace_call(
                "source_refresh" if ident == "source-refresh" else "source_status",
                {"detect": ident == "source-detect"},
            )
            await self.render_state(state)
        except Exception as exc:
            self.status(error_text(exc, self.t))
        finally:
            self.busy = False
            self.update_buttons()


class AttachmentLibraryScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService, account_id: str = "") -> None:
        super().__init__(service)
        self.account_id = account_id
        self.rows: list[dict[str, Any]] = []
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("experience.attachment_library"))
            yield Input(placeholder=self.t("experience.filename"), id="library-name")
            yield Input(placeholder=self.t("experience.file_type"), id="library-type")
            yield Input(
                self.account_id,
                placeholder=self.t("experience.account_filter"),
                id="library-account",
            )
            yield Input(placeholder=self.t("experience.after_date"), id="library-after")
            yield Input(placeholder=self.t("experience.before_date"), id="library-before")
            yield Input(
                str(Path.home() / "Downloads"),
                placeholder=self.t("ux.download_path"),
                id="library-path",
            )
            yield SelectionTable(t=self.t, id="library-table")
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(self.t("experience.search"), id="library-search")
                yield Button(
                    self.t("experience.download_selected"), id="library-download", disabled=True
                )
                yield Button(self.t("experience.source"), id="library-source", disabled=True)
                yield Button(self.t("tui.btn_back"), id="library-back")

    async def load_data(self) -> None:
        table = self.query_one("#library-table", SelectionTable)
        table.add_column("[ ]", key="selected", width=3)
        table.add_columns(
            self.t("experience.filename"),
            self.t("experience.file_type"),
            self.t("experience.account_filter"),
        )
        table.add_column(self.t("experience.cache_state"), key="cache")
        table.cursor_type = "row"
        self.busy = True
        self.update_controls()
        try:
            await self.reload_rows()
        finally:
            self.busy = False
            self.update_controls()

    def update_controls(self) -> None:
        if not self.is_attached:
            return
        for name in ("download", "source", "search"):
            self.query_one("#library-" + name, Button).disabled = self.busy or (
                name != "search" and not self.rows
            )
        self.query_one("#library-table", SelectionTable).disabled = self.busy
        for field in self.query(Input):
            field.disabled = self.busy

    async def reload_rows(self) -> None:
        values: dict[str, Any] = {
            "filename": self.query_one("#library-name", Input).value,
            "content_type": self.query_one("#library-type", Input).value,
            "account_id": self.query_one("#library-account", Input).value.strip(),
        }
        for key in ("after", "before"):
            raw = self.query_one("#library-" + key, Input).value.strip()
            if raw:
                values[key] = raw
        self.rows = await self.service.workspace_call("attachments", values)
        table = self.query_one("#library-table", SelectionTable)
        table.clear()
        table.retain_checks(set())
        for index, row in enumerate(self.rows):
            table.add_row(
                "[ ]",
                row["filename"],
                row["content_type"],
                row["account_id"],
                self.t("experience.cached" if row["cached"] else "experience.source_required"),
                key=str(index),
            )
        if not self.rows:
            self.status(self.t("tui.empty"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "library-back":
            self.dismiss(None)
            return
        if self.busy:
            return
        self.busy = True
        self.update_controls()
        self.run_worker(self.operate(str(event.button.id)), exclusive=True, exit_on_error=False)

    async def operate(self, ident: str) -> None:
        try:
            if ident == "library-search":
                await self.reload_rows()
                return
            table = self.query_one("#library-table", SelectionTable)
            if not self.rows:
                return
            selected = sorted(table.checked_keys, key=int) or [str(table.cursor_row)]
            if ident == "library-source":
                self.dismiss(self.rows[int(selected[0])]["record_id"])
                return
            table.set_checks(set(selected))
            raw_directory = self.query_one("#library-path", Input).value
            directory = await asyncio.to_thread(lambda: Path(raw_directory).expanduser())
            success, skipped = 0, 0
            errors: list[str] = []
            self.status(self.t("ux.downloading"))
            for key in selected:
                row = self.rows[int(key)]
                target = directory / row["filename"].replace("\\", "/").split("/")[-1]
                if target.exists() and not await self.ask(
                    self.confirmation(self.t("ux.overwrite_file", path=str(target)))
                ):
                    skipped += 1
                    continue
                try:
                    downloaded = await self.service.workspace_call("attachment_download", row)
                    await asyncio.to_thread(
                        save_attachment, target, base64.b64decode(downloaded["data"])
                    )
                    success += 1
                    row["cached"] = True
                    table.update_cell(key, "cache", self.t("experience.cached"))
                    table.set_checks(table.checked_keys - {key})
                except Exception as exc:
                    errors.append(row["filename"] + ": " + error_text(exc, self.t))
            self.status(
                "\n".join(
                    [
                        self.t(
                            "experience.download_result",
                            success=success,
                            failed=len(errors),
                            skipped=skipped,
                        ),
                        *errors,
                    ]
                )
            )
        except Exception as exc:
            self.status(error_text(exc, self.t))
        finally:
            self.busy = False
            self.update_controls()


class NotificationPreferencesScreen(WorkflowScreen):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("experience.notification_preferences"))
            yield SafeSelect(
                [
                    (self.t("experience.preset_" + p), p)
                    for p in ("custom", "instant", "important", "tasks")
                ],
                id="notification-preset",
                allow_blank=False,
            )
            yield Checkbox(self.t("experience.quiet_hours"), id="quiet-enabled")
            yield Input("22:00", id="quiet-start")
            yield Input("08:00", id="quiet-end")
            yield TextArea("", id="notification-state", read_only=True)
            yield SafeSelect(
                [], id="connection-setting", prompt=self.t("experience.connection_settings")
            )
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(self.t("tui.btn_save"), id="notification-save")
                yield Button(self.t("experience.connection_check"), id="notification-check")
                yield Button(
                    self.t("experience.connection_settings"),
                    id="notification-settings",
                    disabled=True,
                )
                yield Button(self.t("tui.btn_back"), id="notification-back")

    async def load_data(self) -> None:
        state = await self.service.workspace_call("notification_preferences")
        self.query_one("#notification-preset", Select).value = state["preset"]
        self.query_one("#quiet-enabled", Checkbox).value = state["quiet_enabled"]
        self.query_one("#quiet-start", Input).value = state["quiet_start"]
        self.query_one("#quiet-end", Input).value = state["quiet_end"]
        self.query_one("#notification-state", TextArea).load_text(
            "\n".join(
                c["id"]
                + "\n"
                + self.t(
                    "experience.channel_mail_state",
                    state=c["reason"] or self.t("experience.enabled"),
                )
                + "\n"
                + self.t(
                    "experience.channel_task_state",
                    state=c["task_reason"] or self.t("experience.enabled"),
                )
                for c in state["channels"]
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "notification-back":
            self.dismiss(None)
        else:
            self.run_worker(self.operate(str(event.button.id)), exclusive=True, exit_on_error=False)

    async def operate(self, ident: str) -> None:
        buttons = list(self.query(Button))
        for button in buttons:
            button.disabled = True
        try:
            if ident == "notification-save":
                await self.service.workspace_call(
                    "notification_preferences",
                    {
                        "preset": str(
                            cast(Select[str], self.query_one("#notification-preset", Select)).value
                        ),
                        "quiet_enabled": self.query_one("#quiet-enabled", Checkbox).value,
                        "quiet_start": self.query_one("#quiet-start", Input).value,
                        "quiet_end": self.query_one("#quiet-end", Input).value,
                    },
                )
                self.status(self.t("experience.saved"))
                await self.on_mount()
            elif ident == "notification-settings":
                from mailflow_tui.settings import EntryFormScreen

                key = str(cast(Select[str], self.query_one("#connection-setting", Select)).value)
                group, raw_index = key.rstrip("]").split("[")
                index = int(raw_index)
                entries: list[Any] = getattr(self.service.config, group)

                async def save(values: dict[str, Any]) -> bool:
                    await self.service.update_config_entry(group, index, values)
                    return True

                await self.ask(
                    EntryFormScreen(
                        self.service, group, values=entries[index].model_dump(), on_save=save
                    )
                )
            else:
                self.status(self.t("experience.checking"))
                rows = await self.service.workspace_call("connection_check")
                cast(Select[str], self.query_one("#connection-setting", Select)).set_options(
                    [(r["name"], r["settings"]) for r in rows]
                )
                self.query_one("#notification-state", TextArea).load_text(
                    "\n\n".join(
                        r["name"] + ": " + r["message"] + "\n" + r["settings"] for r in rows
                    )
                )
                self.status(self.t("experience.check_complete"))
        except Exception as exc:
            self.status(error_text(exc, self.t))
        finally:
            for button in buttons:
                button.disabled = False
            self.query_one("#notification-settings", Button).disabled = not isinstance(
                cast(Select[str], self.query_one("#connection-setting", Select)).value, str
            )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "connection-setting":
            self.query_one("#notification-settings", Button).disabled = event.value is Select.BLANK


class AssistantHistoryScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__(service)
        self.rows: list[dict[str, Any]] = []
        self.enabled = True

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("experience.history"))
            yield Input(placeholder=self.t("experience.history_search"), id="history-search")
            yield SafeSelect([], id="history-row", prompt=self.t("tui.entry_pick_first"))
            yield TextArea("", id="history-text", read_only=True)
            yield SafeSelect([], id="history-ref", prompt=self.t("experience.source"))
            yield Static("", id="workflow-status", markup=False)
            for row in [
                [("open", "source"), ("delete", "delete"), ("clear", "clear")],
                [("toggle", "history_toggle"), ("close", "back")],
            ]:
                with Horizontal():
                    for name, key in row:
                        yield Button(
                            self.t("experience." + key),
                            id="history-" + name,
                            disabled=name in {"open", "delete"},
                        )

    async def load_data(self) -> None:
        await self.reload_rows()

    async def reload_rows(self) -> None:
        state = await self.service.workspace_call(
            "assistant_history", {"query": self.query_one("#history-search", Input).value}
        )
        self.rows = state["records"]
        self.enabled = bool(state["retention_days"])
        self.status(
            self.t("experience.history_enabled" if self.enabled else "experience.history_disabled")
        )
        cast(Select[str], self.query_one("#history-row", Select)).set_options(
            [(r["at"][:16] + " · " + r["request"][:80], r["id"]) for r in self.rows]
        )
        self.query_one("#history-text", TextArea).load_text("")
        cast(Select[str], self.query_one("#history-ref", Select)).set_options([])

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self.reload_rows()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "history-row":
            row = next((r for r in self.rows if r["id"] == event.value), None)
            self.query_one("#history-delete", Button).disabled = row is None
            if row:
                text = row["request"] + "\n\n" + row["answer"]
                for ref in row["references"]:
                    text += (
                        "\n\n"
                        + ref["title"]
                        + "\n"
                        + (ref["quote"] if ref["verified"] else self.t("experience.unverified"))
                    )
                self.query_one("#history-text", TextArea).load_text(text)
                cast(Select[str], self.query_one("#history-ref", Select)).set_options(
                    [(r["title"], r["kind"] + ":" + r["id"]) for r in row["references"]]
                )
        elif event.select.id == "history-ref":
            self.query_one("#history-open", Button).disabled = event.value is Select.BLANK

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        ident = event.button.id
        if ident == "history-close":
            self.dismiss(None)
        elif ident == "history-open":
            value = str(cast(Select[str], self.query_one("#history-ref", Select)).value)
            self.dismiss(value.split(":", 1))
        elif ident in {"history-delete", "history-clear", "history-toggle"}:
            values: dict[str, Any] = (
                {"record_id": str(cast(Select[str], self.query_one("#history-row", Select)).value)}
                if ident == "history-delete"
                else {"clear": True}
                if ident == "history-clear"
                else {"retention_days": 0 if self.enabled else 30}
            )
            try:
                await self.service.workspace_call("assistant_history_change", values)
                await self.reload_rows()
            except Exception as exc:
                self.status(error_text(exc, self.t))


class ActionFiltersScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService, category: str, tag: str) -> None:
        super().__init__(service)
        self.category, self.tag = category, tag

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("experience.action_filters"))
            with ScrollableContainer():
                for name, value, key in [
                    ("category", self.category, "category"),
                    ("tag", self.tag, "tags"),
                ]:
                    yield Static(self.t("recurrence." + key))
                    yield Input(value, id="action-" + name)
                    yield SafeSelect(
                        [], id="action-choose-" + name, prompt=self.t("tui.entry_pick_first")
                    )
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(self.t("tui.btn_apply"), id="action-filter-apply")
                yield Button(self.t("tui.btn_back"), id="action-filter-close")

    async def load_data(self) -> None:
        items = await self.service.list_actions_all()
        for name, choices in [
            ("category", {a.category for a in items if a.category}),
            ("tag", {tag for a in items for tag in a.tags}),
        ]:
            cast(Select[str], self.query_one("#action-choose-" + name, Select)).set_options(
                [(value, value) for value in sorted(choices)]
            )

    def on_select_changed(self, event: Select.Changed) -> None:
        if (
            event.select.id
            and event.select.id.startswith("action-choose-")
            and isinstance(event.value, str)
        ):
            self.query_one(
                "#action-" + event.select.id.removeprefix("action-choose-"), Input
            ).value = event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(
            (
                self.query_one("#action-category", Input).value.strip(),
                self.query_one("#action-tag", Input).value.strip(),
            )
            if event.button.id == "action-filter-apply"
            else None
        )


class PlansScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService) -> None:
        super().__init__(service)
        self.rows: list[Any] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("experience.plans"))
            yield SafeSelect([], id="plan-row", prompt=self.t("tui.entry_pick_first"))
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                for op in ("pause", "resume", "skip", "back"):
                    yield Button(self.t("experience." + op), id="plan-" + op, disabled=op != "back")

    async def load_data(self) -> None:
        self.rows = await self.service.action_plans()
        cast(Select[str], self.query_one("#plan-row", Select)).set_options(
            [
                (
                    p.template.summary + (" · " + self.t("experience.paused") if p.paused else ""),
                    p.plan_id,
                )
                for p in self.rows
            ]
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        plan = next((p for p in self.rows if p.plan_id == event.value), None)
        self.query_one("#plan-pause", Button).disabled = plan is None or plan.paused
        self.query_one("#plan-resume", Button).disabled = plan is None or not plan.paused
        self.query_one("#plan-skip", Button).disabled = plan is None or plan.paused

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "plan-back":
            self.dismiss(None)
            return
        plan_id = str(cast(Select[str], self.query_one("#plan-row", Select)).value)
        values: dict[str, Any] = {
            "plan_id": plan_id,
            "operation": str(event.button.id).removeprefix("plan-"),
        }
        try:
            if values["operation"] == "skip":
                items = [a for a in await self.service.list_actions_all() if a.plan_id == plan_id]
                if not items:
                    self.status(self.t("tui.empty"))
                    return
                values["item_id"] = items[-1].item_id
            await self.service.workspace_call("plan_control", values)
            self.status(self.t("experience.saved"))
            await self.on_mount()
        except Exception as exc:
            self.status(error_text(exc, self.t))


class SavedFiltersScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService, query: MailQuery) -> None:
        super().__init__(service)
        self.current_query = query
        self.rows: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("experience.saved_filters"))
            yield SafeSelect([], id="saved-filter", prompt=self.t("tui.entry_pick_first"))
            yield Input(placeholder=self.t("experience.filter_name"), id="filter-name")
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(self.t("tui.btn_apply"), id="filter-open", disabled=True)
                yield Button(self.t("tui.btn_save"), id="filter-save")
                yield Button(self.t("tui.btn_delete"), id="filter-delete", disabled=True)
                yield Button(self.t("tui.btn_back"), id="filter-close")

    async def load_data(self) -> None:
        await self.reload_rows()

    async def reload_rows(self) -> None:
        self.rows = await self.service.workspace_call("filters")
        cast(Select[str], self.query_one("#saved-filter", Select)).set_options(
            [
                (
                    r["name"]
                    + (
                        ""
                        if r["available"]
                        else " · " + self.t("experience.filter_account_missing")
                    ),
                    r["name"],
                )
                for r in self.rows
            ]
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "saved-filter":
            chosen = next((r for r in self.rows if r["name"] == event.value), None)
            self.query_one("#filter-open", Button).disabled = (
                chosen is None or not chosen["available"]
            )
            self.query_one("#filter-delete", Button).disabled = chosen is None

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        choice = cast(Select[str], self.query_one("#saved-filter", Select)).value
        try:
            if event.button.id == "filter-close":
                self.dismiss(None)
            elif event.button.id == "filter-open":
                row = next(r for r in self.rows if r["name"] == choice)
                self.dismiss(MailQuery.model_validate(row["query"]))
            elif event.button.id == "filter-save":
                await self.service.workspace_call(
                    "save_filter",
                    {
                        "name": self.query_one("#filter-name", Input).value,
                        "query": self.current_query.model_dump(mode="json"),
                    },
                )
                await self.reload_rows()
                self.status(self.t("experience.saved"))
            elif event.button.id == "filter-delete":
                await self.service.workspace_call("delete_filter", {"name": str(choice)})
                await self.reload_rows()
        except Exception as exc:
            self.status(error_text(exc, self.t))


class ThreadScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService, record_id: str) -> None:
        super().__init__(service)
        self.record_id = record_id
        self.rows: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("experience.thread"))
            yield SafeSelect([], id="thread-message", prompt=self.t("tui.entry_pick_first"))
            yield TextArea(read_only=True, id="thread-body")
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(self.t("ux.full_mail"), id="thread-open", disabled=True)
                yield Button(self.t("tui.btn_back"), id="thread-close")

    async def load_data(self) -> None:
        try:
            result = await self.service.workspace_call("thread", {"record_id": self.record_id})
            self.rows = result["messages"]
            cast(Select[str], self.query_one("#thread-message", Select)).set_options(
                [
                    (f"{r['at'][:16]} · {r['subject']} · {r['sender']}", str(i))
                    for i, r in enumerate(self.rows)
                ]
            )
            if self.rows:
                self.query_one("#thread-message", Select).value = "0"
        except Exception as exc:
            self.status(error_text(exc, self.t))

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "thread-message" or event.value is Select.NULL:
            return
        row = self.rows[int(str(event.value))]
        text = row["body"]
        if row["tasks"]:
            text += "\n\n" + "\n".join(a["summary"] for a in row["tasks"])
        self.query_one("#thread-body", TextArea).load_text(text)
        self.query_one("#thread-open", Button).disabled = row["kind"] != "mail"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        value = cast(Select[str], self.query_one("#thread-message", Select)).value
        self.dismiss(
            self.rows[int(str(value))]["id"]
            if event.button.id == "thread-open" and value is not Select.NULL
            else None
        )


class ComposeScreen(WorkflowScreen):
    def __init__(
        self,
        service: MailFlowService,
        *,
        draft: ReplyDraft | None = None,
        kind: str = "new",
        mail_id: str = "",
    ) -> None:
        super().__init__(service)
        self.draft = draft
        self.kind, self.mail_id = kind, mail_id
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.t("experience.compose"))
            with ScrollableContainer():
                yield Static(self.t("ux.account"))
                yield SafeSelect(
                    [
                        (a.email or a.account_id, a.account_id)
                        for a in self.service.snapshot().accounts
                        if a.enabled
                    ],
                    id="compose-account",
                    value=Select.NULL,
                    prompt=self.t("tui.entry_pick_first"),
                )
                for name in ("to", "cc", "bcc", "subject"):
                    yield Static(self.t("experience." + name))
                    yield Input(id="compose-" + name)
                yield TextArea(id="compose-body")
                yield Static("", id="compose-files", markup=False)
                yield Input(placeholder=self.t("experience.upload_path"), id="compose-path")
                with Horizontal():
                    yield Button(self.t("experience.add_file"), id="compose-upload")
                    yield Button(self.t("experience.choose_files"), id="compose-choose-files")
                yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(self.t("tui.btn_save"), id="compose-save", variant="primary")
                yield Button(
                    self.t("tui.reply_prepare"),
                    id="compose-prepare",
                    variant="warning",
                    disabled=True,
                )
                yield Button(self.t("tui.reply_confirm"), id="compose-confirm", disabled=True)
                yield Button(self.t("experience.sent_check"), id="compose-check", disabled=True)
                yield Button(self.t("tui.btn_back"), id="compose-close")

    async def load_data(self) -> None:
        try:
            if self.draft is None and self.kind != "new":
                self.draft = ReplyDraft.model_validate(
                    await self.service.workspace_call(
                        "compose", {"kind": self.kind, "mail_id": self.mail_id}
                    )
                )
            if self.draft:
                self.query_one("#compose-account", Select).value = self.draft.account_id
                for name, value in [
                    ("to", ", ".join(a.display for a in self.draft.to_addresses)),
                    ("cc", ", ".join(a.display for a in self.draft.cc)),
                    ("bcc", ", ".join(a.display for a in self.draft.bcc)),
                    ("subject", self.draft.subject),
                ]:
                    self.query_one("#compose-" + name, Input).value = value
                self.query_one("#compose-body", TextArea).load_text(self.draft.body)
            self.sync_controls()
        except Exception as exc:
            self.status(error_text(exc, self.t))

    def values(self) -> dict[str, Any]:
        return {
            **{
                name: self.query_one("#compose-" + name, Input).value
                for name in ("to", "cc", "bcc", "subject")
            },
            "body": self.query_one("#compose-body", TextArea).text,
        }

    def unchanged(self) -> bool:
        if not self.draft:
            return False
        from mailflow.workflows import addresses

        values = self.values()
        return (
            values["subject"] == self.draft.subject
            and values["body"] == self.draft.body
            and addresses(values["to"]) == self.draft.to_addresses
            and addresses(values["cc"]) == self.draft.cc
            and addresses(values["bcc"]) == self.draft.bcc
        )

    def sync_controls(self) -> None:
        if not self.is_attached:
            return
        locked = bool(
            self.draft
            and self.draft.state in {ReplyState.SENT, ReplyState.SENDING, ReplyState.CANCELLED}
        )
        selected = cast(Select[str], self.query_one("#compose-account", Select)).value
        for button in self.query(Button):
            button.disabled = self.busy or (locked and button.id != "compose-close")
        self.query_one("#compose-prepare", Button).disabled = (
            self.busy or locked or selected is Select.NULL
        )
        self.query_one("#compose-confirm", Button).disabled = self.busy or not (
            self.draft
            and self.unchanged()
            and self.draft.is_confirmation_valid(self.draft.token or "")
        )
        self.query_one("#compose-check", Button).disabled = self.busy or not (
            self.draft and self.draft.state == ReplyState.SENDING
        )
        self.query_one("#compose-check", Button).display = bool(
            self.draft and self.draft.state == ReplyState.SENDING
        )
        for name in ("to", "cc", "bcc", "subject"):
            self.query_one("#compose-" + name, Input).disabled = self.busy or locked
        self.query_one("#compose-body", TextArea).read_only = self.busy or locked
        self.query_one("#compose-account", Select).disabled = self.busy or self.draft is not None
        self.query_one("#compose-files", Static).update(
            "\n".join(f"{f.filename} ({f.size} B)" for f in self.draft.attachments)
            if self.draft
            else ""
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        self.sync_controls()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self.sync_controls()

    def on_select_changed(self, event: Select.Changed) -> None:
        self.sync_controls()

    async def save(self) -> None:
        if self.draft is None:
            account = cast(Select[str], self.query_one("#compose-account", Select)).value
            if account is Select.NULL:
                raise ValueError(self.t("ux.choose_account"))
            self.draft = ReplyDraft.model_validate(
                await self.service.workspace_call(
                    "compose", {"kind": "new", "account_id": str(account), **self.values()}
                )
            )
        elif not self.unchanged():
            self.draft = ReplyDraft.model_validate(
                await self.service.workspace_call(
                    "edit_message", {"draft_id": self.draft.draft_id, **self.values()}
                )
            )

    def action_close(self) -> None:
        if not self.busy:
            self.busy = True
            self.sync_controls()
            self.run_worker(self.operate("compose-close"), exclusive=True, exit_on_error=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if not self.busy and not event.button.disabled:
            self.busy = True
            self.sync_controls()
            self.run_worker(
                self.operate(event.button.id or ""), exclusive=True, exit_on_error=False
            )

    async def operate(self, action: str) -> None:
        self.busy = True
        self.sync_controls()
        try:
            if action == "compose-close" and (
                (self.draft is None and not any(self.values().values()))
                or (
                    self.draft is not None
                    and self.draft.state
                    in {ReplyState.SENT, ReplyState.SENDING, ReplyState.CANCELLED}
                )
            ):
                self.dismiss(None)
                return
            if action == "compose-confirm":
                if self.draft and self.unchanged():
                    self.draft = await self.service.confirm_reply(
                        self.draft.draft_id, self.draft.token or ""
                    )
                    self.status(
                        self.t("reply.sent", draft_id=self.draft.draft_id)
                        + (
                            "\n" + self.t("experience.sent_copy_failed")
                            if self.draft.sent_copy_error
                            else ""
                        )
                    )
                return
            if action == "compose-check" and self.draft:
                self.draft = ReplyDraft.model_validate(
                    await self.service.workspace_call(
                        "reply_reconcile", {"draft_id": self.draft.draft_id}
                    )
                )
                self.status(
                    self.t("reply.sent", draft_id=self.draft.draft_id)
                    if self.draft.state == ReplyState.SENT
                    else self.t("reply.send_uncertain")
                )
                return
            await self.save()
            assert self.draft is not None
            if action == "compose-upload":
                raw_path = self.query_one("#compose-path", Input).value
                path = await asyncio.to_thread(lambda: Path(raw_path).expanduser())
                from mailflow.domain import MAX_ATTACHMENT_BYTES

                if await asyncio.to_thread(lambda: path.stat().st_size) > MAX_ATTACHMENT_BYTES:
                    raise ValueError(self.t("experience.file_too_large"))
                data = await asyncio.to_thread(path.read_bytes)
                uploaded = await self.service.workspace_call(
                    "upload",
                    {
                        "filename": path.name,
                        "content_type": mimetypes.guess_type(path.name)[0]
                        or "application/octet-stream",
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                )
                self.draft = ReplyDraft.model_validate(
                    await self.service.workspace_call(
                        "edit_message",
                        {
                            "draft_id": self.draft.draft_id,
                            **self.values(),
                            "file_ids": [f.file_id for f in self.draft.attachments]
                            + [uploaded["file_id"]],
                        },
                    )
                )
                self.query_one("#compose-path", Input).value = ""
            elif action == "compose-choose-files":
                result = await self.ask(
                    FileSelectionScreen(self.service, [f.file_id for f in self.draft.attachments])
                )
                if result is not None:
                    self.draft = ReplyDraft.model_validate(
                        await self.service.workspace_call(
                            "edit_message",
                            {"draft_id": self.draft.draft_id, **self.values(), "file_ids": result},
                        )
                    )
            elif action == "compose-prepare":
                self.draft = await self.service.prepare_reply(self.draft.draft_id)
                self.status(
                    self.t(
                        "experience.send_preview",
                        account=self.draft.account_id,
                        to=", ".join(a.display for a in self.draft.to_addresses),
                        cc=", ".join(a.display for a in self.draft.cc),
                        bcc=", ".join(a.display for a in self.draft.bcc),
                        files=", ".join(f.filename for f in self.draft.attachments),
                    )
                )
            else:
                self.status(self.t("experience.saved"))
                if action == "compose-close":
                    self.dismiss(None)
        except Exception as exc:
            if action in {"compose-confirm", "compose-check"} and self.draft:
                with contextlib.suppress(Exception):
                    self.draft = await self.service.get_draft(self.draft.draft_id)
            self.status(error_text(exc, self.t))
        finally:
            self.busy = False
            self.sync_controls()


class FileSelectionScreen(WorkflowScreen):
    def __init__(self, service: MailFlowService, selected: list[str]) -> None:
        super().__init__(service)
        self.selected = selected

    def compose(self) -> ComposeResult:
        from mailflow_tui.widgets import SelectionTable

        with Vertical():
            yield Static(self.t("experience.choose_files"))
            yield SelectionTable(t=self.t, id="files-table")
            yield Static("", id="workflow-status", markup=False)
            with Horizontal():
                yield Button(self.t("tui.btn_apply"), id="files-apply", disabled=True)
                yield Button(self.t("tui.btn_cancel"), id="files-close")

    async def load_data(self) -> None:
        from mailflow_tui.widgets import SelectionTable

        table = self.query_one("#files-table", SelectionTable)
        table.add_column("[ ]", key="selected", width=3)
        table.add_column(self.t("experience.filename"))
        table.cursor_type = "row"
        table.checked_keys.update(self.selected)
        for row in await self.service.workspace_call("files"):
            table.add_row(
                table.check_label(row["file_id"]),
                f"{row['filename']} ({row['size']} B)",
                key=row["file_id"],
            )
        self.query_one("#files-apply", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        from mailflow_tui.widgets import SelectionTable

        event.stop()
        self.dismiss(
            sorted(self.query_one("#files-table", SelectionTable).checked_keys)
            if event.button.id == "files-apply"
            else None
        )
