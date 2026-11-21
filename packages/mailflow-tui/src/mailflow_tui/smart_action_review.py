"""Run smart actions and review their changes in one temporary panel."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any, ClassVar, cast
from zoneinfo import ZoneInfo

from mailflow.domain import ActionItem, PendingOperation, SmartActionResult
from mailflow.service import MailFlowService
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, LoadingIndicator, Markdown, Static

from mailflow_tui.action_types import ActionTypeField
from mailflow_tui.labels import error_message
from mailflow_tui.pickers import AssistedInput
from mailflow_tui.widgets import Input, TextArea

logger = logging.getLogger(__name__)


class SmartActionReviewModal(ModalScreen[SmartActionResult | None]):
    BINDINGS: ClassVar[list[Any]] = []

    def __init__(
        self,
        service: MailFlowService,
        instruction: str,
        *,
        scope: dict[str, Any] | None = None,
        scope_name: str = "",
        conversation: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._instruction = instruction
        self._scope = {
            key: list(cast(list[Any], value)) if isinstance(value, list) else value
            for key, value in (scope or {}).items()
        }
        self._scope_name = scope_name
        self._conversation = [dict(message) for message in conversation or []]
        self._operations: list[PendingOperation] = []
        self._labels: dict[int, Static] = {}
        self._result: SmartActionResult | None = None
        self._task: asyncio.Task[None] | None = None
        self._state = "running"
        self._stopping = False
        self._apply_started = False
        self._error: Exception | None = None
        self._log_entries: deque[tuple[str, dict[str, Any]]] = deque(maxlen=300)
        self._bindings.bind("escape", "cancel", self._t("tui.btn_cancel"))

    @property
    def cancel_message(self) -> str:
        if self._error is not None and not self._apply_started:
            return error_message(self._service, self._error)
        return self._t(
            "tui.smart_action_stopped" if self._apply_started else "assistant.cancelled"
        )

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def _local_time(self, value: Any) -> str:
        if not value:
            return ""
        try:
            moment = datetime.fromisoformat(str(value))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return moment.astimezone(ZoneInfo(self._service.config.general.timezone)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (TypeError, ValueError):
            return str(value)

    def _description(self, index: int, operation: PendingOperation) -> str:
        args = operation.arguments
        if operation.summary_key == "assistant.preview":
            return self._t(operation.summary_key, **operation.summary_params)
        if operation.tool == "delete_mail":
            count = len(args.get("record_ids", []))
            subjects = " · ".join(args.get("subjects", [])[:5])
            if subjects and count > 5:
                subjects += "…"
            return self._t("tui.agent_pending_delete", count=count) + (
                f" · {subjects}" if subjects else ""
            )
        parts = [
            self._t(f"tui.smart_review_operation_{operation.tool}"),
            str(args.get("summary") or operation.summary_params.get("summary") or ""),
            self._local_time(args.get("due_at")),
            self._local_time(args.get("due_end")),
            str(args.get("location") or ""),
            str(args.get("url") or ""),
            str(args.get("notes") or ""),
            str(args.get("category") or ""),
            ", ".join(args.get("tags") or []),
            self._t("recurrence." + args["repeat"]["frequency"]) if args.get("repeat") else "",
        ]
        if args.get("completed"):
            parts.append(self._t("recurrence.complete_delete"))
        repeat: dict[str, Any] = args.get("repeat") or {}
        if repeat.get("weekdays"):
            parts.append(
                self._t("recurrence.weekdays")
                + ": "
                + ", ".join(self._t("interface.weekday_" + str(day)) for day in repeat["weekdays"])
            )
        if repeat.get("month_day"):
            parts.append(self._t("recurrence.month_day") + f": {repeat['month_day']}")
        if operation.tool == "delete_action":
            key = (
                "tui.smart_review_delete_analysis"
                if args.get("origin") == "analysis"
                else "tui.smart_review_delete_item"
            )
            parts.append(self._t(key))
        return " · ".join(part for part in parts if part)

    def _can_edit(self, operation: PendingOperation) -> bool:
        return operation.tool in {"add_action", "edit_action"}

    def compose(self) -> ComposeResult:
        with Vertical(id="smart-review-dialog"):
            yield Static(self._t("tui.smart_action"), id="smart-review-title")
            with ScrollableContainer(id="smart-action-body"):
                yield Static(self._t("tui.smart_action_request"), id="smart-action-request-title")
                yield Static(self._instruction, id="smart-action-request", markup=False)
                yield Static(self._scope_text(), id="smart-action-scope", markup=False)
                with Horizontal(id="smart-action-animation"):
                    yield LoadingIndicator()
                    yield Static(self._t("assistant.working"), id="smart-action-activity")
                yield Static("", id="smart-action-log", markup=False)
                yield Markdown("", id="smart-action-result")
                yield Static("", id="smart-action-objects", markup=False)
                yield Vertical(id="smart-review-operations")
                yield Static("", id="smart-review-error", markup=False)
            with Horizontal(id="smart-review-buttons"):
                yield Button(
                    self._t("tui.smart_action_reject"), id="smart-review-cancel", variant="error"
                )
                yield Button(
                    self._t("tui.btn_cancel"), id="smart-action-cancel", variant="error"
                )
                yield Button(
                    self._t("tui.smart_action_finish"), id="smart-review-apply", variant="success"
                )

    def _scope_text(self) -> str:
        return self._t("assistant.scope", scope=self._scope_name or self._t("assistant.all"))

    def on_mount(self) -> None:
        self.query_one("#smart-action-result").display = False
        self.query_one("#smart-action-objects").display = False
        self.query_one("#smart-review-operations").display = False
        self.query_one("#smart-review-error").display = False
        self._sync_controls()
        self._task = asyncio.create_task(self._execute(), name="smart-action")

    def on_resize(self, _event: events.Resize) -> None:
        if self.is_mounted:
            self._sync_controls()

    def _sync_controls(self) -> None:
        cancel = self.query_one_optional("#smart-action-cancel", Button)
        if cancel is None:
            return
        busy = self._state in {"running", "applying"}
        cancel.display = busy
        cancel.disabled = self._stopping
        self.query_one("#smart-review-cancel", Button).display = self._state == "review"
        self.query_one("#smart-review-cancel", Button).disabled = self._stopping
        self.query_one("#smart-review-apply", Button).display = not busy
        self.query_one("#smart-review-apply", Button).disabled = self._stopping
        self.query_one("#smart-review-operations").disabled = busy or self._stopping
        self.query_one("#smart-action-animation").display = (
            busy and self.app.size.width >= 100 and self.app.size.height >= 30
        )

    def _log_text(self, key: str, params: dict[str, Any]) -> str:
        params = dict(params)
        if "tool" in params:
            label_key = "tui.smart_action_tools." + str(params["tool"])
            label = self._t(label_key)
            params["tool"] = self._t("tui.smart_action_tool") if label == label_key else label
        return self._t(key, **params)

    def _log(self, key: str, **params: Any) -> None:
        self._log_entries.append((key, params))
        self._render_log()
        self.call_after_refresh(
            self.query_one("#smart-action-body", ScrollableContainer).scroll_end, animate=False
        )

    def _render_log(self) -> None:
        self.query_one("#smart-action-log", Static).update(
            "\n".join("· " + self._log_text(key, params) for key, params in self._log_entries)
        )

    def _progress(self, _stage: str, _done: int, _total: int, detail: Any) -> None:
        if self._stopping or not self.is_mounted:
            return
        if isinstance(detail, (tuple, list)) and len(detail) == 2:
            key, params = cast(tuple[str, dict[str, Any]], detail)
            # Progress names the operation; tool arguments and raw responses stay out of the log.
            self._log(
                "tui." + str(key),
                **{k: v for k, v in params.items() if k not in {"args", "result"}},
            )

    def _show_error(self, exc: Exception | None) -> None:
        self._error = exc
        error = self.query_one("#smart-review-error", Static)
        error.update(error_message(self._service, exc) if exc is not None else "")
        error.display = exc is not None
        if exc is not None:
            error.scroll_visible(animate=False)

    async def _execute(self) -> None:
        self._log("tui.smart_action_working")
        try:
            self._result = await self._service.smart_action(
                self._instruction,
                progress=self._progress,
                conversation=self._conversation,
                **self._scope,
            )
            self._log("tui.smart_action_steps", count=len(self._result.tool_steps))
            await self._show_review()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._state = "failed"
            self._show_error(exc)
            self._sync_controls()

    async def _show_review(self) -> None:
        result = self._result
        if result is None:
            return
        self._operations = [op.model_copy(deep=True) for op in result.pending]
        tasks = (
            {item.item_id: item for item in await self._service.list_actions_all()}
            if any(op.tool in {"edit_action", "delete_action"} for op in self._operations)
            else {}
        )
        for operation in self._operations:
            if operation.tool == "delete_mail":
                operation.arguments["subjects"] = [
                    record.mail.subject
                    if (record := await self._service.get_mail(identity))
                    else identity
                    for identity in operation.record_ids
                ]
            elif operation.tool in {"edit_action", "delete_action"}:
                item = tasks.get(str(operation.arguments.get("item_id") or ""))
                if item is not None:
                    operation.arguments = {**item.model_dump(mode="json"), **operation.arguments}
                    if operation.arguments.get("clear_due"):
                        operation.arguments["due_at"] = operation.arguments["due_end"] = None
                    elif operation.arguments.get("clear_end"):
                        operation.arguments["due_end"] = None
        lines = list(dict.fromkeys(filter(None, [result.final_text, *result.failure_reasons])))
        answer = self.query_one("#smart-action-result", Markdown)
        await answer.update("\n\n".join(lines))
        answer.display = bool(lines)
        objects = self.query_one("#smart-action-objects", Static)
        references = [record.mail.subject for record in result.records[:5]]
        references.extend(item.summary for item in result.actions[:5])
        objects.update("\n".join("• " + label for label in references))
        objects.display = bool(references)
        area = self.query_one("#smart-review-operations", Vertical)
        area.display = False
        await area.remove_children()
        self._labels.clear()
        if self._operations:
            await area.mount(
                Static(self._t("tui.smart_review_title"), id="smart-action-confirm-title"),
                Static(self._t("tui.smart_action_confirm_hint"), id="smart-action-confirm-hint"),
            )
        for index, operation in enumerate(self._operations):
            item = Vertical(classes="smart-review-item")
            await area.mount(item)
            await item.mount(
                Checkbox(
                    self._t("ux.include_change"), value=True, id=f"smart-review-include-{index}"
                )
            )
            if operation.tool == "delete_mail":
                for position, subject in enumerate(operation.arguments["subjects"]):
                    await item.mount(
                        Checkbox(
                            Text(str(subject)),
                            value=True,
                            id=f"smart-review-mail-{index}-{position}",
                        )
                    )
            label = Static(
                self._description(index, operation), markup=False, id=f"smart-review-op-{index}"
            )
            self._labels[index] = label
            row = Horizontal(classes="smart-review-row")
            await item.mount(row)
            await row.mount(label)
            if self._can_edit(operation):
                await row.mount(
                    Button(self._t("tui.smart_review_edit"), id=f"smart-review-edit-{index}")
                )
        area.display = bool(self._operations)
        self._state = "review"
        self._sync_controls()
        self.query_one("#smart-review-cancel", Button).focus()
        if self._operations:
            self.query_one("#smart-action-confirm-title").scroll_visible(animate=False)
        else:
            (answer if lines else objects).scroll_visible(animate=False)

    def _selected_operations(self) -> list[PendingOperation]:
        selected: list[PendingOperation] = []
        for index, operation in enumerate(self._operations):
            if not self.query_one(f"#smart-review-include-{index}", Checkbox).value:
                continue
            operation = operation.model_copy(deep=True)
            if operation.tool == "delete_mail":
                ids = [
                    rid
                    for pos, rid in enumerate(operation.record_ids)
                    if self.query_one(f"#smart-review-mail-{index}-{pos}", Checkbox).value
                ]
                if not ids:
                    continue
                operation.record_ids = ids
                operation.arguments["record_ids"] = ids
            selected.append(operation)
        return selected

    async def _apply(self, operations: list[PendingOperation]) -> None:
        result = self._result
        if result is None:
            return
        self._apply_started = True
        self._log("tui.smart_action_applying")
        try:
            outcome = await self._service.apply_smart_plan(result.plan_id, operations)
            result.pending = list(outcome["failed"])
            result.records = [r for r in result.records if r.record_id not in outcome["deleted"]]
            result.actions.extend(
                ActionItem.model_validate(item) for item in outcome.get("actions", [])
            )
            result.final_text = self._t(
                "tui.agent_applied_partial" if result.pending else "tui.agent_applied",
                applied=outcome["applied"],
                failed=len(result.pending),
            )
            result.final_text += "".join("\n" + error for error in outcome["errors"])
            result.final_text += "".join(
                "\n" + identity + ": " + state["message"]
                for identity, state in outcome.get("source_results", {}).items()
                if state["state"] != "synced"
            )
            if not result.pending:
                result.plan_id = ""
            result.records = [
                record
                for previous in result.records
                if (record := await self._service.get_mail(previous.record_id)) is not None
            ]
            if result.actions:
                current = {
                    item.item_id: item
                    for item in await self._service.list_actions_all(preview_days=30)
                }
                ids = dict.fromkeys(item.item_id for item in result.actions)
                result.actions = [current[item_id] for item_id in ids if item_id in current]
            if result.pending:
                await self._show_review()
            else:
                self._finish(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._state = "review"
            self._show_error(exc)
            self._sync_controls()

    async def _discard_plan(self) -> None:
        if self._result is not None and self._result.plan_id:
            async with asyncio.timeout(3):
                await self._service.workspace_call(
                    "assistant_cancel", {"plan_id": self._result.plan_id}
                )
            self._result.plan_id = ""
            self._result.pending.clear()

    async def _stop(self) -> None:
        try:
            if self._task is not None and not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            await self._discard_plan()
            self._finish(None)
        except Exception as exc:
            self._state = "failed"
            self._show_error(exc)
            self._stopping = False
            self._sync_controls()

    def _finish(self, result: SmartActionResult | None) -> None:
        self._state = "closed"
        self.dismiss(result)

    async def on_unmount(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        try:
            await self._discard_plan()
        except Exception:
            logger.exception("Could not discard the smart action plan during shutdown")

    async def relabel(self) -> None:
        for ident, key in (
            ("smart-review-title", "tui.smart_action"),
            ("smart-action-request-title", "tui.smart_action_request"),
            ("smart-action-activity", "assistant.working"),
            ("smart-action-confirm-title", "tui.smart_review_title"),
            ("smart-action-confirm-hint", "tui.smart_action_confirm_hint"),
        ):
            label = self.query_one_optional(f"#{ident}", Static)
            if label is not None:
                label.update(self._t(key))
        self.query_one("#smart-action-scope", Static).update(self._scope_text())
        for ident, key in (
            ("smart-review-cancel", "tui.smart_action_reject"),
            ("smart-action-cancel", "tui.btn_cancel"),
            ("smart-review-apply", "tui.smart_action_finish"),
        ):
            self.query_one(f"#{ident}", Button).label = self._t(key)
        for index, operation in enumerate(self._operations):
            include = self.query_one_optional(f"#smart-review-include-{index}", Checkbox)
            if include is not None:
                include.label = self._t("ux.include_change")
            if index in self._labels:
                self._labels[index].update(self._description(index, operation))
            edit = self.query_one_optional(f"#smart-review-edit-{index}", Button)
            if edit is not None:
                edit.label = self._t("tui.smart_review_edit")
        self._render_log()
        self._show_error(self._error)
        self._sync_controls()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        ident = event.button.id or ""
        if ident in {"smart-action-cancel", "smart-review-cancel"}:
            self.action_cancel()
        elif ident == "smart-review-apply" and self._state in {"review", "failed"}:
            if self._state == "failed":
                self._finish(None)
                return
            if self._result is None or not self._result.pending:
                self._finish(self._result)
                return
            selected = self._selected_operations()
            self._show_error(None)
            self._state = "applying"
            self._sync_controls()
            self.query_one("#smart-action-cancel", Button).focus()
            self._task = asyncio.create_task(self._apply(selected), name="smart-action-apply")
        elif ident.startswith("smart-review-edit-") and self._state == "review":
            index = int(ident.rsplit("-", 1)[1])
            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                SmartOperationEditModal(self._service, self._operations[index]),
                lambda values: self._updated(index, values),
            )

    def _updated(self, index: int, values: dict[str, Any] | None) -> None:
        if values is None:
            return
        operation = self._operations[index]
        operation.arguments.update(values)
        self._labels[index].update(self._description(index, operation))

    def action_cancel(self) -> None:
        if not self._stopping and self._state != "closed":
            self._stopping = True
            self._sync_controls()
            self.run_worker(self._stop(), group="smart-action-stop", exit_on_error=False)


class SmartOperationEditModal(ModalScreen[dict[str, Any] | None]):
    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService, operation: PendingOperation) -> None:
        super().__init__()
        self._service = service
        self._operation = operation.model_copy(deep=True)
        self._bindings.bind("escape", "cancel", self._t("tui.smart_review_cancel"))
        self._bindings.bind("ctrl+s", "save_edit", self._t("tui.btn_save"))

    def action_save_edit(self) -> None:
        self.query_one("#smart-review-save-edit", Button).press()

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def _time_text(self, raw: Any) -> str:
        if not raw:
            return ""
        try:
            value = datetime.fromisoformat(str(raw))
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(ZoneInfo(self._service.config.general.timezone)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (TypeError, ValueError):
            return ""

    def compose(self) -> ComposeResult:
        args = self._operation.arguments
        with Vertical(id="smart-review-edit-dialog"):
            yield Static(self._t("tui.smart_review_edit_title"), id="smart-review-edit-title")
            with ScrollableContainer(id="smart-review-edit-fields"):
                for key, field, default in (
                    ("summary", "summary", ""),
                    ("action_type", "type", ""),
                    ("start", "start", ""),
                    ("end", "end", ""),
                ):
                    yield Static(self._t(f"tui.smart_review_{key}"), classes="todo-label")
                    value = (
                        self._time_text(args.get("due_at" if field == "start" else "due_end"))
                        if field in {"start", "end"}
                        else str(args.get("action_type" if field == "type" else key) or default)
                    )
                    if field in {"start", "end"}:
                        yield AssistedInput(
                            self._service, value, id=f"smart-review-{field}", mode="datetime"
                        )
                    elif field == "type":
                        yield ActionTypeField(self._service, value, id="smart-review-type")
                    else:
                        yield Input(value=value, id=f"smart-review-{field}")
                yield Static(self._t("tui.smart_review_notes"), classes="todo-label")
                yield TextArea(str(args.get("notes") or ""), id="smart-review-notes")
                for field in ("location", "url"):
                    yield Static(self._t(f"tui.smart_review_{field}"), classes="todo-label")
                    yield Input(value=str(args.get(field) or ""), id=f"smart-review-{field}")
            yield Static("", id="smart-review-edit-error")
            with Horizontal(id="smart-review-edit-buttons"):
                yield Button(
                    self._t("tui.smart_review_save_edit"),
                    id="smart-review-save-edit",
                    variant="success",
                )
                yield Button(self._t("tui.smart_review_cancel"), id="smart-review-cancel-edit")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "smart-review-cancel-edit":
            self.dismiss(None)
        elif event.button.id == "smart-review-save-edit":
            self._save()

    def _save(self) -> None:
        error = self.query_one("#smart-review-edit-error", Static)
        summary = self.query_one("#smart-review-summary", Input).value.strip()
        action_type = self.query_one("#smart-review-type", ActionTypeField).value
        if not summary:
            error.update(self._t("tui.smart_review_required"))
            return
        try:
            zone = ZoneInfo(self._service.config.general.timezone)
            start_raw = self.query_one("#smart-review-start", Input).value.strip()
            start = (
                datetime.strptime(start_raw, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
                if start_raw
                else None
            )
            end_raw = self.query_one("#smart-review-end", Input).value.strip()
            end = (
                datetime.strptime(end_raw, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
                if end_raw
                else None
            )
            for field, raw in (("due_at", start_raw), ("due_end", end_raw)):
                original = self._operation.arguments.get(field)
                if original and raw == self._time_text(original):
                    value = datetime.fromisoformat(str(original))
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=UTC)
                    if field == "due_at":
                        start = value
                    else:
                        end = value
        except ValueError:
            error.update(self._t("tui.smart_review_invalid_time"))
            return
        if end is not None and (start is None or end <= start):
            error.update(self._t("tui.smart_review_invalid_window"))
            return
        values: dict[str, Any] = {
            "summary": summary,
            "due_at": start.isoformat() if start else None,
            "due_end": end.isoformat() if end else None,
            "action_type": action_type,
            "notes": self.query_one("#smart-review-notes", TextArea).text.strip(),
            "location": self.query_one("#smart-review-location", Input).value.strip(),
            "url": self.query_one("#smart-review-url", Input).value.strip(),
        }
        if self._operation.tool == "edit_action":
            values["clear_end"] = end is None
            values["clear_due"] = start is None
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)
