"""The natural-language workspace, independent of mailbox filtering."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

from mailflow.domain import SmartActionResult
from mailflow.errors import error_info
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, DataTable, RichLog, Static

from mailflow_tui.smart_action_review import SmartActionReviewModal
from mailflow_tui.widgets import Input


class AssistantPane(Vertical):
    class OpenResult(Message):
        def __init__(self, kind: str, identity: str) -> None:
            super().__init__()
            self.kind = kind
            self.identity = identity

    DEFAULT_CSS = """
    AssistantPane { padding: 0 1; }
    #assistant-scope, #assistant-status, #assistant-answer { height: auto; max-height: 12; overflow-y: auto; }
    #assistant-input-row, #assistant-controls { height: auto; }
    #assistant-input { width: 1fr; }
    #assistant-progress { height: 6; display: none; }
    #assistant-results { height: 1fr; min-height: 3; }
    #assistant-controls Button { min-width: 10; width: 1fr; }
    """

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._scope: dict[str, Any] = {}
        self._scope_name = ""
        self._task: asyncio.Task[Any] | None = None
        self._result: SmartActionResult | None = None
        self._history: list[dict[str, str]] = []
        self._busy = False
        self._status_key = "assistant.ready"
        self._status_params: dict[str, Any] = {}
        self._status_extra = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="assistant-scope", markup=False)
        with Horizontal(id="assistant-input-row"):
            yield Input(placeholder=self._service.t("assistant.placeholder"), id="assistant-input")
        with Horizontal(id="assistant-controls"):
            yield Button(self._service.t("assistant.send"), id="assistant-send", variant="primary")
            yield Button(self._service.t("assistant.stop"), id="assistant-stop", disabled=True)
            yield Button(self._service.t("assistant.review"), id="assistant-review", disabled=True)
            yield Button(self._service.t("assistant.all"), id="assistant-all")
        yield Static("", id="assistant-status", markup=False)
        yield Button(self._service.t("assistant.configure_model"), id="assistant-configure")
        yield RichLog(id="assistant-progress", max_lines=100, wrap=True, markup=False)
        yield Static("", id="assistant-answer", markup=False)
        yield DataTable(id="assistant-results", cursor_type="row")

    async def on_mount(self) -> None:
        await self.relabel()

    async def on_unmount(self) -> None:
        await self.stop()

    async def relabel(self) -> None:
        for ident, key in (
            ("send", "send"),
            ("stop", "stop"),
            ("review", "review"),
            ("all", "all"),
        ):
            self.query_one(f"#assistant-{ident}", Button).label = self._service.t(
                f"assistant.{key}"
            )
        self.query_one("#assistant-input", Input).placeholder = self._service.t(
            "assistant.placeholder"
        )
        self.query_one("#assistant-configure", Button).label = self._service.t(
            "assistant.configure_model"
        )
        self._show_scope()
        self._render_status()
        self._render_result()

    def set_scope(self, scope: dict[str, Any], name: str) -> bool:
        if self._busy:
            return False
        self._scope = {
            key: list(cast(list[Any], value)) if isinstance(value, list) else value
            for key, value in scope.items()
        }
        self._scope_name = name
        self._history.clear()
        self._result = None
        self._show_scope()
        self._render_result()
        self.query_one("#assistant-input", Input).focus()
        return True

    def _show_scope(self) -> None:
        label = self._scope_name or self._service.t("assistant.all")
        self.query_one("#assistant-scope", Static).update(
            self._service.t("assistant.scope", scope=label)
        )

    def _show_status(self, key: str, **params: Any) -> None:
        self._status_key = key
        self._status_params = params
        self._status_extra = ""
        self._render_status()

    def _render_status(self) -> None:
        self.query_one("#assistant-status", Static).update(
            self._service.t(self._status_key, **self._status_params) + self._status_extra
        )

    def _show_error(self, exc: Exception) -> None:
        info = error_info(exc, self._service.t)
        self._show_status(info["code"], **info["params"])

    def _sync_controls(self) -> None:
        self.query_one("#assistant-send", Button).disabled = self._busy or not bool(
            self.query_one("#assistant-input", Input).value.strip()
        )
        self.query_one("#assistant-stop", Button).disabled = self._task is None or self._task.done()
        self.query_one("#assistant-all", Button).disabled = self._busy
        self.query_one("#assistant-review", Button).disabled = self._busy or not bool(
            self._result and self._result.pending
        )
        self.query_one("#assistant-configure", Button).display = not bool(
            self._service.snapshot().llms
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        self._sync_controls()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not self._busy:
            self.run_worker(self.execute(), group="assistant", exit_on_error=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "assistant-send":
            self.run_worker(self.execute(), group="assistant", exit_on_error=False)
        elif event.button.id == "assistant-stop":
            self.run_worker(self.stop(), group="assistant-stop", exit_on_error=False)
        elif event.button.id == "assistant-review":
            self.run_worker(self.review(), group="assistant-review", exit_on_error=False)
        elif event.button.id == "assistant-all":
            self.set_scope({}, "")
        elif event.button.id == "assistant-configure":
            self.post_message(self.OpenResult("model", ""))

    async def execute(self) -> None:
        text = self.query_one("#assistant-input", Input).value.strip()
        if self._busy or not text:
            return
        self._busy = True
        self._sync_controls()
        self._show_status("assistant.working")
        progress = self.query_one("#assistant-progress", RichLog)
        progress.clear()
        progress.display = True

        def report(stage: str, done: int, total: int, detail: Any) -> None:
            key = f"tui.smart_stage_{stage}"
            label = self._service.t(key)
            if label == key:
                label = self._service.t("assistant.working")
            progress.write(f"{label}  {done}/{total}")

        task = asyncio.create_task(
            self._service.smart_action(
                text, progress=report, conversation=self._history[-12:], **self._scope
            ),
            name="assistant-query",
        )
        self._task = task
        self._sync_controls()
        try:
            result = await task
            self._result = result
            refs = "\n".join(
                [f"mail {r.record_id}: {r.mail.subject}" for r in result.records]
                + [f"task {a.item_id}: {a.summary}" for a in result.actions]
            )
            self._history.extend(
                [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": result.final_text + "\n" + refs},
                ]
            )
            self._show_status("assistant.review_needed" if result.pending else "assistant.done")
            self._render_result()
        except asyncio.CancelledError:
            self._show_status("assistant.cancelled")
        except Exception as exc:
            self._show_error(exc)
        finally:
            self._task = None
            self._busy = False
            progress.clear()
            progress.display = False
            self._sync_controls()

    async def stop(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _render_result(self) -> None:
        table = cast(DataTable[str], self.query_one("#assistant-results", DataTable))
        cursor = table.cursor_row
        table.clear(columns=True)
        table.add_columns(self._service.t("assistant.kind"), self._service.t("assistant.result"))
        result = self._result
        answer = ""
        if result is not None:
            answer = "\n".join(dict.fromkeys([result.final_text, *result.failure_reasons])).strip()
            for record in result.records:
                table.add_row(
                    self._service.t("tui.tab_mail"),
                    record.mail.subject,
                    key=f"mail:{record.record_id}",
                )
            for item in result.actions:
                table.add_row(
                    self._service.t("tui.tab_actions"), item.summary, key=f"action:{item.item_id}"
                )
            for plugin in result.plugins:
                table.add_row(
                    self._service.t("assistant.plugin"),
                    str(plugin.get("name") or plugin["id"]),
                    key=f"plugin:{plugin['id']}",
                )
        self.query_one("#assistant-answer", Static).update(answer)
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
        self._sync_controls()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value or "")
        kind, _, identity = key.partition(":")
        if identity:
            self.post_message(self.OpenResult(kind, identity))

    async def review(self) -> None:
        result = self._result
        if self._busy or result is None or not result.pending:
            return
        self._busy = True
        self._sync_controls()
        try:
            operations = [op.model_copy(deep=True) for op in result.pending]
            tasks = {item.item_id: item for item in await self._service.list_actions_all()}
            for op in operations:
                if op.tool == "delete_mail":
                    op.arguments["subjects"] = [
                        mail.mail.subject
                        for rid in op.record_ids
                        if (mail := await self._service.get_mail(rid)) is not None
                    ]
                elif op.tool in {"edit_action", "delete_action"}:
                    item = tasks.get(str(op.arguments.get("item_id") or ""))
                    if item is not None:
                        op.arguments = {**item.model_dump(mode="json"), **op.arguments}
                        if op.arguments.get("clear_due"):
                            op.arguments["due_at"] = op.arguments["due_end"] = None
                        elif op.arguments.get("clear_end"):
                            op.arguments["due_end"] = None
            selected = await self.app.push_screen_wait(  # pyright: ignore[reportUnknownMemberType]
                SmartActionReviewModal(self._service, operations)
            )
            if selected is None:
                result.pending.clear()
                self._show_status("tui.smart_changes_cancelled")
                return
            task = asyncio.create_task(
                self._service.apply_smart_plan(result.plan_id, selected), name="assistant-apply"
            )
            self._task = task
            self._sync_controls()
            outcome = await task
            result.pending = list(outcome["failed"])
            self._show_status(
                "tui.agent_applied_partial" if result.pending else "tui.agent_applied",
                applied=outcome["applied"],
                failed=len(result.pending),
            )
            self._status_extra = "\n" + "\n".join(outcome["errors"]) if outcome["errors"] else ""
            self._render_status()
            self._history.append(
                {
                    "role": "assistant",
                    "content": f"Confirmed operations applied: {outcome['applied']}; failed: {len(result.pending)}",
                }
            )
        except asyncio.CancelledError:
            result.pending.clear()
            self._show_status("assistant.cancelled")
        except Exception as exc:
            self._show_error(exc)
        finally:
            self._task = None
            self._busy = False
            self._sync_controls()
