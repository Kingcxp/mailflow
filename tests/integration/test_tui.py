"""Headless TUI tests via Textual's test driver: compose, data rendering,
search filtering, urgency change, language switch, reply modal gating."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from mailflow.commands import CommandRouter
from mailflow.config import LLMConfig
from mailflow.domain import MailRecord, ReplyState, Urgency
from mailflow.plugins import PluginManager
from mailflow.service import start_service
from mailflow_storage_sqlite.plugin import plugin as storage_plugin
from mailflow_testkit.fakes import make_mail
from mailflow_tui.app import MailFlowApp
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Select,
    Static,
    TabbedContent,
    TextArea,
)

from mailflow_test_support.tui import (
    TUIPlugin,
    build_config,
    find_mail_ids,
    find_settings_status,
    set_select_value,
)
from mailflow_test_support.ui import until

# The deadline is relative: an absolute date would age into the past and be
# retired by the schedule-expiry sweep, which is not what this fixture tests.


@pytest.mark.asyncio
async def test_smart_action_keeps_real_progress_and_relevance_order(tmp_path: Path) -> None:
    """The progress line names the running tool, and the ranking the model
    asked for survives to the table (best match first)."""
    import asyncio
    import queue

    from mailflow.contracts import LLMCompletion, ToolCall
    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.app import MailPane

    class DelayedSearchRouter:
        """Turn 1 asks for a semantic search; the ranking turn is held open
        once so the test can read the in-flight progress line."""

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.held = False

        async def chat(self, messages: list[Any], **kwargs: Any) -> LLMCompletion:
            system = str(messages[0].get("content") or "")
            # the ranking call carries its own system prompt; anything else is
            # either the operator loop asking for a tool or the warm-up
            if "smart mail finder" not in system and "shortlist" not in system:
                if not any(m.get("role") == "tool" for m in messages):
                    return LLMCompletion(
                        text="",
                        model="smart",
                        tool_calls=[
                            ToolCall(
                                call_id="c1", name="find_mail", arguments={"query": "student ID"}
                            )
                        ],
                    )
                return LLMCompletion(text="Answered.", model="smart")
            self.started.set()
            if not self.held:
                self.held = True
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
            user = next(
                str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"
            )

            def candidate_for(subject: str) -> str:
                before_subject = user.split(f"subject={subject}", maxsplit=1)[0]
                return before_subject.rsplit("candidate=", maxsplit=1)[1].splitlines()[0]

            return LLMCompletion(
                text=(
                    "["
                    f'{{"id":"{candidate_for("Optional Friday lecture")}","relevance":30}},'
                    f'{{"id":"{candidate_for("Pick up your student ID card")}","relevance":95}}'
                    "]"
                ),
                model="smart",
            )

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    CommandRouter(service)
    router = DelayedSearchRouter()
    service.router = cast(Any, router)
    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test() as pilot:
            for _ in range(100):
                if await service.count_mails() == 3:
                    break
                await pilot.pause(0.05)
            pane = app.query_one(MailPane)
            search = app.query_one("#mail-search", Input)
            search.value = "the student ID invitation"
            button = app.query_one("#smart-action", Button)
            table = cast(DataTable[Any], app.query_one("#mail-table", DataTable))
            button.press()
            for _ in range(60):
                if router.started.is_set():
                    break
                await asyncio.sleep(0.05)
            assert router.started.is_set(), "smart action did not reach the ranking turn"
            await asyncio.sleep(0.2)
            hint = app.query_one("#mail-empty-hint", Static)
            # The inner scan reports the actual candidate count while it runs.
            assert "0/3" in str(hint.render())
            # Changing the request cancels its old result and keeps the new input.
            search.value = "lecture"
            for _ in range(60):
                if not pane._smart_action_running and int(table.row_count) == 1:  # pyright: ignore[reportPrivateUsage]
                    break
                await asyncio.sleep(0.05)
            assert not pane._smart_action_running  # pyright: ignore[reportPrivateUsage]
            assert search.value == "lecture"
            assert table.row_count == 1
            router.release.set()

            search.value = "the student ID invitation"
            await asyncio.sleep(0.05)
            button.press()
            for _ in range(80):
                if (
                    int(table.row_count) == 1
                    and str(button.label) == "Smart action…"
                    and pane._smart_action_result is not None  # pyright: ignore[reportPrivateUsage]
                ):
                    break
                await asyncio.sleep(0.05)
            assert int(table.row_count) == 1
            assert "Pick up your student ID card" in str(
                table.get_cell(next(iter(table.rows)), "subject")
            )
            assert pane._smart_action_result is not None  # pyright: ignore[reportPrivateUsage]
            router.held = False
            router.started.clear()
            router.release.clear()
            router.cancelled.clear()
            search.value = "another query"
            await pilot.pause()
            button.press()
            for _ in range(60):
                if router.started.is_set():
                    break
                await asyncio.sleep(0.05)
            assert router.started.is_set()
            app.exit()
            await pilot.pause()
        assert router.cancelled.is_set()
        assert pane._smart_spinner_task is None  # pyright: ignore[reportPrivateUsage]
    finally:
        await service.stop()


async def test_smart_query_task_crud_requires_confirmation(tmp_path: Path) -> None:
    """Queries create, find, edit and delete ordinary tasks through one confirmation."""
    import queue

    from mailflow.contracts import LLMCompletion, ToolCall
    from mailflow.domain import ActionOrigin
    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.smart_action_review import SmartActionReviewModal, SmartOperationEditModal

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    CommandRouter(service)

    class ActingRouter:
        mode = "add"

        async def chat(self, messages: list[Any], **kwargs: Any) -> LLMCompletion:
            tool_results = [
                str(m.get("content") or "") for m in messages if m.get("role") == "tool"
            ]
            if not tool_results:
                return LLMCompletion(
                    text="",
                    model="smart",
                    tool_calls=[
                        ToolCall(
                            call_id="c1",
                            name="find_mail" if self.mode == "add" else "list_actions",
                            arguments={"contains": "lecture"}
                            if self.mode == "add"
                            else {"query": "Project review"},
                        )
                    ],
                )
            if len(tool_results) == 1:
                if self.mode != "add":
                    item_id = next(
                        line.split()[1].removeprefix("id=")
                        for line in tool_results[0].splitlines()
                        if line.startswith("- id=")
                    )
                    return LLMCompletion(
                        text="",
                        model="smart",
                        tool_calls=[
                            ToolCall(
                                call_id="c2",
                                name="edit_action" if self.mode == "edit" else "delete_action",
                                arguments={"item_id": item_id, "clear_end": True}
                                if self.mode == "edit"
                                else {"item_id": item_id},
                            )
                        ],
                    )
                return LLMCompletion(
                    text="",
                    model="smart",
                    tool_calls=[
                        ToolCall(
                            call_id="c2",
                            name="add_action",
                            arguments={
                                "summary": "Project review",
                                "due_at": "2099-10-15T14:00:00+08:00",
                                "due_end": "2099-10-15T15:30:00+08:00",
                                "location": "Room 201",
                                "action_type": "meeting",
                            },
                        )
                    ],
                )
            return LLMCompletion(text="Task change ready for confirmation.", model="smart")

    router = ActingRouter()
    service.router = cast(Any, router)
    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(160, 50)) as pilot:
            for _ in range(100):
                if await service.count_mails() == 3:
                    break
                await pilot.pause(0.05)
            search = app.query_one("#mail-search", Input)
            search.value = "查找包含 lecture 的邮件，并在2099年10月15日14:00添加项目复核待办"

            def modal() -> SmartActionReviewModal | None:
                screens = [s for s in app.screen_stack if isinstance(s, SmartActionReviewModal)]
                return screens[-1] if screens else None

            app.query_one("#smart-action", Button).press()
            for _ in range(120):
                if modal() is not None:
                    break
                await pilot.pause(0.05)
            prompt = modal()
            assert prompt is not None, "a staged schedule entry must ask before it is added"
            # nothing exists until the user confirms
            assert await service.storage.list_custom_actions() == []
            prompt.query_one("#smart-review-apply", Button).press()
            for _ in range(120):
                if await service.storage.list_custom_actions():
                    break
                await pilot.pause(0.05)

            scheduled = await service.storage.list_custom_actions()
            assert [item.summary for item in scheduled] == ["Project review"]
            assert scheduled[0].action_type == "meeting"
            assert "2099-10-15T06:00:00+00:00" in scheduled[0].due_at.isoformat()
            assert scheduled[0].mail_id == ""  # a plain event, not a mail import
            assert scheduled[0].origin is ActionOrigin.CUSTOM
            assert scheduled[0].due_end is not None
            await until(pilot, lambda: modal() is None)

            router.mode = "edit"
            search.value = "清除 Project review 待办的结束时间"
            app.query_one("#smart-action", Button).press()
            await until(pilot, lambda: modal() is not None)
            prompt = modal()
            assert prompt is not None
            assert (await service.storage.list_custom_actions())[0].due_end is not None
            prompt.query_one("#smart-review-edit-0", Button).press()
            await until(pilot, lambda: isinstance(app.screen, SmartOperationEditModal))
            assert app.screen.query_one("#smart-review-end", Input).value == ""
            assert app.screen.query_one("#smart-review-type", Input).value == "meeting"
            app.screen.query_one("#smart-review-location", Input).value = "Office 4"
            app.screen.query_one("#smart-review-save-edit", Button).press()
            await until(pilot, lambda: isinstance(app.screen, SmartActionReviewModal))
            app.screen.query_one("#smart-review-apply", Button).press()
            await until(pilot, lambda: modal() is None)
            await pilot.pause()
            edited = (await service.storage.list_custom_actions())[0]
            assert edited.due_end is None and edited.location == "Office 4"
            assert edited.action_type == "meeting" and edited.item_id == scheduled[0].item_id

            router.mode = "delete"
            search.value = "删除 Project review 待办"
            app.query_one("#smart-action", Button).press()
            await until(pilot, lambda: modal() is not None)
            assert len(await service.storage.list_custom_actions()) == 1
            app.screen.query_one("#smart-review-apply", Button).press()
            await until(pilot, lambda: modal() is None)
            await pilot.pause()
            assert await service.storage.list_custom_actions() == []
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_smart_action_deletes_only_after_confirmation(tmp_path: Path) -> None:
    """The model stages the deletion; the mails move only once the user
    confirms — and the move is a recoverable trash move."""
    import queue

    from mailflow.contracts import LLMCompletion, ToolCall
    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.smart_action_review import SmartActionReviewModal

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    CommandRouter(service)

    class DeletingRouter:
        """Finds the promotion by keyword, then stages its deletion."""

        async def chat(self, messages: list[Any], **kwargs: Any) -> LLMCompletion:
            tool_results = [
                str(m.get("content") or "") for m in messages if m.get("role") == "tool"
            ]
            if not tool_results:
                return LLMCompletion(
                    text="",
                    model="smart",
                    tool_calls=[
                        ToolCall(
                            call_id="c1", name="find_mail", arguments={"contains": "promotion"}
                        )
                    ],
                )
            if len(tool_results) == 1:
                return LLMCompletion(
                    text="",
                    model="smart",
                    tool_calls=[
                        ToolCall(
                            call_id="c2",
                            name="delete_mail",
                            arguments={"record_ids": find_mail_ids(messages)},
                        )
                    ],
                )
            return LLMCompletion(text="Staged the promotion mail for deletion.", model="smart")

    service.router = cast(Any, DeletingRouter())
    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(160, 50)) as pilot:
            for _ in range(100):
                if await service.count_mails() == 3:
                    break
                await pilot.pause(0.05)
            search = app.query_one("#mail-search", Input)
            search.value = "把广告邮件都删掉"

            def modal() -> SmartActionReviewModal | None:
                screens = [
                    s
                    for s in app.screen_stack
                    if isinstance(s, SmartActionReviewModal)
                    and s.is_mounted
                    and s.query("#smart-review-apply")
                ]
                return screens[-1] if screens else None

            app.query_one("#smart-action", Button).press()
            for _ in range(120):
                if modal() is not None:
                    break
                await pilot.pause(0.05)
            prompt = modal()
            assert prompt is not None, "a staged delete must ask before removing mail"
            body = str(prompt.query_one("#smart-review-op-0", Static).render())
            assert "1" in body  # the real matched count, not the mailbox size

            # nothing is removed while the dialog is open
            assert await service.count_mails() == 3
            # cancelling leaves the mailbox untouched
            prompt.query_one("#smart-review-cancel", Button).press()
            for _ in range(40):
                if modal() is None:
                    break
                await pilot.pause(0.05)
            assert modal() is None
            assert await service.count_mails() == 3

            # a second run, this time confirmed
            app.query_one("#smart-action", Button).press()
            for _ in range(120):
                if modal() is not None:
                    break
                await pilot.pause(0.05)
            prompt = modal()
            assert prompt is not None
            assert "Huge promotion sale" in str(
                prompt.query_one("#smart-review-op-0", Static).render()
            )
            prompt.query_one("#smart-review-apply", Button).press()
            for _ in range(120):
                if await service.count_mails() == 2:
                    break
                await pilot.pause(0.05)
            assert await service.count_mails() == 2
            # the deleted mail is recoverable, not destroyed
            trash = await service.list_trash()
            assert [item.mail.subject for item in trash] == ["Huge promotion sale"]
            # the pane reports the real number moved (written after the
            # deferred reload, so it survives the re-render)
            hint = app.query_one("#mail-empty-hint", Static)
            for _ in range(60):
                if "1" in str(hint.render()):
                    break
                await pilot.pause(0.05)
            assert "1" in str(hint.render()), str(hint.render())
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_plugin_scaffold_wizard(tmp_path: Path) -> None:
    """The market wizard scaffolds a loadable plugin into a picked folder."""
    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.app import MarketPane
    from mailflow_tui.scaffold import PluginScaffoldScreen
    from textual.widgets import Checkbox, DirectoryTree

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    CommandRouter(service)
    import queue

    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test() as pilot:
            # open the wizard from the market tab
            from textual.widgets import TabbedContent

            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-market"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause()
            market_pane = app.query_one(MarketPane)
            cast(Button, market_pane.query_one("#market-create")).press()
            await pilot.pause(0.1)
            assert isinstance(app.screen, PluginScaffoldScreen)
            tree = app.screen.query_one("#scaffold-tree", DirectoryTree)
            tree.path = tmp_path
            await pilot.pause(0.2)
            tree.move_cursor(tree.root, animate=False)  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.05)
            # subfolder + name + plugin id + template type
            app.screen.query_one("#scaffold-subfolder", Checkbox).value = True
            await pilot.pause(0.05)
            app.screen.query_one("#scaffold-folder-name", Input).value = "mailflow-demo-wizard"
            app.screen.query_one("#scaffold-plugin-id", Input).value = "mailflow-demo-wizard"
            type_select = cast(Select[Any], app.screen.query_one("#scaffold-type", Select))
            type_select.value = "processor"
            await pilot.pause(0.05)
            # the DirectoryTree loads lazily: wait until its root node is
            # ready, otherwise Generate bails with "pick a folder"
            from textual.widgets import DirectoryTree

            tree = app.screen.query_one("#scaffold-tree", DirectoryTree)
            for _ in range(100):
                if tree.cursor_node is not None and tree.cursor_node.data is not None:
                    break
                await pilot.pause(0.05)
            app.screen.query_one("#scaffold-generate", Button).press()
            # wait for the FULL scaffold, not just the first file: on slow
            # Windows runners plugin.json can land while the worker thread
            # is still writing src/ (the window between the two asserts)
            plugin_py = (
                tmp_path / "mailflow-demo-wizard" / "src" / "mailflow_demo_wizard" / "plugin.py"
            )
            for _ in range(200):
                if plugin_py.is_file():
                    break
                await pilot.pause(0.05)
            assert (tmp_path / "mailflow-demo-wizard" / "plugin.json").is_file()
            assert (
                tmp_path / "mailflow-demo-wizard" / "src" / "mailflow_demo_wizard" / "plugin.py"
            ).is_file()
            import json as jsonlib

            metadata = jsonlib.loads(
                (tmp_path / "mailflow-demo-wizard" / "plugin.json").read_text(encoding="utf-8")
            )
            assert metadata["id"] == "mailflow-demo-wizard"
            assert metadata["categories"] == ["processor"]
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_bot_export_wizard(tmp_path: Path) -> None:
    """The market export wizard generates a NoneBot plugin into a picked folder."""
    from mailflow.plugin_market import PluginMarket
    from mailflow_export_nonebot.plugin import plugin as nonebot_export_plugin
    from mailflow_tui.app import MarketPane
    from mailflow_tui.export import BotExportScreen
    from textual.widgets import DirectoryTree, TabbedContent

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    manager.register(nonebot_export_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    service.config_path = tmp_path / "cfg.toml"
    CommandRouter(service)
    import queue

    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-market"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause()
            market_pane = app.query_one(MarketPane)
            cast(Button, market_pane.query_one("#market-export")).press()
            await pilot.pause(0.1)
            assert isinstance(app.screen, BotExportScreen)
            # the exporter plugin is registered -> the framework select defaults to nonebot
            framework_select = cast(Select[Any], app.screen.query_one("#export-framework", Select))
            for _ in range(60):
                if framework_select.value is not Select.NULL:
                    break
                await pilot.pause(0.05)
            assert framework_select.value == "nonebot"
            tree = app.screen.query_one("#export-tree", DirectoryTree)
            tree.path = tmp_path
            await pilot.pause(0.2)
            tree.move_cursor(tree.root, animate=False)  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.05)
            app.screen.query_one("#export-run", Button).press()
            for _ in range(100):
                if (tmp_path / "pyproject.toml").is_file():
                    break
                await pilot.pause(0.05)
            assert (tmp_path / "pyproject.toml").is_file()
            assert (tmp_path / "src" / "nonebot_plugin_mailflow" / "config.toml").is_file()
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_reply_letter_template_and_toolbar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reply modal applies CN/EN letter templates (auto date, right-aligned
    signature) and the toolbar wraps selections in bold/italic and aligns."""
    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.app import MailPane, ReplyModal
    from textual.widgets import TextArea
    from textual.widgets.text_area import Selection

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    CommandRouter(service)
    import queue

    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            pane = app.query_one(MailPane)
            record = next(r for r in await service.list_mails() if r.mail.message_id == "m-id")
            pane._selected_id = record.record_id  # pyright: ignore[reportPrivateUsage]
            cast(Button, app.query_one("#btn-reply")).press()
            await pilot.pause(0.1)
            assert isinstance(app.screen, ReplyModal)
            textarea = app.screen.query_one("#reply-body", TextArea)
            # apply the Chinese letter template: structure + auto date + alignment
            cast(Button, app.screen.query_one("#reply-tpl-cn")).press()
            await pilot.pause(0.05)
            body = textarea.text
            assert "尊敬的" in body
            assert "text-align:right" in body
            assert "署名：" in body
            # select some text and bold it
            textarea.selection = Selection((0, 0), (0, 4))
            await pilot.pause(0.05)
            cast(Button, app.screen.query_one("#reply-bold")).press()
            await pilot.pause(0.05)
            assert "<b>" in textarea.text
            # align the cursor line right (no selection -> current line)
            textarea.selection = Selection(textarea.cursor_location, textarea.cursor_location)
            await pilot.pause(0.05)
            cast(Button, app.screen.query_one("#reply-align-right")).press()
            await pilot.pause(0.05)
            assert "text-align:right" in textarea.text
            # saving persists the templated body
            cast(Button, app.screen.query_one("#reply-save")).press()
            await pilot.pause(0.1)
            modal = app.screen
            assert isinstance(modal, ReplyModal)
            draft = modal._draft  # pyright: ignore[reportPrivateUsage]
            assert draft is not None
            stored = await service.get_draft(draft.draft_id)
            assert stored is not None and stored.body == textarea.text
            await pilot.press("escape")
            await pilot.pause()
            app.query_one("#btn-reply", Button).press()
            await until(
                pilot,
                lambda: (
                    isinstance(app.screen, ReplyModal)
                    and app.screen.query_one("#reply-body", TextArea).text == stored.body
                ),
            )
            modal = app.screen
            assert isinstance(modal, ReplyModal)
            textarea = modal.query_one("#reply-body", TextArea)
            assert textarea.text == stored.body
            assert len(await service.storage.list_drafts(record.record_id)) == 1
            # Preparing saves the editor contents, and later edits disable confirm.
            modal.query_one("#reply-prepare", Button).press()
            await pilot.pause(0.1)
            assert not modal.query_one("#reply-confirm", Button).disabled
            textarea.text = "Updated reply from the editor"
            await pilot.pause(0.05)
            assert modal.query_one("#reply-confirm", Button).disabled
            modal.query_one("#reply-prepare", Button).press()
            await pilot.pause(0.1)
            source = service.sources[record.mail.account_id]
            original_send = source.send_reply

            async def fail_send(*_: Any) -> None:
                raise OSError("fake send failure")

            monkeypatch.setattr(source, "send_reply", fail_send)
            modal.query_one("#reply-confirm", Button).press()
            await pilot.pause(0.1)
            failed = await service.get_draft(draft.draft_id)
            assert failed is not None and failed.state is ReplyState.FAILED
            assert failed.body == textarea.text
            assert modal.query_one("#reply-confirm", Button).disabled
            assert not modal.query_one("#reply-prepare", Button).disabled
            await pilot.press("escape")
            await pilot.pause()
            app.query_one("#btn-reply", Button).press()
            await until(
                pilot,
                lambda: (
                    isinstance(app.screen, ReplyModal)
                    and app.screen.query_one("#reply-body", TextArea).text == failed.body
                ),
            )
            modal = app.screen
            assert isinstance(modal, ReplyModal)
            assert modal.query_one("#reply-body", TextArea).text == failed.body
            monkeypatch.setattr(source, "send_reply", original_send)
            modal.query_one("#reply-prepare", Button).press()
            await pilot.pause(0.1)
            modal.query_one("#reply-confirm", Button).press()
            await pilot.pause(0.1)
            sent = await service.get_draft(draft.draft_id)
            assert sent is not None and sent.state is ReplyState.SENT
            assert modal.query_one("#reply-confirm", Button).disabled
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_market_repos_screen(tmp_path: Path) -> None:
    """The Market tab manages remote repositories from a dedicated screen."""
    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.app import MarketPane
    from mailflow_tui.repos import ReposScreen
    from textual.widgets import DataTable, TabbedContent

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    service.config_path = tmp_path / "cfg.toml"
    CommandRouter(service)
    import queue

    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-market"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause()
            market_pane = app.query_one(MarketPane)
            cast(Button, market_pane.query_one("#market-repos")).press()
            await pilot.pause(0.1)
            assert isinstance(app.screen, ReposScreen)
            # add a repository through the form
            app.screen.query_one("#repos-name", Input).value = "third-party"
            app.screen.query_one("#repos-url", Input).value = "https://example.com/repo"
            cast(Button, app.screen.query_one("#repos-add")).press()
            await pilot.pause(0.1)
            table = app.screen.query_one("#repos-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
            assert table.row_count == 1  # pyright: ignore[reportUnknownMemberType]
            names = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]  # pyright: ignore[reportUnknownMemberType, reportUnknownIndexType, reportUnknownArgumentType]
            assert "third-party" in names
            # the service config now carries the repository
            assert any(repo.name == "third-party" for repo in service.config.plugins.repositories)
            # a visible Back button closes the form: escape must not be the
            # only way out (regression: the dialog had no return control)
            back = cast(Button, app.screen.query_one("#repos-cancel"))
            assert str(back.label).strip()
            assert back.variant != "default"  # never the black default variant
            back.press()
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ReposScreen)
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_market_detail_shows_author_and_updated(tmp_path: Path) -> None:
    """Selecting a plugin row opens a full-screen detail modal with the
    author, last-updated date and the markdown readme."""
    import json as jsonlib

    from mailflow.plugin_market import PluginMarket, Repository
    from mailflow_tui.app import MarketDetailScreen, MarketPane
    from textual.widgets import Markdown

    repo_root = tmp_path / "market"
    plugin_dir = repo_root / "notifier" / "mailflow-demo-notify"
    plugin_dir.mkdir(parents=True)
    (repo_root / "index.json").write_text(
        jsonlib.dumps(
            {"name": "local", "schema": 2, "categories": [{"id": "notifier", "path": "notifier"}]}
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.json").write_text(
        jsonlib.dumps(
            {
                "id": "mailflow-demo-notify",
                "name": "Demo Notify",
                "version": "1.0.0",
                "description": "demo",
                "categories": ["notifier"],
                "package": "mailflow-demo-notify",
                "source": str(plugin_dir),
                "author": "Test Author",
                "updated": "2026-08-01",
                "readme": "# Demo\n\nFull markdown body.",
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([Repository("local", repo_root.as_uri())])
    CommandRouter(service)
    import queue

    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            market_pane = app.query_one(MarketPane)
            await market_pane.refresh_market()
            market_table = cast(DataTable[Any], app.query_one("#market-table", DataTable))
            # select the row -> full-screen detail modal
            market_table.focus()
            market_table.move_cursor(row=0, animate=False)  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.1)
            market_table.action_select_cursor()  # equivalent of pressing Enter
            await pilot.pause(0.2)
            assert isinstance(app.screen, MarketDetailScreen)
            # the readme renders in a worker now: poll until it lands
            readme = app.screen.query_one("#market-detail-readme", Markdown)  # pyright: ignore[reportUnknownMemberType]
            content = ""
            for _ in range(40):
                content = str(getattr(readme, "_markdown", ""))  # pyright: ignore[reportUnknownMemberType]
                if "Full markdown body" in content:
                    break
                await pilot.pause(0.05)
            assert "Test Author" in content
            assert "2026-08-01" in content
            assert "Full markdown body" in content
            # close returns to the tabbed screen
            from textual.widgets import Button

            app.screen.query_one("#detail-close", Button).press()  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.2)
            assert not isinstance(app.screen, MarketDetailScreen)
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_settings_card_saves_resets_and_rejects(tmp_path: Path) -> None:
    """An option card saves a valid value, reports an invalid one, and can
    restore the schema default — all persisted through the service."""
    from mailflow_tui.settings import OptionCard, SettingsPane

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.config_path = tmp_path / "cfg.toml"
    CommandRouter(service)
    import queue

    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-settings"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.2)

            async def card_for(key: str) -> OptionCard:
                # saving triggers reload(), which swaps the card widgets
                # asynchronously — poll until the requested card is mounted
                for _ in range(40):
                    match = next((c for c in app.query(OptionCard) if c.spec.key == key), None)
                    if match is not None:
                        await pilot.pause(0.1)
                        return match
                    await pilot.pause(0.05)
                raise AssertionError(f"option card {key!r} never rendered")

            card = await card_for("general.timezone")
            # the description is localized, never the raw lookup key
            descriptions = [str(node.render()) for node in card.query(Static)]
            assert not any("config.desc." in text for text in descriptions)

            # drive saves through the pane handlers directly: awaiting them
            # also awaits the reload that swaps the card widgets, so the
            # next lookup can never race a mid-swap tree (button-press
            # timing is covered by the other settings tests)
            settings_pane = cast(Any, app.query_one(SettingsPane))
            await settings_pane._save("general.timezone", "Asia/Shanghai")  # pyright: ignore[reportPrivateUsage]
            await pilot.pause(0.1)
            assert service.config.general.timezone == "Asia/Shanghai"

            # an invalid value is rejected with a message naming the option
            await settings_pane._save("general.cleanup_hour", "99")  # pyright: ignore[reportPrivateUsage]
            assert service.config.general.cleanup_hour == 4  # unchanged
            status_node = find_settings_status(app)
            status = str(status_node.render()) if status_node is not None else ""
            assert "cleanup_hour" in status

            # restore-default puts the schema default back
            card = await card_for("general.timezone")
            await settings_pane._reset("general.timezone")  # pyright: ignore[reportPrivateUsage]
            assert service.config.general.timezone == "UTC"
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_llm_tab_orders_the_fallback_chain(tmp_path: Path) -> None:
    """The LLM tab's order *is* the chain: first entry default, rest fallbacks."""
    from mailflow.plugin_market import PluginMarket

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    config = build_config(tmp_path / "tui.db")
    config.llms.append(
        LLMConfig(llm_id="llm2", provider="test-llm", model="m2")  # second, so a fallback
    )
    service = await start_service(
        config, plugin_manager=manager, discover_plugins=False, enable_logging=False
    )
    service.market = PluginMarket([])
    service.config_path = tmp_path / "cfg.toml"
    CommandRouter(service)
    import queue

    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-llms"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.2)
            table = cast(DataTable[Any], app.query_one("#llms-table", DataTable))
            assert table.row_count == 2
            assert [str(table.get_row_at(i)[1]) for i in range(2)] == ["llm1", "llm2"]

            # moving the second entry up makes it the default; the chain follows
            from mailflow_tui.settings import LLMPane

            pane = app.query_one(LLMPane)
            pane._selected = 1  # pyright: ignore[reportPrivateUsage]
            app.query_one("#llm-up", Button).press()
            await pilot.pause(0.4)
            assert [llm.llm_id for llm in service.config.llms] == ["llm2", "llm1"]
            assert service.config.llms[0].default is True
            assert service.config.llms[0].fallback == ["llm1"]
            assert service.config.llms[1].default is False
            assert service.config.llms[1].fallback == []
            assert service.config.default_llm() is not None
            assert service.config.default_llm().llm_id == "llm2"  # pyright: ignore[reportOptionalMemberAccess]
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_mailbox_history_analyzes_selected_mail(tmp_path: Path) -> None:
    """The mailbox tab lists already-received mail and only the picked ones
    are pushed through the pipeline."""
    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.settings import AccountsPane

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    service.config_path = tmp_path / "cfg.toml"
    CommandRouter(service)
    import queue

    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            # let the live stream settle first so the dedup path is realistic
            for _ in range(100):
                if await service.count_mails() == 3:
                    break
                await pilot.pause(0.05)
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-mailboxes"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.2)
            accounts = cast(DataTable[Any], app.query_one("#accounts-table", DataTable))
            assert accounts.row_count == 1

            pane = app.query_one(AccountsPane)
            pane._selected = 0  # pyright: ignore[reportPrivateUsage]
            app.query_one("#account-history", Button).press()
            await pilot.pause(0.5)
            history = cast(DataTable[Any], app.query_one("#history-table", DataTable))
            assert history.row_count == 3
            # every one of them is already stored, so the browser marks them
            states = {str(history.get_row_at(i)[4]) for i in range(history.row_count)}
            assert states == {service.t("tui.history_marked_known")}

            # analyzing an already-known mail is a no-op, never a duplicate
            known_id = str(next(iter(history.rows)).value)
            pane._picked = {known_id}  # pyright: ignore[reportPrivateUsage]
            app.query_one("#history-analyze", Button).press()
            await pilot.pause(0.5)
            assert await service.count_mails() == 3

            # a mail that never came through the live stream *is* analyzed
            from mailflow_testkit.fakes import make_mail as make_test_mail

            unseen = make_test_mail(
                message_id="m-archived",
                account_id="acct-1",
                subject="Optional Friday lecture from the archive",
                body_text="The guest lecture is optional.",
            )
            pane._history = [*pane._history, unseen]  # pyright: ignore[reportPrivateUsage]
            pane._picked = {unseen.normalized_message_id()}  # pyright: ignore[reportPrivateUsage]
            app.query_one("#history-analyze", Button).press()
            await pilot.pause(0.6)
            assert await service.count_mails() == 4
            stored = await service.get_mail(unseen.normalized_message_id())
            assert stored is not None
            assert stored.summary, "on-demand analysis must keep the summary guarantee"
            assert stored.analysis is not None  # the pipeline really ran
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_notifications_pane_lists_all_notifiers_and_toggles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Notifications tab shows every notifier (not just IM providers),
    toggles the selected one enabled/disabled in place, and edits urgency."""
    from mailflow_tui.notifications import NotificationsPane

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    # add a gateway-backed (onebot) and a plain (console) notifier so the
    # pane really lists more than the IM providers
    service.config.notifiers.append(
        __import__("mailflow.config", fromlist=["NotifierConfig"]).NotifierConfig(
            notifier_id="qq-1",
            provider="onebot",
            enabled=True,
            options={"http_url": "http://127.0.0.1:1", "targets": ["group:123"]},
        )
    )
    service.config.notifiers.append(
        __import__("mailflow.config", fromlist=["NotifierConfig"]).NotifierConfig(
            notifier_id="console-2",
            provider="console",
            enabled=True,
        )
    )
    service.config_path = tmp_path / "cfg.toml"
    CommandRouter(service)
    import queue

    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.2)
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-notifications"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.2)
            pane = app.query_one(NotificationsPane)
            table = cast(DataTable[Any], pane.query_one("#notifications-table", DataTable))
            assert table.row_count == 3  # console + onebot
            names = {str(table.get_row_at(i)[0]) for i in range(table.row_count)}
            assert "qq-1" in names
            assert "console-2" in names
            # select the onebot row and toggle it off
            qq_row = next(
                i for i in range(table.row_count) if str(table.get_row_at(i)[0]) == "qq-1"
            )
            table.move_cursor(row=qq_row, animate=False)  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.05)
            cast(Button, pane.query_one("#notif-toggle")).press()
            await pilot.pause(0.2)
            qq = next(n for n in service.config.notifiers if n.notifier_id == "qq-1")
            assert qq.enabled is False
            # edit urgency via the dropdown
            urgency = cast(Select[Any], pane.query_one("#notif-urgency", Select))
            await set_select_value(pilot, urgency, "urgent")
            await pilot.pause(0.2)
            qq = next(n for n in service.config.notifiers if n.notifier_id == "qq-1")
            assert qq.minimum_urgency is Urgency.URGENT
            # Backend details are safely displayed in the active language:
            # do not expose a notifier credential or interpret its markup.
            qq.options["access_token"] = "notify-token-secret"
            await service.set_language("zh-CN")

            async def reject_update(*args: Any, **kwargs: Any) -> None:
                raise ValueError("save [markup] failed: notify-token-secret")

            monkeypatch.setattr(service, "update_config_entry", reject_update)
            await pane._toggle_selected()  # pyright: ignore[reportPrivateUsage]
            status = str(pane.query_one("#notifications-status", Static).render())
            assert "错误" in status
            assert "notify-token-secret" not in status
            assert service.t("errors.invalid_value") in status
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_mail_bulk_buttons_purge_reanalyze_and_save_profile(tmp_path: Path) -> None:
    """The Mail tab's third button row: clear expired mail (recoverable),
    re-analyze everything, and save the preferences the LLM reads."""
    import queue

    from mailflow.domain import ActionItem, MailAnalysis
    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.app import MailPane
    from mailflow_tui.confirm import ConfirmModal
    from mailflow_tui.profile import UserProfileModal

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    CommandRouter(service)
    now = datetime.now(UTC)
    stale = make_mail(
        message_id="stale-ad", account_id="acct-1", subject="Old promotion", body_text="sale"
    ).model_copy(update={"received_at": now - timedelta(days=3)})
    await service.storage.save_mail(
        MailRecord(
            record_id="stale-ad",
            mail=stale,
            auto_urgency=Urgency.AD,
            analysis=MailAnalysis(summary="promotion", urgency=Urgency.AD),
        )
    )
    live = make_mail(
        message_id="live-info", account_id="acct-1", subject="Lab talk", body_text="attend"
    ).model_copy(update={"received_at": now})
    await service.storage.save_mail(
        MailRecord(
            record_id="live-info",
            mail=live,
            auto_urgency=Urgency.INFO,
            analysis=MailAnalysis(
                summary="Lab talk",
                urgency=Urgency.INFO,
                action_items=[
                    ActionItem(
                        item_id="live-info-1",
                        mail_id="live-info",
                        summary="Attend the talk",
                        action_type="meeting",
                        due_at=now + timedelta(days=2),
                    )
                ],
            ),
        )
    )
    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(160, 50)) as pilot:
            for _ in range(200):
                if await service.count_mails() == 2:
                    break
                await pilot.pause(0.05)
            pane = app.query_one(MailPane)
            assert pane.query("#btn-purge-expired") and pane.query("#btn-reparse-all")
            assert pane.query("#btn-profile")

            # 1. clearing expired mail asks first, then moves exactly the
            #    expired mail to the trash
            expired_ids = {record.record_id for record in await service.list_expired_mails()}
            assert "stale-ad" in expired_ids
            assert "live-info" not in expired_ids  # a future talk still matters
            app.query_one("#btn-purge-expired", Button).press()
            purge_dialog: ConfirmModal | None = None
            for _ in range(80):
                current = app.screen
                if isinstance(current, ConfirmModal):
                    purge_dialog = current
                    break
                await pilot.pause(0.05)
            assert purge_dialog is not None, "clear-expired must ask before deleting"
            body = str(purge_dialog.query_one("#confirm-body", Static).render())
            assert f"{len(expired_ids)} mail(s)" in body
            purge_dialog.query_one("#confirm-run", Button).press()
            for _ in range(120):
                remaining = {record.record_id for record in await service.list_mails()}
                if not (remaining & expired_ids):
                    break
                await pilot.pause(0.05)
            remaining = {record.record_id for record in await service.list_mails()}
            assert "stale-ad" not in remaining and "live-info" in remaining
            # recoverable: the purged mail is in the trash and comes back
            trashed = {entry.record_id for entry in await service.list_trash()}
            assert "stale-ad" in trashed
            restored = await service.restore_mail("stale-ad")
            assert restored is not None and restored.record_id == "stale-ad"
            await service.storage.delete_mail("stale-ad")
            await pilot.pause(0.2)

            # 2. re-analyzing everything states the real count first
            app.query_one("#btn-reparse-all", Button).press()
            reanalyze_dialog: ConfirmModal | None = None
            for _ in range(80):
                current = app.screen
                if isinstance(current, ConfirmModal):
                    reanalyze_dialog = current
                    break
                await pilot.pause(0.05)
            assert reanalyze_dialog is not None, "re-analyze-all must state the count first"
            body = str(reanalyze_dialog.query_one("#confirm-body", Static).render())
            assert f"{await service.count_mails()} stored mail(s)" in body
            reanalyze_dialog.query_one("#confirm-cancel", Button).press()
            dismissed = False
            for _ in range(80):
                if not isinstance(app.screen, ConfirmModal):
                    dismissed = True
                    break
                await pilot.pause(0.05)
            assert dismissed, "cancel must close the dialog without running anything"

            # a second dismissal (button then Escape) must not pop the whole
            # screen stack (that used to end the session with ScreenStackError)
            assert await service.restore_mail("stale-ad") is not None
            app.query_one("#btn-purge-expired", Button).press()
            double_dialog: ConfirmModal | None = None
            for _ in range(80):
                current = app.screen
                if isinstance(current, ConfirmModal):
                    double_dialog = current
                    break
                await pilot.pause(0.05)
            assert double_dialog is not None
            double_dialog.query_one("#confirm-cancel", Button).press()
            double_dialog.action_cancel()  # Escape on the already-dismissed dialog
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ConfirmModal)
            assert app.is_running

            # 3. the preferences form saves what the model will read
            app.query_one("#btn-profile", Button).press()
            profile_dialog: UserProfileModal | None = None
            for _ in range(80):
                current = app.screen
                if isinstance(current, UserProfileModal):
                    profile_dialog = current
                    break
                await pilot.pause(0.05)
            assert profile_dialog is not None
            profile_dialog.query_one("#profile-text", TextArea).text = "I am a CS master's student"
            profile_dialog.query_one("#profile-save", Button).press()
            for _ in range(80):
                if await service.user_profile():
                    break
                await pilot.pause(0.05)
            assert await service.user_profile() == "I am a CS master's student"
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_reparse_all_can_be_stopped_while_running(tmp_path: Path) -> None:
    """The bulk button runs the job and, while it runs, ends it.

    A full re-analysis of a real mailbox takes a long time on a local model;
    being unable to stop it forced killing the app.
    """
    import asyncio
    import queue

    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.app import MailPane
    from mailflow_tui.confirm import ConfirmModal

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    CommandRouter(service)

    gate = asyncio.Event()
    started = asyncio.Event()
    processed: list[str] = []
    original = service.process_mail

    async def slow_process(mail: Any, *, force: bool = False) -> Any:
        started.set()
        await gate.wait()
        processed.append(mail.message_id)
        return await original(mail, force=force)

    service.process_mail = slow_process  # type: ignore[method-assign]
    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            for _ in range(200):
                if await service.count_mails() == 3:
                    break
                await pilot.pause(0.05)
            pane = app.query_one(MailPane)
            button = app.query_one("#btn-reparse-all", Button)

            button.press()
            dialog: ConfirmModal | None = None
            for _ in range(80):
                current = app.screen
                if isinstance(current, ConfirmModal):
                    dialog = current
                    break
                await pilot.pause(0.05)
            assert dialog is not None
            dialog.query_one("#confirm-run", Button).press()
            for _ in range(80):
                if started.is_set():
                    break
                await pilot.pause(0.05)
            assert started.is_set(), "the bulk run must start"
            # the same button is the stop control while the job is in flight
            for _ in range(40):
                if "Stop" in str(button.label):
                    break
                await pilot.pause(0.05)
            assert "Stop" in str(button.label), f"label stayed {button.label!r}"

            button.press()  # stop
            await pilot.pause(0.1)
            gate.set()
            for _ in range(120):
                if "Re-analyze" in str(button.label):
                    break
                await pilot.pause(0.05)
            assert "Re-analyze" in str(button.label), "the button must return to its run label"
            # stopping cancels the worker, so it no longer waits for the call in
            # flight to reach its own timeout
            assert len(processed) <= 1, f"cancel must not keep analyzing, got {processed}"
            status = str(pane.query_one("#mail-operation-status", Static).render())
            assert "stopped" in status.lower()
            assert "of 3" in status
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_bulk_reparse_stop_covers_reanalyze_failed_and_never_latches(
    tmp_path: Path,
) -> None:
    """Both long bulk buttons act as their own stop control, and cancelling the
    confirmation must not leave re-analysis impossible afterwards."""
    import asyncio
    import queue

    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.app import MailPane
    from mailflow_tui.confirm import ConfirmModal

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    CommandRouter(service)

    gate = asyncio.Event()
    started = asyncio.Event()
    processed: list[str] = []
    original = service.process_mail

    async def slow_process(mail: Any, *, force: bool = False) -> Any:
        started.set()
        await gate.wait()
        processed.append(mail.message_id)
        return await original(mail, force=force)

    records = await service.list_mails()
    service.list_failed_mails = lambda: asyncio.sleep(0, result=list(records))  # type: ignore[method-assign]
    service.process_mail = slow_process  # type: ignore[method-assign]
    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            for _ in range(200):
                if await service.count_mails() == 3:
                    break
                await pilot.pause(0.05)
            pane = app.query_one(MailPane)
            all_button = app.query_one("#btn-reparse-all", Button)
            failed_button = app.query_one("#btn-reparse-failed", Button)

            # 1. cancelling the confirmation leaves the button usable
            all_button.press()
            dialog: ConfirmModal | None = None
            for _ in range(80):
                current = app.screen
                if isinstance(current, ConfirmModal):
                    dialog = current
                    break
                await pilot.pause(0.05)
            assert dialog is not None
            dialog.query_one("#confirm-cancel", Button).press()
            for _ in range(80):
                if not isinstance(app.screen, ConfirmModal):
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.1)
            assert not all_button.disabled, "a cancelled run must leave the button usable"

            # 2. the single-mail button is a run/stop control too
            app.query_one("#btn-reparse", Button).press()
            for _ in range(80):
                if started.is_set():
                    break
                await pilot.pause(0.05)
            assert started.is_set(), "the single-mail re-analyze must start"
            for _ in range(40):
                if "Stop" in str(app.query_one("#btn-reparse", Button).label):
                    break
                await pilot.pause(0.05)
            single = app.query_one("#btn-reparse", Button)
            assert "Stop" in str(single.label), f"label stayed {single.label!r}"
            assert failed_button.disabled and all_button.disabled
            single.press()  # cancel: must end at once, not after the call's timeout
            for _ in range(60):
                if "Re-analyze" in str(single.label):
                    break
                await pilot.pause(0.05)
            assert "Re-analyze" in str(single.label), "cancel must restore the button promptly"
            assert not failed_button.disabled and not all_button.disabled
            status = str(pane.query_one("#mail-operation-status", Static).render())
            assert "stopped" in status.lower()
            started.clear()

            # 3. the failed-mails button is a run/stop control too
            gate = asyncio.Event()
            failed_button.press()
            for _ in range(80):
                if started.is_set():
                    break
                await pilot.pause(0.05)
            assert started.is_set(), "re-analyze-failed must start"
            for _ in range(40):
                if "Stop" in str(failed_button.label):
                    break
                await pilot.pause(0.05)
            assert "Stop" in str(failed_button.label), f"label stayed {failed_button.label!r}"
            assert all_button.disabled, "the other bulk button parks while a run is in flight"

            failed_button.press()  # stop: cancels the worker, so it ends at once
            for _ in range(120):
                if "Re-analyze" in str(failed_button.label):
                    break
                await pilot.pause(0.05)
            assert "Re-analyze" in str(failed_button.label)
            assert not all_button.disabled, "the parked button must come back"

            # 4. and a new run can still start afterwards
            gate.set()
            started.clear()
            failed_button.press()
            for _ in range(80):
                if isinstance(app.screen, ConfirmModal):
                    app.screen.query_one("#confirm-cancel", Button).press()
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.1)
            assert not failed_button.disabled
            await cast(Any, app.workers).wait_for_complete()
            table = cast(DataTable[Any], app.query_one("#mail-table", DataTable))
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("space", "down", "space")
            expected: set[str] = set()
            for key in list(table.rows)[:2]:
                selected = await service.get_mail(str(key.value))
                assert selected is not None
                expected.add(selected.mail.message_id)
            processed.clear()
            app.query_one("#btn-reparse", Button).press()
            for _ in range(80):
                if isinstance(app.screen, ConfirmModal):
                    app.screen.query_one("#confirm-run", Button).press()
                    break
                await pilot.pause(0.05)
            await cast(Any, app.workers).wait_for_complete()
            assert set(processed) == expected, "only the two checked messages are re-analyzed"
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_llm_move_up_follows_the_entry_and_repeats(tmp_path: Path) -> None:
    """Moving an entry up keeps the cursor on it so the move can be repeated."""
    import queue

    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.settings import LLMPane
    from textual.widgets import DataTable

    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        config_path=tmp_path / "cfg.toml",  # edits persist, like the real runner
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    service.market = PluginMarket([])
    CommandRouter(service)
    service.config.llms = [
        service.config.llms[0].model_copy(update={"llm_id": f"llm-{i}", "provider": "test-llm"})
        for i in range(3)
    ]
    service.config.processors = [
        p.model_copy(update={"llm": "llm-0", "fallback_llms": ["llm-1", "llm-2"]})
        if p.provider == "llm-importance"
        else p
        for p in service.config.processors
    ]
    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-llms"  # pyright: ignore[reportUnknownMemberType]
            for _ in range(60):
                if app.query(LLMPane):
                    break
                await pilot.pause(0.05)
            pane = app.query_one(LLMPane)
            await pilot.pause(0.3)
            table = cast(DataTable[Any], pane.query_one("#llms-table", DataTable))

            # select the third entry (index 2) and move it up twice
            table.move_cursor(row=2, animate=False)  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.1)
            assert pane._selected == 2  # pyright: ignore[reportPrivateUsage]

            app.query_one("#llm-up", Button).press()
            await pilot.pause(0.4)
            assert [llm.llm_id for llm in service.config.llms] == ["llm-0", "llm-2", "llm-1"]
            assert pane._selected == 1, "the cursor must follow the moved entry"  # pyright: ignore[reportPrivateUsage]

            app.query_one("#llm-up", Button).press()
            await pilot.pause(0.4)
            assert [llm.llm_id for llm in service.config.llms] == ["llm-2", "llm-0", "llm-1"]
            assert pane._selected == 0  # pyright: ignore[reportPrivateUsage]

            # at the top the move is a no-op with an explanation, not a jump
            app.query_one("#llm-up", Button).press()
            await pilot.pause(0.3)
            assert [llm.llm_id for llm in service.config.llms] == ["llm-2", "llm-0", "llm-1"]
            assert pane._selected == 0  # pyright: ignore[reportPrivateUsage]
            status = str(pane.query_one("#llms-status", Static).render())
            assert "default" in status.lower() or "首选" in status
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()
