"""Local and remote users keep their view while data and language change."""

from __future__ import annotations

import queue
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from mailflow.domain import (
    ActionItem,
    Attachment,
    MailAddress,
    PluginSnapshot,
    ProcessorNote,
    Urgency,
)
from mailflow.i18n import I18n
from mailflow.protocol import json_value
from mailflow.tools import ToolRegistry
from mailflow_server.app import create_app
from mailflow_server.client import RemoteClient
from mailflow_tui.app import (
    ActionModal,
    ActionsPane,
    MailFlowApp,
    MailPane,
    MarketPane,
    ReplyModal,
    RuntimePane,
)
from mailflow_tui.confirm import ConfirmModal
from mailflow_tui.mail_workflows import MailReader
from mailflow_tui.remote import RemoteServiceAdapter
from mailflow_tui.widgets import Input as EditingInput
from mailflow_tui.widgets import SelectionTable, ShortcutHelp
from mailflow_tui.widgets import TextArea as EditingTextArea
from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer
from textual.coordinate import Coordinate
from textual.widgets import Button, DataTable, Input, Select, Static, TabbedContent, TextArea

from mailflow_test_support.commands import MemoryStorage, commands_service, make_record
from mailflow_test_support.ui import mock_http_client, until


@pytest.fixture(params=[False, True], ids=["local", "remote"])
def host(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    service = commands_service()
    service.config.plugins.repositories = []
    service.config.server.username, service.config.server.password = "admin", "secret"
    snapshot = service.snapshot
    plugins = [PluginSnapshot(plugin_id=f"plugin-{i:02}", name=f"Plugin {i}") for i in range(35)]
    monkeypatch.setattr(
        service, "snapshot", lambda: snapshot().model_copy(update={"plugins": plugins})
    )
    monkeypatch.setattr(MailFlowApp, "_eager_market_fetch", AsyncMock())
    monkeypatch.setattr(MailFlowApp, "_preload_market_cache", AsyncMock())
    monkeypatch.setattr(MarketPane, "on_mount", AsyncMock())
    failures: set[str] = set()
    transport = httpx.ASGITransport(app=create_app(service))

    async def respond(req: httpx.Request) -> httpx.Response:
        if req.url.path in failures:
            return httpx.Response(503, json={"detail": "offline [retry]"})
        return await transport.handle_async_request(req)

    mock_http_client(monkeypatch, httpx.MockTransport(respond))
    client = RemoteClient("http://fake", "admin", "secret")
    remote = bool(request.param)
    view = (
        RemoteServiceAdapter(client, json_value(service.snapshot()), I18n()) if remote else service
    )
    app = MailFlowApp(view, queue.Queue(), remote=remote)

    async def emit(event: str, **payload: Any) -> None:
        if remote:
            await client._dispatch(event, json_value(payload))  # pyright: ignore[reportPrivateUsage]
        else:
            await service.events.emit(event, **payload)

    return service, view, app, emit, failures


def keys(table: DataTable[Any]) -> list[str]:
    return [str(key.value) for key in table.rows]


def cursor(table: DataTable[Any]) -> str:
    return str(table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value)


def text(app: Any, ident: str) -> str:
    return str(app.query_one(ident, Static).render())


async def settle(pilot: Any) -> None:
    """Wait for debounced reads, event workers and the resulting layout."""
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def test_stored_html_is_readable_in_mail_reader_and_related_task(host: Any) -> None:
    service, view, app, _emit, _failures = host
    source = (
        "<p>First paragraph<BR class='break'>Second line</p>"
        "<div>Next block</div><ul><li>First item</li><li>Second item</li></ul>"
        "<p><a href='https://example.com/details'>Read details</a></p>"
        "<style>secret-style</style><div hidden>secret-preview</div>"
    )
    task = ActionItem(
        item_id="html-task", mail_id="html", summary="Follow up", action_type="errand"
    )
    record = make_record("html", action=task)
    record.mail.body_text = source
    record.mail.body_html = source
    await service.storage.save_mail(record)
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(pilot)
        body = text(app, "#mail-body")
        assert "First paragraph\nSecond line\n\nNext block\n• First item\n• Second item" in body
        assert "https://example.com/details" in body
        assert "<p>" not in body and "secret-" not in body
        search = app.query_one("#mail-search", Input)
        search.value = "example.com/details"
        await settle(pilot)
        assert keys(app.query_one("#mail-table", DataTable)) == ["html"]
        loaded = await view.get_mail("html")
        assert loaded is not None
        reader = MailReader(view, loaded)
        app.push_screen(reader)
        await pilot.pause()
        full = reader.query_one("#reader-body", TextArea).text
        assert "First paragraph\nSecond line" in full and "• First item\n• Second item" in full
        assert "<p>" not in full and "secret-" not in full
        reader.query_one("#reader-close", Button).press()
        await pilot.pause()
        app.push_screen(ActionModal(view, task))
        await pilot.pause()
        detail = str(app.screen.query_one("#action-mail-detail", Static).render())
        assert "First paragraph\nSecond line" in detail and "<p>" not in detail
    stored = await service.get_mail("html")
    assert (
        stored is not None and stored.mail.body_text == source and stored.mail.body_html == source
    )


@pytest.mark.parametrize("multiline", [False, True], ids=["input", "editor"])
async def test_text_editing_shortcuts_copy_cut_paste_undo_and_redo(multiline: bool) -> None:
    class EditorApp(App[None]):
        def compose(self) -> ComposeResult:
            yield (
                EditingTextArea("Original", id="field")
                if multiline
                else EditingInput("Original", id="field")
            )
            yield EditingInput("password", password=True, id="secret")

    app = EditorApp()
    async with app.run_test() as pilot:
        field = app.query_one("#field", EditingTextArea if multiline else EditingInput)

        def value() -> str:
            return field.text if isinstance(field, EditingTextArea) else field.value

        field.focus()
        await pilot.press("ctrl+a", "ctrl+c")
        assert app.clipboard == "Original"
        await pilot.press("ctrl+x")
        assert value() == ""
        await pilot.press("ctrl+z")
        assert value() == "Original"
        await pilot.press("ctrl+y")
        assert value() == ""
        await pilot.press("ctrl+v")
        assert value() == "Original"

        app.copy_to_clipboard("Replacement")
        await pilot.press("ctrl+a", "ctrl+v")
        assert value() == "Replacement"
        await pilot.press("ctrl+z")
        assert value() == "Original"
        await pilot.press("ctrl+y", "f7", "ctrl+insert")
        assert app.clipboard == "Replacement"
        await pilot.press("shift+delete")
        assert value() == ""
        await pilot.press("shift+insert")
        assert value() == "Replacement"

        # An empty session clipboard must not erase selected text, or Ctrl+C quit the app.
        app.copy_to_clipboard("")
        await pilot.press("ctrl+a", "ctrl+v")
        assert value() == "Replacement"
        await pilot.press("end", "ctrl+c")
        assert app.is_running
        secret = app.query_one("#secret", EditingInput)
        secret.focus()
        await pilot.press("ctrl+a", "ctrl+c", "ctrl+x")
        assert app.clipboard == "" and secret.value == "password"


async def test_checked_batches_keep_scope_retry_failures_and_undo_mail(
    host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _view, app, emit, _failures = host
    for ident in ("old-1", "old-2", "keep"):
        record = make_record(
            ident, Urgency.AD if ident.startswith("old") else Urgency.IMPORTANT, subject=ident
        )
        record.mail.received_at = datetime.now(UTC) - timedelta(days=3)
        await service.storage.save_mail(record)
    past = datetime.now(UTC) - timedelta(hours=1)
    actions = [await service.add_action(f"Expired {i}", past) for i in range(2)]
    future = await service.add_action("Keep future", datetime.now(UTC) + timedelta(days=2))
    storage = cast(MemoryStorage, service.storage)
    delete = storage.delete_mail
    failing = {"old-2"}

    async def delete_with_failure(ident: str, **kwargs: Any) -> None:
        if ident in failing:
            raise OSError("private storage failure")
        await delete(ident, **kwargs)

    monkeypatch.setattr(storage, "delete_mail", delete_with_failure)
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(pilot)
        table = app.query_one("#mail-table", SelectionTable)
        app.query_one("#mail-urgency-filter", Select).value = "expired"
        await settle(pilot)
        assert set(keys(table)) == {"old-1", "old-2"}
        await pilot.click("#mail-table", offset=(2, 2))
        await pilot.click("#mail-table", offset=(2, 3), shift=True)
        assert all(str(table.get_cell(key, "selected")) == "[x]" for key in keys(table))
        app.query_one("#mail-search", Input).value = "old-1"
        await settle(pilot)
        await emit("language.changed", language="zh-CN")
        await settle(pilot)
        assert keys(table) == ["old-1"]
        app.query_one("#btn-trash", Button).press()
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmModal) and bool(app.screen.query("#confirm-run")),
        )
        assert "old-1" in text(app.screen, "#confirm-body") and "old-2" in text(
            app.screen, "#confirm-body"
        )
        await pilot.press(
            "enter"
        )  # Cancel has focus; neither visible nor hidden selection is deleted.
        await settle(pilot)
        assert await service.count_mails() == 3
        app.query_one("#btn-trash", Button).press()
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmModal) and bool(app.screen.query("#confirm-run")),
        )
        app.screen.query_one("#confirm-run", Button).press()
        await settle(pilot)
        assert await service.storage.get_mail("old-1") is None
        assert await service.storage.get_mail("old-2") is not None
        assert "private storage failure" not in text(app, "#mail-operation-status")
        app.query_one("#mail-search", Input).value = ""
        await settle(pilot)
        assert keys(table) == ["old-2"] and str(table.get_cell("old-2", "selected")) == "[x]"
        failing.clear()
        app.query_one("#btn-trash", Button).press()
        await settle(pilot)
        assert await service.count_mails() == 1
        await app.query_one(MailPane).refresh_mail()
        assert "Ctrl+Z" in text(app, "#mail-operation-status")
        search = app.query_one("#mail-search", Input)
        search.focus()
        await pilot.press("ctrl+a", "x", "ctrl+z")
        assert search.value == ""
        assert await service.storage.get_mail("old-2") is None
        table.focus()
        await pilot.press("ctrl+z")
        await settle(pilot)
        assert await service.storage.get_mail("old-2") is not None
        assert await service.storage.get_mail("old-1") is None
        assert await service.storage.get_mail("keep") is not None
        # The same selection controls operate on the current task filter.
        app.query_one(TabbedContent).active = "tab-actions"
        app.query_one("#actions-range", Select).value = "expired"
        await settle(pilot)
        todos = app.query_one("#actions-table", SelectionTable)
        assert set(keys(todos)) == {item.item_id for item in actions}
        todos.focus()
        await pilot.press("ctrl+a", "escape", "space", "shift+down")
        app.query_one("#actions-delete", Button).press()
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmModal) and bool(app.screen.query("#confirm-run")),
        )
        app.screen.query_one("#confirm-run", Button).press()
        await settle(pilot)
        assert [item.item_id for item in await service.list_actions()] == [future.item_id]
        assert todos.row_count == 0


async def test_search_scope_matches_smart_tools_and_deleted_actions_stay_hidden(host: Any) -> None:
    service, view, app, emit, _failures = host
    now = datetime.now(UTC)
    action = ActionItem(
        item_id="deleted",
        mail_id="target",
        summary="Dismiss me",
        action_type="exam",
        due_at=now + timedelta(hours=1),
    )
    target = make_record("target", subject="SubjectNeedle", action=action)
    target.mail.sender = MailAddress(name="NameNeedle", address="sender@example.com")
    target.mail.body_text = "BodyNeedle"
    target.mail.attachments = [Attachment(filename="PosterNeedle.png")]
    target.mail.image_text = "OCRNeedle Research Up date Room Y908"
    assert target.analysis is not None
    target.analysis.summary = "SummaryNeedle"
    await service.storage.save_mail(target)
    await service.storage.save_mail(make_record("other", subject="unrelated"))
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(MailPane)
        table = app.query_one("#mail-table", DataTable)
        search = app.query_one("#mail-search", Input)
        for query in (
            "SubjectNeedle",
            "NameNeedle",
            "BodyNeedle",
            "SummaryNeedle",
            "PosterNeedle",
            "OCRNeedle",
            "update",
            "room y908",
        ):
            search.value = query
            await settle(pilot)
            assert keys(table) == ["target"], query
            assert "SummaryNeedle" in text(app, "#mail-summary")
            result, _ = await ToolRegistry(service).call("find_mail", {"contains": query})
            assert "id=target" in result and "id=other" not in result
        search.value = "no matching mail"
        await settle(pilot)
        assert keys(table) == [] and not text(app, "#mail-summary").strip()
        search.value = "SubjectNeedle"
        await settle(pilot)
        app.query_one("#urgency-select", Select).value = "ad"
        await settle(pilot)
        stored = await service.storage.get_mail("target")
        assert stored.manual_urgency is Urgency.AD and stored.auto_urgency is Urgency.IMPORTANT
        app.query_one("#urgency-select", Select).value = "auto"
        await settle(pilot)
        assert (await service.storage.get_mail("target")).manual_urgency is None
        assert "Dismiss me" in text(app, "#mail-actions")
        assert await service.delete_action("deleted")
        await emit("action.deleted", item_id="deleted")
        await pilot.pause()
        await pane.refresh_mail()
        await app.query_one(ActionsPane).refresh_actions()
        assert "Dismiss me" not in text(app, "#mail-actions")
        assert app.query_one("#actions-table", DataTable).row_count == 0
        assert not (await view.get_mail("target")).action_items
        assert not (await service.runtime._approaching_actions(now, 1, now=now))
        # Visibility is a view; the saved analysis remains available for reanalysis.
        assert (await service.storage.get_mail("target")).action_items == [action]


async def test_events_language_and_refresh_keep_reading_state(host: Any) -> None:
    service, _view, app, emit, _failures = host
    now = datetime.now(UTC)
    for i in range(40):
        record = make_record(f"mail-{i:02}", subject=f"Keep {i:02}")
        record.mail.received_at = now + timedelta(minutes=i)
        record.mail.body_text = "\n".join(f"Body line {j}" for j in range(150))
        await service.storage.save_mail(record)
        await service.add_action(f"Todo {i:02}", now + timedelta(days=1, minutes=i))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        mail = app.query_one(MailPane)
        actions = app.query_one(ActionsPane)
        runtime = app.query_one(RuntimePane)
        search = app.query_one("#mail-search", Input)
        search.value = "Keep"
        app.query_one("#mail-urgency-filter", Select).value = "important"
        app.query_one("#mail-sort", Select).value = "urgency"
        await settle(pilot)
        table = app.query_one("#mail-table", DataTable)
        table.move_cursor(row=30)
        await pilot.pause()
        detail = app.query_one("#mail-detail", ScrollableContainer)
        detail.scroll_to(y=18, animate=False, force=True)
        await pilot.pause()
        mail_state = cursor(table), table.scroll_y, detail.scroll_y
        assert mail_state[1] > 0 and mail_state[2] > 0
        app.query_one(TabbedContent).active = "tab-actions"
        app.query_one("#actions-search", Input).value = "Todo"
        app.query_one("#actions-range", Select).value = "week"
        app.query_one("#actions-type-filter", Select).value = "errand"
        await settle(pilot)
        action_table = app.query_one("#actions-table", DataTable)
        action_table.move_cursor(row=30)
        await pilot.pause()
        action_state = cursor(action_table), action_table.scroll_y
        assert action_state[1] > 0
        app.query_one(TabbedContent).active = "tab-runtime"
        plugin_table = app.query_one("#runtime-plugins-table", DataTable)
        plugin_table.move_cursor(row=25)
        await pilot.pause()
        plugin_state = cursor(plugin_table), plugin_table.scroll_y
        incoming = make_record("mail-new", subject="Keep incoming")
        incoming.mail.received_at = now + timedelta(days=1)
        await service.storage.save_mail(incoming)
        await emit("mailflow.mail.processed", record=incoming)
        await settle(pilot)
        assert table.row_count == 41
        service.i18n.set_language("zh-CN")
        await emit("language.changed", language="zh-CN")
        await settle(pilot)
        assert app.query_one(MailPane) is mail
        assert app.query_one(ActionsPane) is actions
        assert app.query_one(RuntimePane) is runtime
        assert (cursor(table), table.scroll_y, detail.scroll_y) == mail_state
        assert (cursor(action_table), action_table.scroll_y) == action_state
        assert (cursor(plugin_table), plugin_table.scroll_y) == plugin_state
        assert search.value == "Keep"
        assert app.query_one("#mail-urgency-filter", Select).value == "important"
        assert app.query_one("#mail-sort", Select).value == "urgency"
        assert app.query_one("#actions-search", Input).value == "Todo"
        assert app.query_one("#actions-range", Select).value == "week"
        assert app.query_one("#actions-type-filter", Select).value == "errand"
        assert str(app.query_one("#btn-refresh", Button).label) == service.t("tui.btn_refresh")
        assert app.query_one(TabbedContent).active == "tab-runtime"
        assert all(record.manual_urgency is None for record in await service.storage.list_mails())
        # A new selection wins over the previous refresh's layout callback.
        app.query_one(TabbedContent).active = "tab-mail"
        await settle(pilot)
        await mail.refresh_mail()
        table.move_cursor(row=35)
        await settle(pilot)
        assert cursor(table) == keys(table)[35]
        assert table.scroll_y > mail_state[1]


async def test_failure_details_agree_and_missing_mail_has_visible_state(host: Any) -> None:
    service, _view, app, _emit, _failures = host
    now = datetime.now(UTC)
    action = ActionItem(
        item_id="action",
        mail_id="failed",
        summary="Attend",
        action_type="meeting",
        due_at=now,
    )
    record = make_record("failed", action=action)
    assert record.analysis is not None
    record.analysis.summary_is_fallback = True
    record.processor_notes = [
        ProcessorNote(
            processor_id="llm",
            plugin_id="fake",
            status="failed",
            message="offline [retry]",
            started_at=now,
            finished_at=now,
        )
    ]
    await service.storage.save_mail(record)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        assert service.t("errors.operation") in text(app, "#mail-analysis-status")
        assert "offline [retry]" not in text(app, "#mail-analysis-status")
        assert service.t("tui.detail_summary_fallback") in text(app, "#mail-summary")
        app.push_screen(ActionModal(app._service, action))
        await pilot.pause()
        assert text(app.screen, "#action-mail-detail").startswith(
            text(app, "#mail-analysis-status")
        )
        assert service.t("tui.detail_summary_fallback") in text(app.screen, "#action-mail-detail")
        app.screen.dismiss()
        pane = app.query_one(MailPane)
        pane._selected_id = "missing"
        await pane._show_selected()
        assert service.t("mail.not_found", mail_id="missing") in text(app, "#mail-analysis-status")
        assert not text(app, "#mail-summary")


async def test_remote_errors_preserve_data_then_recover_and_only_supported_actions_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Use the same mock HTTP boundary as the parametrized local/remote tests.
    service = commands_service()
    service.config.server.username, service.config.server.password = "admin", "secret"
    record = make_record()
    await service.storage.save_mail(record)
    await service.add_action("Remote todo", datetime.now(UTC) + timedelta(days=1))
    asgi = httpx.ASGITransport(app=create_app(service))
    failures: set[str] = set()

    async def respond(req: httpx.Request) -> httpx.Response:
        if req.url.path in failures:
            return httpx.Response(503, json={"detail": "offline [retry]"})
        return await asgi.handle_async_request(req)

    mock_http_client(monkeypatch, httpx.MockTransport(respond))
    view = RemoteServiceAdapter(
        RemoteClient("http://fake", "admin", "secret"), json_value(service.snapshot()), I18n()
    )
    app = MailFlowApp(view, queue.Queue(), remote=True)
    failures.update({"/mails", "/mails/query", "/actions", "/snapshot"})
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        assert app.query_one("#mail-table", DataTable).row_count == 0
        assert app.query_one("#actions-table", DataTable).row_count == 0
        for ident in ("#mail-empty-hint", "#actions-hint", "#runtime-status"):
            assert view.t("errors.connection") in text(app, ident)
            assert "offline [retry]" not in text(app, ident)
        failures.clear()
        await app.query_one(MailPane).refresh_mail()
        await app.query_one(ActionsPane).refresh_actions()
        await app.query_one(RuntimePane).refresh_runtime()
        await settle(pilot)
        failures.update({"/mails", "/mails/query", "/actions", "/snapshot"})
        await app.query_one(MailPane).refresh_mail()
        await app.query_one(ActionsPane).refresh_actions()
        await app.query_one(RuntimePane).refresh_runtime()
        await settle(pilot)
        for ident in ("#mail-empty-hint", "#actions-hint", "#runtime-status"):
            assert view.t("errors.connection") in text(app, ident)
            assert "offline [retry]" not in text(app, ident)
        assert app.query_one("#mail-table", DataTable).row_count == 1
        assert app.query_one("#actions-table", DataTable).row_count == 1
        failures.add("/mails/m1")
        await view.client._set_connection_state("retrying", 2)  # pyright: ignore[reportPrivateUsage]
        assert view.t("tui.remote_events_retrying", seconds=2) in app.sub_title
        assert view.t("tui.remote_events_retrying", seconds=2) in text(app, "#runtime-plugins")
        # Filtering the loaded data stays usable without an HTTP connection.
        app.query_one("#mail-search", Input).value = "no match"
        app.query_one("#actions-search", Input).value = "no match"
        await settle(pilot)
        assert app.query_one("#mail-table", DataTable).row_count == 0
        assert app.query_one("#actions-table", DataTable).row_count == 0
        app.query_one("#mail-search", Input).value = "Exam"
        app.query_one("#actions-search", Input).value = "Remote"
        await settle(pilot)
        assert app.query_one("#mail-table", DataTable).row_count == 1
        assert app.query_one("#actions-table", DataTable).row_count == 1
        assert record.analysis is not None and record.analysis.summary in text(app, "#mail-summary")
        assert service.t("errors.connection") not in text(app, "#mail-analysis-status")
        app.query_one("#btn-trash", Button).press()
        await pilot.pause()
        assert view.t("errors.connection") in text(app, "#mail-operation-status")
        assert "offline [retry]" not in text(app, "#mail-operation-status")
        assert await service.storage.get_mail("m1") is not None
        failures.clear()
        await service.storage.save_mail(make_record("during-outage"))
        await view.client._set_connection_state("connected")  # pyright: ignore[reportPrivateUsage]
        await settle(pilot)
        assert view.t("tui.remote_events_connected") in app.sub_title
        assert app.query_one("#mail-table", DataTable).row_count == 2
        assert app.query_one("#mail-search", Input).value == "Exam"
        for ident in ("#mail-empty-hint", "#actions-hint", "#runtime-status"):
            assert "offline" not in text(app, ident)
        assert not app.query("#btn-reparse")
        assert app.query("#smart-action, #btn-ask-correct, #btn-profile")
        assert not app.query("#tab-mailboxes, #tab-llms, #tab-notifications, #tab-market")
        app.query_one("#btn-reply", Button).press()
        await until(
            pilot,
            lambda: (
                isinstance(app.screen, ReplyModal)
                and app.screen.query_one("#reply-body", TextArea).text == "I will attend."
            ),
        )
        assert isinstance(app.screen, ReplyModal)
        assert app.screen.query_one("#reply-body", TextArea).text == "I will attend."
        assert len(cast(MemoryStorage, service.storage).drafts) == 1
        draft_screen = app.screen
        draft_screen.query_one("#reply-body", TextArea).text = "My unfinished reply"
        service.i18n.set_language("zh-CN")
        await view.client._dispatch("language.changed", {"language": "zh-CN"})  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()
        assert app.screen is draft_screen
        assert draft_screen.query_one("#reply-body", TextArea).text == "My unfinished reply"
        draft_id = next(iter(cast(MemoryStorage, service.storage).drafts))
        failures.add(f"/drafts/{draft_id}")
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is draft_screen
        assert draft_screen.query_one("#reply-body", TextArea).text == "My unfinished reply"
        assert view.t("reply.connection_failed") in text(draft_screen, "#reply-status")
        failures.clear()
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is not draft_screen
        app.query_one("#btn-reply", Button).press()
        await settle(pilot)
        reopened = app.screen
        assert isinstance(reopened, ReplyModal)
        assert reopened.query_one("#reply-body", TextArea).text == "My unfinished reply"
        saved = await service.get_draft(draft_id)
        assert saved is not None and saved.body == "My unfinished reply"
        assert len(cast(MemoryStorage, service.storage).drafts) == 1
        # Discard is explicit, confirmed, and no longer confused with closing.
        reopened.query_one("#reply-cancel", Button).press()
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmModal) and bool(app.screen.query("#confirm-run")),
        )
        app.screen.query_one("#confirm-cancel", Button).press()
        await settle(pilot)
        assert app.screen is reopened
        reopened.query_one("#reply-cancel", Button).press()
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmModal) and bool(app.screen.query("#confirm-run")),
        )
        app.screen.query_one("#confirm-run", Button).press()
        await settle(pilot)
        app.query_one("#btn-reply", Button).press()
        await settle(pilot)
        assert isinstance(app.screen, ReplyModal)
        assert app.screen.query_one("#reply-body", TextArea).text == "I will attend."


async def test_multiselect_without_shift_and_remote_analysis_controls(host: Any) -> None:
    service, view, app, _emit, _failures = host
    for index in range(3):
        await service.storage.save_mail(make_record(f"select-{index}", subject=f"Select {index}"))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot)
        table = app.query_one("#mail-table", SelectionTable)
        table.focus()
        await pilot.press("f1")
        assert isinstance(app.screen, ShortcutHelp)
        await pilot.press("pagedown")
        app.screen.query_one("#shortcut-close", Button).press()
        await pilot.pause()
        assert table.has_focus
        await pilot.press("space", "down", "down", "f3")
        assert len(table.checked_keys) == 3
        await pilot.press("escape", "f2")
        await pilot.click(table, offset=(25, 2))
        assert len(table.checked_keys) == 1
        await pilot.click(table, offset=(25, 3))
        assert len(table.checked_keys) == 2
        await pilot.press("f4")
        assert len(table.checked_keys) == 3
        await pilot.press("escape", "f2")
        assert not table.checked_keys
        await view.set_analysis_paused(True)
        app.schedule_reload()
        await settle(pilot)
        button = app.query_one("#analysis-toggle", Button)
        assert button.display and view.snapshot().analysis_paused
        button.press()
        await settle(pilot)
        assert not service.snapshot().analysis_paused
        assert not view.snapshot().analysis_paused
