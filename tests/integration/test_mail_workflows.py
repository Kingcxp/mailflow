"""User workflows against SQLite, the service and the actual remote/UI adapters."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from mailflow.config import LLMConfig, MailFlowConfig, NotifierConfig, StorageConfig
from mailflow.contracts import (
    LLMCompletion,
    LLMRouter,
    MailSource,
    ProcessingContext,
    ProcessorResult,
    ToolCall,
)
from mailflow.domain import ActionItem, Attachment, MailAnalysis, MailMessage, MailRecord, Urgency
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.mail_query import MailQuery
from mailflow.pipeline import PipelineEngine, ProcessorBinding
from mailflow.plugins import PluginManager
from mailflow.protocol import json_value
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService
from mailflow_server.app import create_app
from mailflow_server.client import RemoteClient, RemoteError
from mailflow_storage_sqlite.plugin import SQLiteStorage
from mailflow_testkit.fakes import FakeMailSource
from mailflow_tui.confirm import ConfirmModal
from mailflow_tui.mail_workflows import AttachmentsScreen, MailReader, TrashScreen
from mailflow_tui.remote import RemoteServiceAdapter
from textual.app import App
from textual.widgets import Button, Input, Select, TextArea

from mailflow_test_support.commands import make_record
from mailflow_test_support.ui import mock_http_client, until


@pytest.fixture
async def service(tmp_path: Path) -> Any:
    config = MailFlowConfig()
    config.general.daily_digest = False
    config.server.username, config.server.password = "admin", "test-secret"
    config.storage = StorageConfig(path=str(tmp_path / "mail.db"))
    storage = SQLiteStorage(config.storage)
    await storage.initialize()
    instance = MailFlowService(
        config=config,
        registry=ComponentRegistry(),
        plugin_manager=PluginManager(),
        storage=storage,
        sources={},
        router=cast(LLMRouter, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )
    yield instance
    await instance.stop()


async def test_mail_states_search_and_task_survive_trash_and_reopen(
    service: MailFlowService,
) -> None:
    task = ActionItem(
        item_id="task", mail_id="mail", summary="Handle this mail", action_type="errand"
    )
    record = make_record("mail", action=task)
    record.mail.image_text = "Up date available"
    await service.storage.save_mail(record)
    assert (await service.query_mails(MailQuery(text="Update"))).total == 1
    assert (await service.query_mails(MailQuery(sender="name"))).total == 0
    assert (await service.query_mails(MailQuery(sender=record.mail.sender.address))).total == 1
    await service.storage.update_mail_analysis("mail", summary="First summary")
    assert (await service.query_mails(MailQuery(text="First summary"))).total == 1
    await service.storage.update_mail_analysis("mail", summary="Revised summary")
    assert (await service.query_mails(MailQuery(text="First summary"))).total == 0
    assert (await service.query_mails(MailQuery(text="Revised summary"))).total == 1
    await service.set_mail_state(["mail"], is_read=True, starred=True, archived=True)
    assert (await service.query_mails(MailQuery())).total == 0
    archived = await service.query_mails(MailQuery(state="archived", account_id="acct-1"))
    assert archived.records[0].is_read and archived.records[0].starred
    assert (await service.batch_mail(["mail", "missing"])).succeeded == ["mail"]
    assert [item.item_id for item in await service.list_actions()] == ["task"]
    await service.storage.close()
    await service.storage.initialize()
    trashed = await service.list_trash()
    assert len(trashed) == 1 and trashed[0].archived and trashed[0].starred
    await service.edit_action("task", completed=True)
    await service.batch_mail(["mail"], operation="restore")
    assert (await service.query_mails(MailQuery(text="Revised summary", state="all"))).total == 1
    assert not await service.list_actions()
    assert not await service.list_actions_all()
    with pytest.raises(ValueError):
        await service.edit_action("task", completed=False)
    assert (await service.batch_mail(["mail"], delete_actions=True)).succeeded == ["mail"]
    assert not await service.list_actions_all()

    # Completing one task must not suppress a different task with the same
    # title and time in the same mail, including after another analysis save.
    first = task.model_copy(update={"item_id": "a", "notes": "Client A"})
    second = task.model_copy(update={"item_id": "b", "notes": "Client B"})
    record = make_record("mail", action=first)
    assert record.analysis is not None
    record.analysis.action_items.append(second)
    await service.storage.save_mail(record)
    await service.edit_action("a", completed=True)
    assert [item.item_id for item in await service.list_actions()] == ["b"]
    first.summary += "."
    await service.storage.save_mail(record)
    assert [item.item_id for item in await service.list_actions()] == ["b"]


async def test_original_mail_is_readable_during_analysis_and_history_is_quiet(
    service: MailFlowService,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Processor:
        processor_id = "delayed"

        async def process(self, mail: Any, context: ProcessingContext) -> ProcessorResult:
            entered.set()
            await release.wait()
            return ProcessorResult(analysis=MailAnalysis(summary="Processed", urgency=Urgency.INFO))

    class Notifier:
        def __init__(self) -> None:
            self.delivered: list[str] = []

        async def notify(self, record: MailRecord) -> None:
            self.delivered.append(record.record_id)

    notifier = Notifier()
    await service.runtime.reconfigure(
        config=service.config,
        sources={},
        pipeline=PipelineEngine([ProcessorBinding("delayed", "fake", Processor())]),
        notifiers=[cast(Any, notifier)],
        notifier_configs=[
            NotifierConfig(provider="fake", notifier_id="fake", minimum_urgency=Urgency.AD)
        ],
    )
    mail = make_record().mail.model_copy(update={"ingestion": "history"})
    await service.runtime.receive_mail(mail)
    processing = asyncio.create_task(service.runtime.process_mail_now(mail))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        rows = await service.list_mails()
        assert len(rows) == 1 and rows[0].mail.body_text == mail.body_text
        assert rows[0].analysis_state == "analysing"
        assert (await service.query_mails(MailQuery(text="Original body"))).total == 1
    finally:
        release.set()
        await processing
    assert not notifier.delivered
    assert (await service.list_mails())[0].analysis_state == "ready"


async def test_remote_smart_task_is_reviewed_and_applied_once(
    service: MailFlowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Router:
        def __init__(self) -> None:
            self.turns = [
                LLMCompletion(
                    text="",
                    tool_calls=[
                        ToolCall(
                            call_id="task", name="add_action", arguments={"summary": "Buy paper"}
                        )
                    ],
                ),
                LLMCompletion(text="Ready for review"),
            ]

        async def chat(self, messages: Any, **kwargs: Any) -> LLMCompletion:
            return self.turns.pop(0)

    service.config.llms = [LLMConfig(llm_id="fake")]
    service.router = cast(Any, Router())
    transport = httpx.ASGITransport(app=create_app(service))
    offline = False

    async def route(request: httpx.Request) -> httpx.Response:
        if offline and request.url.path.startswith("/smart/jobs/"):
            raise httpx.ConnectError("offline", request=request)
        return await transport.handle_async_request(request)

    mock_http_client(monkeypatch, httpx.MockTransport(route))
    remote = RemoteServiceAdapter(
        RemoteClient("http://fake", "admin", "test-secret"), json_value(service.snapshot()), I18n()
    )
    progress: list[str] = []

    def on_progress(stage: str, *_: Any) -> None:
        progress.append(stage)

    result = await remote.smart_action("Add a task to buy paper, no deadline", progress=on_progress)
    assert result.pending and result.plan_id and progress
    assert not await service.list_actions()
    other_session = RemoteClient("http://fake", "admin", "test-secret")
    with pytest.raises(RemoteError):
        await other_session.apply_smart_plan(
            result.plan_id, [operation.model_dump(mode="json") for operation in result.pending]
        )
    assert not await service.list_actions()
    applied = await remote.apply_smart_plan(result.plan_id, result.pending)
    assert applied["applied"] == 1 and not applied["failed"]
    assert (await remote.list_actions())[0].due_at is None
    with pytest.raises(RemoteError):
        await remote.apply_smart_plan(result.plan_id, result.pending)
    assert len(await service.list_actions()) == 1

    entered, stopped = asyncio.Event(), asyncio.Event()

    # Stopping confirmation must stop server-side work as well as polling.
    service.router = cast(Any, Router())
    pending = await remote.smart_action("Add another task")
    add_action = service.add_action

    async def blocked_add(*args: Any, **values: Any) -> ActionItem:
        entered.set()
        try:
            await asyncio.Event().wait()
            return await add_action(*args, **values)
        finally:
            stopped.set()

    monkeypatch.setattr(service, "add_action", blocked_add)
    confirming = asyncio.create_task(remote.apply_smart_plan(pending.plan_id, pending.pending))
    await asyncio.wait_for(entered.wait(), 1)
    confirming.cancel()
    with pytest.raises(asyncio.CancelledError):
        await confirming
    await asyncio.wait_for(stopped.wait(), 1)
    assert len(await service.list_actions()) == 1
    monkeypatch.setattr(service, "add_action", add_action)
    with pytest.raises(RemoteError):
        await remote.apply_smart_plan(pending.plan_id, pending.pending)
    entered.clear()
    stopped.clear()

    class BlockingRouter:
        async def chat(self, messages: Any, **kwargs: Any) -> LLMCompletion:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            return LLMCompletion(text="")

    service.router = cast(Any, BlockingRouter())
    offline = True
    with pytest.raises(httpx.ConnectError):
        await remote.smart_action("Find messages")
    await asyncio.wait_for(entered.wait(), 1)
    assert not stopped.is_set()
    offline = False
    await remote.client.snapshot()
    await asyncio.wait_for(stopped.wait(), 1)

    # A vanished client must also release the server job without reconnecting.
    monkeypatch.setattr("mailflow_server.app._SMART_JOB_IDLE_SECONDS", 0.05)
    entered.clear()
    stopped.clear()
    async with httpx.AsyncClient(
        base_url="http://fake",
        auth=("admin", "test-secret"),
        headers={"X-MailFlow-Session": "idle-client"},
    ) as http:
        started = await http.post("/smart/jobs", json={"instruction": "Find messages"})
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(stopped.wait(), 1)
        job_id = started.json()["job_id"]
        status = await http.get(f"/smart/jobs/{job_id}")
        assert status.json()["state"] == "cancelled"
        assert (await http.delete(f"/smart/jobs/{job_id}")).is_success


async def test_task_notification_failure_retries_without_repeating_success(
    service: MailFlowService,
) -> None:
    class Channel:
        def __init__(self, failing: bool) -> None:
            self.failing = failing
            self.messages: list[str] = []

        async def push_text(self, text: str) -> None:
            if self.failing:
                raise OSError("offline")
            self.messages.append(text)

    good, flaky = Channel(False), Channel(True)
    await service.runtime.reconfigure(
        config=service.config,
        sources={},
        pipeline=service.pipeline,
        notifiers=cast(Any, [good, flaky]),
        notifier_configs=[
            NotifierConfig(provider="fake", notifier_id="good"),
            NotifierConfig(provider="fake", notifier_id="flaky"),
        ],
    )
    now = datetime.now(UTC)
    service.config.general.reminder_hour = 0
    task = await service.add_action("Due task", now + timedelta(days=2))
    await service.runtime.run_reminder_tick()
    failed = await service.failed_deliveries()
    assert failed and good.messages and not flaky.messages
    delivered = len(good.messages)
    flaky.failing = False
    await service.retry_deliveries([row["delivery_id"] for row in failed])
    assert len(good.messages) == delivered and flaky.messages
    assert not await service.failed_deliveries()
    await service.edit_action(task.item_id, completed=True)
    before = len(flaky.messages)
    await service.runtime.run_reminder_tick()
    assert len(flaky.messages) == before
    flaky.failing = True
    reopened = await service.add_action("Rescheduled task", now + timedelta(days=1))
    await service.runtime.run_reminder_tick()
    assert await service.failed_deliveries()
    await service.edit_action(
        reopened.item_id, snoozed_until=now + timedelta(days=1), reply_task=True
    )
    assert not await service.failed_deliveries()
    assert (await service.list_actions())[0].reply_task


async def test_assistant_mail_changes_respect_scope_and_retry_only_failed_items(
    service: MailFlowService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Router:
        async def chat(self, messages: Any, **kwargs: Any) -> LLMCompletion:
            if messages[-1]["role"] == "tool":
                return LLMCompletion(text="Ready for review")
            return LLMCompletion(
                text="",
                tool_calls=[
                    ToolCall(
                        call_id="outside",
                        name="write_reply",
                        arguments={"record_id": "other", "body": "Outside the selected scope"},
                    ),
                    ToolCall(
                        call_id="star",
                        name="update_mail",
                        arguments={"record_ids": ["kept", "missing"], "starred": True},
                    ),
                ],
            )

    for identity in ("kept", "missing", "other"):
        await service.storage.save_mail(make_record(identity))
    service.config.llms = [LLMConfig(llm_id="fake")]
    service.router = cast(Any, Router())
    result = await service.smart_action("Star these messages", record_ids=["kept", "missing"])
    assert [operation.tool for operation in result.pending] == ["update_mail"]
    assert not any(record.starred for record in await service.list_mails())
    save_mail = service.storage.save_mail
    fail_save = True

    async def flaky_save(record: MailRecord) -> None:
        if fail_save and record.record_id == "missing":
            raise OSError("Temporary storage failure")
        await save_mail(record)

    monkeypatch.setattr(service.storage, "save_mail", flaky_save)
    partial = await service.apply_smart_plan(result.plan_id, result.pending)
    assert partial["applied"] == 1 and partial["errors"]
    assert partial["failed"][0].arguments["record_ids"] == ["missing"]
    await service.set_mail_state(["kept"], starred=False)
    fail_save = False
    retry = await service.apply_smart_plan(result.plan_id, partial["failed"])
    assert retry["applied"] == 1 and not retry["failed"]
    assert {record.record_id for record in await service.list_mails() if record.starred} == {
        "missing"
    }


async def test_mail_read_download_and_trash_recovery(
    service: MailFlowService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = make_record("long")
    record.mail.body_text = "a" * 21000 + "END OF ORIGINAL"
    record.mail.body_html = ""
    record.mail.attachments = [Attachment(filename="first.txt"), Attachment(filename="second.txt")]
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Source(FakeMailSource):
        async def fetch_attachment(self, mail: MailMessage, index: int) -> bytes:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return b"FIRST" if index == 0 else b"SECOND"

    service.sources[record.mail.account_id] = Source()
    await service.storage.save_mail(record)
    app = App[None]()
    async with app.run_test(size=(110, 40)) as pilot:
        reader = MailReader(service, record)
        app.push_screen(reader)
        await pilot.pause()
        reader.query_one("#reader-next", Button).press()
        await pilot.pause()
        assert "END OF ORIGINAL" in reader.query_one("#reader-body", TextArea).text
        reader.query_one("#reader-close", Button).press()
        await pilot.pause()
        target = tmp_path / "downloads" / "first.txt"
        attachments = AttachmentsScreen(service, record)
        app.push_screen(attachments)
        await pilot.pause()
        attachments.query_one("#attachment-path", Input).value = str(target)
        attachments.query_one("#attachment-download", Button).press()
        await asyncio.wait_for(entered.wait(), 1)
        assert attachments.query_one("#attachment-choice", Select).disabled
        assert attachments.query_one("#attachment-path", Input).disabled
        attachments.query_one("#attachment-close", Button).press()
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not target.exists()
        attachments = AttachmentsScreen(service, record)
        app.push_screen(attachments)
        await pilot.pause()
        attachments.query_one("#attachment-path", Input).value = str(target)
        release.set()
        attachments.query_one("#attachment-download", Button).press()
        await until(pilot, lambda: attachments.saved == target)
        assert target.read_bytes() == b"FIRST"
        assert not list(target.parent.glob("*.part"))
        attachments.query_one("#attachment-choice", Select).value = "1"
        await pilot.pause()
        assert attachments.query_one("#attachment-open", Button).disabled
        attachments.query_one("#attachment-path", Input).value = str(target)
        attachments.query_one("#attachment-download", Button).press()
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmModal) and app.screen.is_mounted,
        )
        app.screen.query_one("#confirm-cancel", Button).press()
        await until(pilot, lambda: not attachments.busy)
        assert target.read_bytes() == b"FIRST"
        with monkeypatch.context() as patch:

            def refuse_replace(*args: Any, **kwargs: Any) -> Any:
                raise PermissionError("private file system detail")

            patch.setattr(Path, "replace", refuse_replace)
            attachments.query_one("#attachment-download", Button).press()
            await until(
                pilot, lambda: isinstance(app.screen, ConfirmModal) and app.screen.is_mounted
            )
            app.screen.query_one("#confirm-run", Button).press()
            await until(pilot, lambda: not attachments.busy)
            assert target.read_bytes() == b"FIRST"
            assert not list(target.parent.glob("*.part"))
            assert service.t("errors.access") in str(
                attachments.query_one("#workflow-status").render()
            )
        attachments.query_one("#attachment-download", Button).press()
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal) and app.screen.is_mounted)
        app.screen.query_one("#confirm-run", Button).press()
        await until(pilot, lambda: attachments.saved == target)
        assert target.read_bytes() == b"SECOND"
        attachments.query_one("#attachment-close", Button).press()
        await pilot.pause()
        await service.batch_mail(["long"])
        trash = TrashScreen(service)
        app.push_screen(trash)
        await pilot.pause()
        trash.query_one("#trash-restore", Button).press()
        for _ in range(20):
            await pilot.pause(0.05)
            if await service.storage.get_mail("long") is not None:
                break
        restored = await service.storage.get_mail("long")
        assert restored is not None and restored.mail.body_text.endswith("END OF ORIGINAL")
        assert not await service.list_trash()
        await pilot.pause()
        assert trash.query_one("#trash-restore", Button).disabled
        assert trash.query_one("#trash-purge", Button).disabled


async def test_backup_restores_configuration_mail_and_tasks_without_accepting_invalid_data(
    service: MailFlowService,
    tmp_path: Path,
) -> None:
    await service.storage.save_mail(make_record("saved"))
    task = await service.add_action("Unscheduled task")
    completed = await service.add_action("Completed task")
    await service.edit_action(completed.item_id, completed=True)
    # A fresh installation can use the configured language before a UI
    # preference has ever been written to the database.
    service.config.general.language = "zh-CN"
    service.i18n.set_language("zh-CN")
    path = await service.create_backup(str(tmp_path / "backup.zip"))
    if os.name == "posix":
        assert (await asyncio.to_thread(Path(path).stat)).st_mode & 0o777 == 0o600
    saved_backup = await asyncio.to_thread(Path(path).read_bytes)
    with pytest.raises(FileExistsError):
        await service.create_backup(path)
    assert await asyncio.to_thread(Path(path).read_bytes) == saved_backup
    await service.set_language("en")
    await service.batch_mail(["saved"])
    await service.delete_action(task.item_id)
    service.config.general.timezone = "Asia/Tokyo"
    recovery = await service.restore_backup(path)
    assert await asyncio.to_thread(Path(recovery).is_file)
    assert service.config.general.timezone == "UTC"
    assert await service.get_language() == service.config.general.language == "zh-CN"
    assert service.runtime.analysis_language == "zh-CN"
    assert await service.storage.get_mail("saved") is not None
    assert [record.record_id for record in await service.list_failed_mails()] == ["saved"]
    restored_tasks = await service.list_actions_all()
    assert len(restored_tasks) == 1 and restored_tasks[0].item_id == task.item_id
    assert all(item.item_id != completed.item_id for item in restored_tasks)
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a backup")
    with pytest.raises(ValueError):
        await service.restore_backup(str(broken))
    assert await service.storage.get_mail("saved") is not None
    assert len(await service.list_actions_all()) == 1
    assert await service.get_language() == "zh-CN"


def test_remembered_remote_password_uses_vault_and_migrates_old_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from mailflow_tui import credentials, remote

    vault: dict[str, str] = {}

    def store(target: str, password: str, username: str) -> None:
        vault[target] = password

    def delete(target: str) -> None:
        vault.pop(target, None)

    monkeypatch.setattr(credentials, "set_password", store)
    monkeypatch.setattr(credentials, "get_password", vault.get)
    monkeypatch.setattr(credentials, "delete_password", delete)
    session = tmp_path / "session.json"
    monkeypatch.setattr(remote, "_SESSION_FILE", session)
    session.write_text(
        json.dumps(
            {
                "url": "http://fake",
                "username": "admin",
                "saved_password": "private",
                "autologin": True,
            }
        ),
        encoding="utf-8",
    )
    assert remote.load_session()["saved_password"] == "private"
    assert "private" not in session.read_text(encoding="utf-8") and vault
    assert remote.load_session()["saved_password"] == "private"
    remote.clear_password()
    assert not vault and "saved_password" not in remote.load_session()


async def test_clear_during_analysis_covers_future_mail_and_prevents_resurrection(
    service: MailFlowService,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Processor:
        processor_id = "blocked"

        async def process(self, mail: Any, context: ProcessingContext) -> ProcessorResult:
            entered.set()
            await release.wait()
            return ProcessorResult(analysis=MailAnalysis(summary="Finished", urgency=Urgency.INFO))

    await service.runtime.reconfigure(
        config=service.config,
        sources={},
        notifiers=[],
        notifier_configs=[],
        pipeline=PipelineEngine([ProcessorBinding("blocked", "fake", Processor())]),
    )
    mail = make_record().mail.model_copy(
        update={"received_at": datetime.now(UTC) + timedelta(days=30)}
    )
    task = asyncio.create_task(service.process_mail(mail))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await service.clear_all_records() == (1, 0)
        assert await service.purge_trash_now() == 1
    finally:
        release.set()
        await task
    assert not await service.list_mails() and not await service.list_trash()
    assert await service.runtime.receive_mail(mail) is None
    assert not await service.list_actions_all()


async def test_history_analysis_updates_mail_before_batch_finishes_and_can_stop(
    service: MailFlowService,
) -> None:
    import queue

    from mailflow.config import MailAccountConfig
    from mailflow_testkit.fakes import FakeMailSource
    from mailflow_tui.app import MailFlowApp
    from mailflow_tui.widgets import SelectionTable
    from textual.widgets import Static, TabbedContent

    from mailflow_test_support.ui import until

    started, cancelled = asyncio.Event(), asyncio.Event()

    class Processor:
        processor_id = "history"

        async def process(self, mail: Any, context: ProcessingContext) -> ProcessorResult:
            if mail.subject == "History 1":
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return ProcessorResult(
                analysis=MailAnalysis(summary="Completed result", urgency=Urgency.INFO)
            )

    service.config.plugins.repositories = []
    service.config.accounts = [MailAccountConfig(account_id="acct-1", provider="fake")]
    mails = [make_record(str(i), subject=f"History {i}").mail for i in range(3)]
    for i, mail in enumerate(mails):
        mail.date = mail.received_at = datetime.now(UTC) - timedelta(days=i)
    service.sources["acct-1"] = cast(MailSource, FakeMailSource(mails))
    await service.runtime.reconfigure(
        config=service.config,
        sources={},
        notifiers=[],
        notifier_configs=[],
        pipeline=PipelineEngine([ProcessorBinding("history", "fake", Processor())]),
    )
    app = MailFlowApp(service, queue.Queue())
    async with app.run_test(size=(145, 48)) as pilot:
        app.query_one(TabbedContent).active = "tab-mailboxes"
        await pilot.pause()
        app.query_one("#account-history", Button).press()
        history = app.query_one("#history-table", SelectionTable)
        await until(pilot, lambda: history.row_count == 3)
        history.focus()
        await pilot.press("ctrl+a")
        analyze = app.query_one("#history-analyze", Button)
        analyze.press()
        await until(pilot, started.is_set)
        first_id = mails[0].normalized_message_id()
        mail_table = app.query_one("#mail-table", SelectionTable)
        await until(pilot, lambda: first_id in {str(k.value) for k in mail_table.rows})
        assert "Completed result" in str(app.query_one("#mail-summary", Static).render())
        analyze.press()
        await until(pilot, cancelled.is_set)
        await until(pilot, lambda: str(analyze.label) == service.t("tui.history_analyze"))
        records = await service.list_mails()
        assert len(records) == 2
        first = await service.get_mail(first_id)
        second = await service.get_mail(mails[1].normalized_message_id())
        assert first is not None and first.analysis is not None
        assert first.analysis.summary == "Completed result"
        assert second is not None and second.analysis_state == "cancelled"
        assert len(history.checked_keys) == 2


async def test_history_controls_follow_selection_loading_and_failure(
    service: MailFlowService,
) -> None:
    import queue

    from mailflow.config import MailAccountConfig
    from mailflow_testkit.fakes import FakeMailSource
    from mailflow_tui.app import MailFlowApp
    from mailflow_tui.settings import AccountsPane
    from mailflow_tui.widgets import SelectionTable
    from textual.widgets import DataTable, Input, TabbedContent

    from mailflow_test_support.ui import until

    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[int] = []
    fail = True
    mails = [make_record(str(index)).mail for index in range(3)]

    class Source(FakeMailSource):
        async def fetch_history(self, limit: int = 50, offset: int = 0) -> Any:
            calls.append(offset)
            entered.set()
            await release.wait()
            if fail:
                raise TimeoutError("connection timed out")
            return mails[offset : offset + limit]

    service.config.plugins.repositories = []
    app = MailFlowApp(service, queue.Queue())
    async with app.run_test(size=(145, 48)) as pilot:
        app.query_one(TabbedContent).active = "tab-mailboxes"
        await pilot.pause()
        pane = app.query_one(AccountsPane)
        for ident in (
            "account-edit",
            "account-delete",
            "account-history",
            "history-more",
            "history-analyze",
            "history-select-all",
            "btn-reply",
            "btn-reparse",
            "btn-trash",
            "actions-edit",
            "llm-edit",
            "llm-delete",
            "llm-up",
            "llm-down",
        ):
            assert app.query_one(f"#{ident}", Button).disabled
        service.config.accounts = [
            MailAccountConfig(account_id=ident, provider="fake") for ident in ("acct-1", "acct-2")
        ]
        service.sources.update(
            {ident: cast(MailSource, Source(mails)) for ident in ("acct-1", "acct-2")}
        )
        await pane.reload()
        app.query_one("#history-count", Input).value = "2"
        await pilot.pause()
        load = app.query_one("#account-history", Button)
        more = app.query_one("#history-more", Button)
        assert not load.disabled and more.disabled
        load.press()
        await until(pilot, entered.is_set)
        assert load.disabled and more.disabled
        more.press()
        await pilot.pause()
        assert calls == [0]
        release.set()
        await until(pilot, lambda: not load.disabled)
        assert more.disabled
        fail = False
        load.press()
        history = app.query_one("#history-table", SelectionTable)
        await until(pilot, lambda: history.row_count == 2)
        assert not more.disabled
        history.focus()
        await pilot.press("ctrl+a")
        assert not app.query_one("#history-analyze", Button).disabled
        accounts = cast(DataTable[Any], app.query_one("#accounts-table", DataTable))
        accounts.focus()
        await pilot.press("down")
        await until(pilot, lambda: history.row_count == 0)
        assert more.disabled and app.query_one("#history-analyze", Button).disabled
        assert not history.checked_keys


async def test_open_mailbox_refreshes_after_another_connection_clears_records(
    service: MailFlowService,
) -> None:
    import queue

    from mailflow_tui.app import MailFlowApp
    from mailflow_tui.widgets import SelectionTable

    from mailflow_test_support.ui import until

    service.config.plugins.repositories = []
    await service.storage.save_mail(make_record("external-clear"))
    await service.start()
    other = SQLiteStorage(service.config.storage)
    await other.initialize()
    try:
        app = MailFlowApp(service, queue.Queue())
        async with app.run_test(size=(145, 48)) as pilot:
            table = app.query_one("#mail-table", SelectionTable)
            await until(pilot, lambda: table.row_count == 1)
            await other.cleanup_mail(datetime.max.replace(tzinfo=UTC))
            await other.purge_trash(datetime.max.replace(tzinfo=UTC))
            await until(pilot, lambda: table.row_count == 0)
            assert not await service.list_mails() and not await service.list_trash()
    finally:
        await other.close()


async def test_recurring_tasks_keep_current_occurrence_and_preview_without_completed_history(
    service: MailFlowService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mailflow.recurrence as recurrence

    clock = datetime(2028, 1, 31, 10, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return clock.astimezone(tz)

    monkeypatch.setattr(recurrence, "datetime", Clock)
    await service.add_action(
        "Monthly bill",
        datetime(2028, 1, 31, 9, tzinfo=UTC),
        repeat={"frequency": "monthly"},
        category="Home",
        tags=["bills", "bills"],
    )
    january = (await service.list_actions())[0]
    assert january.due_at == datetime(2028, 1, 31, 9, tzinfo=UTC) and january.tags == ["bills"]
    assert [
        item.due_at for item in await service.list_actions_all(preview_days=30) if item.preview
    ] == [datetime(2028, 2, 29, 9, tzinfo=UTC)]
    from mailflow_tui.todo_create import TodoEditModal

    preview = next(item for item in await service.list_actions_all(preview_days=30) if item.preview)
    app: App[None] = App()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(TodoEditModal(service, preview))
        await pilot.pause()
        app.screen.query_one("#todo-summary", Input).value = "Renamed bill"
        app.screen.query_one("#todo-save", Button).press()
        await until(pilot, lambda: not isinstance(app.screen, TodoEditModal))
    plan = (await service.action_plans())[0]
    assert plan.template.due_at == january.due_at and plan.schedule.month_day == 31
    assert (await service.list_actions())[0].summary == "Monthly bill"
    assert (
        next(
            item for item in await service.list_actions_all(preview_days=30) if item.preview
        ).summary
        == "Renamed bill"
    )
    await service.edit_action(january.item_id, completed=True)
    assert not await service.list_actions_all()
    await service.storage.close()
    await service.storage.initialize()
    assert not await service.list_actions_all()
    clock = datetime(2028, 3, 31, 10, tzinfo=UTC)
    march = (await service.list_actions())[0]
    assert march.due_at == datetime(2028, 3, 31, 9, tzinfo=UTC)
    assert await service.stop_action_plan(march.plan_id)
    assert not [item for item in await service.list_actions_all(preview_days=30) if item.preview]
    assert (await service.list_actions())[0].item_id == march.item_id
    await service.edit_action(march.item_id, completed=True)
    assert not await service.list_actions_all()
    await service.add_action(
        "Weekly", clock.replace(hour=9), repeat={"frequency": "weekly", "weekdays": [0, 4]}
    )
    days = [
        item.due_at
        for item in await service.list_actions_all(preview_days=30)
        if item.due_at is not None
    ]
    assert all(day.weekday() in {0, 4} for day in days)
    clock += timedelta(days=40)
    assert (
        len(await service.list_actions()) == 2
    )  # existing outstanding + current, no missed occurrences
    future = await service.add_action(
        "Future weekly",
        clock + timedelta(days=10),
        repeat={"frequency": "weekly", "weekdays": [3]},
    )
    assert future.preview and future.due_at is not None and future.due_at.weekday() == 3
    assert all(item.item_id != future.item_id for item in await service.list_actions())
    assert await service.stop_action_plan(future.plan_id)
