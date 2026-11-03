"""User workflows against SQLite, the service and the actual remote/UI adapters."""

from __future__ import annotations

import asyncio
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
from mailflow.domain import ActionItem, MailAnalysis, MailRecord, Urgency
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
from mailflow_tui.mail_workflows import MailReader, TrashScreen
from mailflow_tui.remote import RemoteServiceAdapter
from textual.app import App
from textual.widgets import Button, TextArea

from mailflow_test_support.commands import make_record
from mailflow_test_support.ui import mock_http_client


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
    assert not await service.list_actions()
    assert len(await service.list_actions_all()) == 1
    await service.edit_action("task", completed=False)
    assert len(await service.list_actions()) == 1
    assert (await service.batch_mail(["mail"], delete_actions=True)).succeeded == ["mail"]
    assert not await service.list_actions_all()


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
    mock_http_client(monkeypatch, httpx.ASGITransport(app=create_app(service)))
    remote = RemoteServiceAdapter(
        RemoteClient("http://fake", "admin", "test-secret"), json_value(service.snapshot()), I18n()
    )
    progress: list[str] = []

    def on_progress(stage: str, *_: Any) -> None:
        progress.append(stage)

    result = await remote.smart_action("Add a task to buy paper, no deadline", progress=on_progress)
    assert result.pending and result.plan_id and progress
    assert not await service.list_actions()
    applied = await remote.apply_smart_plan(result.plan_id, result.pending)
    assert applied["applied"] == 1 and not applied["failed"]
    assert (await remote.list_actions())[0].due_at is None
    with pytest.raises(RemoteError):
        await remote.apply_smart_plan(result.plan_id, result.pending)
    assert len(await service.list_actions()) == 1


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
    task = await service.add_action("Due task", now - timedelta(minutes=1))
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
    reopened = await service.edit_action(task.item_id, completed=False, summary="Rescheduled task")
    await service.runtime.run_reminder_tick()
    assert await service.failed_deliveries()
    await service.edit_action(
        reopened.item_id, snoozed_until=now + timedelta(days=1), reply_task=True
    )
    assert not await service.failed_deliveries()
    assert (await service.list_actions())[0].reply_task


async def test_reader_reaches_end_and_trash_restore_changes_mailbox(
    service: MailFlowService,
) -> None:
    record = make_record("long")
    record.mail.body_text = "a" * 21000 + "END OF ORIGINAL"
    record.mail.body_html = ""
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
    await service.edit_action(task.item_id, completed=True)
    path = await service.create_backup(str(tmp_path / "backup.zip"))
    await service.batch_mail(["saved"])
    await service.delete_action(task.item_id)
    service.config.general.timezone = "Asia/Tokyo"
    recovery = await service.restore_backup(path)
    assert await asyncio.to_thread(Path(recovery).is_file)
    assert service.config.general.timezone == "UTC"
    assert await service.storage.get_mail("saved") is not None
    restored_tasks = await service.list_actions_all()
    assert len(restored_tasks) == 1 and restored_tasks[0].completed_at is not None
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a backup")
    with pytest.raises(ValueError):
        await service.restore_backup(str(broken))
    assert await service.storage.get_mail("saved") is not None
    assert len(await service.list_actions_all()) == 1


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
