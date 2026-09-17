"""Headless TUI tests via Textual's test driver: compose, data rendering,
search filtering, urgency change, language switch, reply modal gating."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from mailflow.commands import CommandRouter
from mailflow.config import LLMConfig, MailAccountConfig, MailFlowConfig
from mailflow.contracts import MailSource
from mailflow.domain import MailRecord, Urgency
from mailflow.plugins import PluginInfo, PluginManager
from mailflow.registry import PluginRegistrar
from mailflow.service import start_service
from mailflow_storage_sqlite.plugin import plugin as storage_plugin
from mailflow_testkit.fakes import FakeMailSource, make_mail
from mailflow_tui.app import MailFlowApp
from textual.css.query import NoMatches
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Select,
    Static,
    TabbedContent,
    TextArea,
)

AD_JSON = """{
  "summary": "Special promotion inside",
  "urgency": "ad",
  "reason": "",
  "reply_required": false,
  "suggested_reply": "",
  "action_items": [],
  "notes": ""
}"""

INFO_JSON = """{
  "summary": "Lecture on Friday is optional",
  "urgency": "info",
  "reason": "",
  "reply_required": false,
  "suggested_reply": "",
  "action_items": [],
  "notes": ""
}"""

URGENT_JSON = """{
  "summary": "Pick up student ID card today",
  "urgency": "urgent",
  "reason": "must be collected by 17:00",
  "reply_required": true,
  "suggested_reply": "I will come before 17:00.",
  "action_items": [
    {"summary": "Collect student ID", "action_type": "errand",
     "due_at": "2026-06-10T17:00:00+00:00", "due_end": null,
     "notes": "Bring your own ID photo"}
  ],
  "notes": ""
}"""


class MapLLM:
    """Returns canned JSON depending on the mail subject."""

    backend_id = "test-llm"

    def __init__(self, llm_config: LLMConfig) -> None:
        self.llm_config = llm_config

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any):
        from mailflow.contracts import LLMCompletion

        subject = next((m["content"] for m in messages if m["role"] == "user"), "")
        if "promotion" in subject:
            text = AD_JSON
        elif "ID card" in subject:
            text = URGENT_JSON
        else:
            text = INFO_JSON
        return LLMCompletion(text=text, model="m1")


class TUIPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PluginInfo(
            plugin_id="mailflow-tui-test",
            name="TUI Test Components",
            version="0.0.0",
        )

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        def source_factory(account: MailAccountConfig) -> MailSource:
            return FakeMailSource(
                [
                    make_mail(
                        message_id="m-ad",
                        account_id=account.account_id,
                        subject="Huge promotion sale",
                        body_text="unsubscribe now",
                    ),
                    make_mail(
                        message_id="m-id",
                        account_id=account.account_id,
                        subject="Pick up your student ID card",
                        body_text="Please collect your student ID card at the office before 17:00.",
                    ),
                    make_mail(
                        message_id="m-info",
                        account_id=account.account_id,
                        subject="Optional Friday lecture",
                        body_text="The guest lecture is optional.",
                    ),
                ]
            )

        registrar.add_source("test-source", source_factory)
        registrar.add_llm("test-llm", MapLLM)


def build_config(db_path: Path) -> MailFlowConfig:
    return MailFlowConfig.model_validate(
        {
            "general": {"timezone": "Asia/Shanghai", "workers": 2},
            "storage": {"provider": "sqlite", "path": str(db_path)},
            "plugins": {"repositories": []},
            "accounts": [
                {"account_id": "acct-1", "provider": "test-source", "email": "me@example.com"}
            ],
            "llms": [
                {
                    "llm_id": "llm1",
                    "provider": "test-llm",
                    "model": "m1",
                    "api_key": "sk-tui-secret",
                }
            ],
            "processors": [
                {"processor_id": "rules", "provider": "rules", "priority": 10},
                {
                    "processor_id": "llm-importance",
                    "provider": "llm-importance",
                    "priority": 20,
                    "llm": "llm1",
                },
            ],
            "notifiers": [
                {"notifier_id": "console", "provider": "console", "minimum_urgency": "important"}
            ],
            "logging": {"console": False, "file": False, "jsonl": False},
        }
    )


@pytest.mark.asyncio
def _find_status(app: Any) -> Any:
    try:
        return app.query_one("#settings-status", Static)
    except Exception:
        return None


async def set_select_value(pilot: Any, select: Any, value: str) -> None:
    """Assign a Select value, retrying once around the Textual render race
    where a programmatic assignment can hit the widget before its internal
    label node exists (intermittent '#label' NoMatches on Python 3.12)."""
    from textual.css.query import QueryError

    for attempt in range(10):
        try:
            select.value = value
            await pilot.pause(0.15)
            return
        except QueryError:
            if attempt == 9:
                raise
            await pilot.pause(0.2)


async def test_tui_compose_and_data(tmp_path: Path) -> None:
    # a local marketplace (per-plugin folder layout)
    import json as jsonlib

    (tmp_path / "notifier" / "mailflow-test-market-plugin").mkdir(parents=True)
    (tmp_path / "index.json").write_text(
        jsonlib.dumps(
            {"name": "local", "schema": 2, "categories": [{"id": "notifier", "path": "notifier"}]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "notifier" / "mailflow-test-market-plugin" / "plugin.json").write_text(
        jsonlib.dumps(
            {
                "id": "mailflow-test-market-plugin",
                "name": "Market Test",
                "version": "9.9.9",
                "description": "browsable from the tui",
                "categories": ["notifier"],
                "package": "mailflow-test-market-plugin",
                "source": "",
                "readme": "# Market Test\n\nLong markdown readme.",
            }
        ),
        encoding="utf-8",
    )
    index_path = tmp_path
    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    from mailflow_notify_console.plugin import plugin as notify_plugin

    manager.register(notify_plugin)
    service = await start_service(
        build_config(tmp_path / "tui.db"),
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    from mailflow.config import PluginRepositoryConfig
    from mailflow.plugin_market import PluginMarket, Repository

    service.config.plugins.repositories.append(
        PluginRepositoryConfig(name="local", url=index_path.as_uri())
    )
    service.market = PluginMarket([Repository("local", index_path.as_uri())])
    CommandRouter(service)
    import queue

    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test() as pilot:
            # wait for the pipeline to process the three mails
            for _ in range(100):
                if await service.count_mails() == 3:
                    break
                await pilot.pause(0.05)
            await pilot.pause()
            from mailflow_tui.app import ActionsPane, MailPane

            await app.query_one(MailPane).refresh_mail()

            table = cast(DataTable[Any], app.query_one("#mail-table", DataTable))
            assert table.row_count == 3
            actions_pane = app.query_one(ActionsPane)
            await actions_pane.refresh_actions()
            actions_table = cast(DataTable[Any], app.query_one("#actions-table", DataTable))
            assert actions_table.row_count == 1
            assert "2026-06-11 01:00" in str(actions_table.get_row_at(0)[0])
            # A results-only render (Smart find's final step) must replace
            # the detail too, and an empty result must not leave a stale
            # previously selected mail beside the empty table.
            pane = app.query_one(MailPane)
            old_record = await service.get_mail("m-info")
            result_record = await service.get_mail("m-id")
            assert old_record is not None and result_record is not None
            pane._selected_id = old_record.record_id  # pyright: ignore[reportPrivateUsage]
            await pane._show_selected()  # pyright: ignore[reportPrivateUsage]
            assert "Lecture on Friday" in str(app.query_one("#mail-summary", Static).render())
            await pane._render_records([result_record])  # pyright: ignore[reportPrivateUsage]
            assert "Pick up student ID" in str(app.query_one("#mail-summary", Static).render())
            await pane._render_records([])  # pyright: ignore[reportPrivateUsage]
            assert str(app.query_one("#mail-summary", Static).render()) == ""
            await pane.refresh_mail()
            # search filters the table
            search = app.query_one("#mail-search", Input)
            search.value = "promotion"
            await pilot.pause(0.05)
            assert table.row_count == 1

            # search matches the BODY too: "before 17:00" only appears in
            # the ID-card mail's body_text, never in its subject/summary
            search.value = "before 17:00"
            await pilot.pause(0.05)
            assert table.row_count == 1
            assert "student ID" in " ".join(str(cell) for cell in table.get_row_at(0))

            search.value = ""
            await pilot.pause(0.05)
            assert table.row_count == 3
            # select the urgent mail row: its urgency cell carries the contract color
            urgent_index = next(
                (
                    i
                    for i in range(table.row_count)
                    if "urgent" in str(table.get_row_at(i)[0]).lower()
                ),
                -1,
            )
            assert urgent_index >= 0, "no urgent row rendered"
            urgent_cell_text = str(table.get_row_at(urgent_index)[0])
            assert "■" in urgent_cell_text
            assert "urgent" in urgent_cell_text.lower()

            # select the urgent mail, then the urgency dropdown drives the mutation
            pane = app.query_one(MailPane)
            pane._selected_id = "m-id"  # pyright: ignore[reportPrivateUsage]
            select = cast(Select[Any], app.query_one("#urgency-select", Select))
            await set_select_value(pilot, select, "ad")
            record: MailRecord | None = await service.get_mail("m-id")
            assert record is not None
            assert record.manual_urgency is Urgency.AD
            # language switch persists — driven through the general.language
            # option card (the standalone selector row was removed)
            from mailflow_tui.settings import SettingsPane

            settings_pane = app.query_one(SettingsPane)
            await settings_pane.reload()
            await pilot.pause(0.15)
            from mailflow_tui.settings import OptionCard

            # the language-switch remount can swap the cards between reload
            # and scan: poll for the card instead of a bare next() (a
            # StopIteration inside a coroutine surfaces as RuntimeError)
            lang_card = None
            for _ in range(40):
                lang_card = next(
                    (card for card in app.query(OptionCard) if card.spec.key == "general.language"),
                    None,
                )
                if lang_card is not None:
                    break
                await pilot.pause(0.05)
            assert lang_card is not None, "language option card never rendered"
            lang_select = cast(Select[Any], lang_card.query_one(Select))
            await set_select_value(pilot, lang_select, "zh-CN")
            await settings_pane._save("general.language", "zh-CN")  # pyright: ignore[reportPrivateUsage]
            await pilot.pause(0.05)
            from textual.widgets import TabbedContent

            tabs = app.query_one(TabbedContent)
            tab_label = tabs.get_tab("tab-mail")  # pyright: ignore[reportUnknownMemberType]
            assert str(tab_label.label) == "邮件"  # pyright: ignore[reportUnknownMemberType]
            # settings tab: sidebar sections plus one card per option.
            # The language switch remounts the pane concurrently — re-query
            # and poll instead of racing the remount.
            from mailflow_tui.settings import OptionCard, SettingsPane

            sections = None
            for _ in range(40):
                panes = app.query(SettingsPane)
                listings = app.query("#settings-sections")
                if panes and listings:
                    await panes.first().reload()
                    candidate = listings.first()
                    cards_now = list(app.query(OptionCard))
                    # only proceed once the language card exists: reloading
                    # again after interaction starts would swap the Select
                    # out from under the overlay
                    if len(list(candidate.children)) >= 5 and any(
                        card.spec.key == "general.language" for card in cards_now
                    ):  # general/logging/plugins/storage/i18n
                        sections = candidate
                        break
                await pilot.pause(0.05)
            assert sections is not None and len(sections.children) >= 5
            # reload() swaps cards asynchronously under a lock — poll instead
            # of racing it
            cards = []
            keys: set[str] = set()
            for _ in range(40):
                cards = list(app.query(OptionCard))
                keys = {card.spec.key for card in cards}
                if "general.reminder_hour" in keys:
                    break
                await pilot.pause(0.05)
            assert "general.reminder_hour" in keys, f"cards rendered: {sorted(keys)[:6]}"
            # searching filters across every section — poll until the filter
            # has actually been applied. A late remount resets the search
            # input, so re-type it whenever the box goes empty.
            filtered: set[str] = set()
            for _ in range(120):
                # re-query every iteration: a late remount replaces the pane
                # and its input, detaching the previously captured widget.
                # The settings pane itself may not be mounted yet on a slow
                # runner — skip this tick until it is.
                try:
                    search = app.query_one("#settings-search", Input)
                except NoMatches:
                    await pilot.pause(0.05)
                    continue
                if search.value != "reminder_hour":
                    search.value = "reminder_hour"
                filtered = {card.spec.key for card in app.query(OptionCard)}
                if filtered == {"general.reminder_hour"}:
                    break
                await pilot.pause(0.05)
            assert filtered == {"general.reminder_hour"}
            search.value = ""
            await pilot.pause(0.1)
            # secrets never reach the rendered value
            rendered = "\n".join(str(node.render()) for node in app.query(Static))
            assert "sk-tui-secret" not in rendered
            # market tab browses the local repository
            from mailflow_tui.app import MarketPane

            market_pane = app.query_one(MarketPane)
            await market_pane.refresh_market()
            # the fetch runs in a worker thread: on slow CI runners the
            # table is still empty when refresh_market returns — poll
            # until the rows land (bounded)
            market_table = cast(DataTable[Any], app.query_one("#market-table", DataTable))
            for _ in range(100):
                if market_table.row_count >= 1:
                    break
                await pilot.pause(0.05)
            assert market_table.row_count >= 1
            market_rows = " ".join(
                " ".join(str(cell) for cell in market_table.get_row_at(i))
                for i in range(market_table.row_count)
            )
            assert "Market Test" in market_rows
            assert "browsable from the tui" in market_rows
            # reply modal: confirm is disabled until prepared
            pane._selected_id = "m-id"  # pyright: ignore[reportPrivateUsage]
            cast(Button, app.query_one("#btn-reply")).press()
            await pilot.pause(0.05)
            from mailflow_tui.app import ReplyModal

            modal = app.screen
            assert isinstance(modal, ReplyModal)
            confirm_button = cast(Button, modal.query_one("#reply-confirm"))
            assert confirm_button.disabled is True
            cast(Button, modal.query_one("#reply-prepare")).press()
            await pilot.pause(0.05)
            confirm_button = cast(Button, modal.query_one("#reply-confirm"))
            assert confirm_button.disabled is False
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_smart_action_keeps_real_progress_and_relevance_order(tmp_path: Path) -> None:
    """Spinner frames must retain batch status until ranked results arrive."""
    import asyncio
    import queue

    from mailflow.contracts import LLMCompletion
    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.app import MailPane

    class DelayedSearchRouter:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMCompletion:
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if system.startswith("You route one free-form"):
                return LLMCompletion(text='{"intent":"search"}', model="smart")
            if user.startswith("Reply with"):
                return LLMCompletion(text="ok", model="smart")
            self.calls += 1
            self.started.set()
            await self.release.wait()

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
            for _ in range(40):
                if router.started.is_set():
                    break
                await asyncio.sleep(0.05)
            assert router.started.is_set(), "smart action batch did not start"
            await asyncio.sleep(0.2)
            hint = app.query_one("#mail-empty-hint", Static)
            assert "Smart action  0/3" in str(hint.render())
            assert "scanning 3 mails" in str(hint.render())
            button.press()
            for _ in range(40):
                if str(search.value) == "" and int(table.row_count) == 3:
                    break
                await asyncio.sleep(0.05)
            assert not pane._smart_action_running  # pyright: ignore[reportPrivateUsage]
            assert search.value == ""
            assert table.row_count == 3

            search.value = "the student ID invitation"
            await asyncio.sleep(0.05)
            button.press()
            for _ in range(40):
                if router.calls == 2:
                    break
                await asyncio.sleep(0.05)
            assert router.calls == 2
            await asyncio.sleep(0.2)
            assert "Smart action  0/3" in str(hint.render())
            router.release.set()
            for _ in range(40):
                if int(table.row_count) == 1 and str(button.label) == "Smart action…":
                    break
                await asyncio.sleep(0.05)
            assert int(table.row_count) == 1
            assert "Pick up your student ID card" in str(table.get_row_at(0)[1])
            assert pane._smart_action_result is not None  # pyright: ignore[reportPrivateUsage]
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_smart_action_schedules_matched_seminar_mail(tmp_path: Path) -> None:
    """One instruction on the Mail tab filters or acts: asking for the
    schedule adds the matched seminar mail as a real schedule entry."""
    import queue

    from mailflow.contracts import LLMCompletion
    from mailflow.domain import ActionOrigin
    from mailflow.plugin_market import PluginMarket
    from mailflow_tui.app import MailPane

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
        """Routes each phase by its system prompt like a real endpoint.

        Candidate refs are opaque and per-call: each phase numbers the mails
        it was given, so the extraction reply uses the extraction listing's
        own ref rather than the matching phase's.
        """

        @staticmethod
        def _candidate_for(user: str, subject: str) -> str:
            before_subject = user.split(f"subject={subject}", maxsplit=1)[0]
            return before_subject.rsplit("candidate=", maxsplit=1)[1].splitlines()[0]

        @staticmethod
        def _ref_of(user: str) -> str:
            return next(
                line.split("=", 1)[1] for line in user.splitlines() if line.startswith("id=")
            )

        async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMCompletion:
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if system.startswith("You route one free-form"):
                return LLMCompletion(text='{"intent":"schedule_seminar"}', model="smart")
            if system.startswith("You are MailFlow's smart mail finder and"):
                candidate = self._candidate_for(user, "Optional Friday lecture")
                return LLMCompletion(text=f'[{{"id":"{candidate}","relevance":96}}]', model="smart")
            if system.startswith("You identify optional academic seminars"):
                return LLMCompletion(
                    text=(
                        f'[{{"id":"{self._ref_of(user)}","title":"Research colloquium",'
                        '"starts_at":"2099-10-15T14:00:00+08:00",'
                        '"ends_at":"2099-10-15T15:30:00+08:00",'
                        '"timezone":"Asia/Shanghai","location":"Room 201",'
                        '"confidence":93,"evidence":"15 October, Room 201"}]'
                    ),
                    model="smart",
                )
            return LLMCompletion(text="ok", model="smart")

    service.router = cast(Any, ActingRouter())
    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(160, 50)) as pilot:
            for _ in range(100):
                if await service.count_mails() == 3:
                    break
                await pilot.pause(0.05)
            pane = app.query_one(MailPane)
            search = app.query_one("#mail-search", Input)
            search.value = "把研讨会邮件加入我的日程"
            app.query_one("#smart-action", Button).press()
            for _ in range(80):
                if pane._smart_action_result is not None:  # pyright: ignore[reportPrivateUsage]
                    break
                await pilot.pause(0.05)
            result = pane._smart_action_result  # pyright: ignore[reportPrivateUsage]
            assert result is not None

            scheduled = await service.storage.list_custom_actions()
            assert [item.summary for item in scheduled] == ["Research colloquium"]
            assert scheduled[0].origin is ActionOrigin.SEMINAR
            # the entry keeps its source-mail backlink and the event window
            assert scheduled[0].mail_id == result.records[0].record_id
            assert scheduled[0].action_type == "seminar"
            assert scheduled[0].location == "Room 201"
            # the matched mail is what the table shows for this instruction
            table = cast(DataTable[Any], app.query_one("#mail-table", DataTable))
            assert table.row_count == 1
            assert "Optional Friday lecture" in str(table.get_row_at(0)[1])

            hint = app.query_one("#mail-empty-hint", Static)
            assert "Added 1 entry(ies) to the schedule." in str(hint.render())

            # nothing is left for review, so the Actions tab shows no
            # review control (discovery is not advertised as a feature)
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-actions"  # pyright: ignore[reportUnknownMemberType]
            for _ in range(60):
                if not app.query_one("#actions-review-seminars", Button).display:
                    break
                await pilot.pause(0.05)
            assert not app.query_one("#actions-review-seminars", Button).display
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
async def test_reply_letter_template_and_toolbar(tmp_path: Path) -> None:
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
            pane._selected_id = "m-id"  # pyright: ignore[reportPrivateUsage]
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
            status_node = _find_status(app)
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
async def test_market_buttons_rendered_with_labels(tmp_path: Path) -> None:
    """Every button keeps a visible label (regression: Textual 8 flat
    buttons collapsed to zero height under the old fixed-height CSS)."""
    from mailflow.plugin_market import PluginMarket
    from textual.widgets import Button

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
        async with app.run_test(size=(110, 42)) as pilot:
            await pilot.pause()
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-market"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.2)
            market_buttons = [b for b in app.query(Button) if (b.id or "").startswith("market-")]
            assert len(market_buttons) >= 5
            for button in market_buttons:
                assert button.size.height > 0, f"{button.id} collapsed"
                assert str(button.label).strip(), f"{button.id} label missing"
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_processed_mail_event_refreshes_panes(tmp_path: Path) -> None:
    """The runtime emits ``mailflow.mail.processed``; the app must subscribe
    to that exact name or mail processed after mount never shows up without
    a manual refresh (regression: it subscribed to the unprefixed name)."""
    from mailflow.domain import MailAnalysis
    from mailflow.plugin_market import PluginMarket
    from mailflow_testkit.fakes import make_mail as make_test_mail

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
            for _ in range(100):
                if await service.count_mails() == 3:
                    break
                await pilot.pause(0.05)
            await pilot.pause()
            table = cast(DataTable[Any], app.query_one("#mail-table", DataTable))
            before = table.row_count

            # store a fourth mail directly, then emit the runtime event the
            # app is expected to react to — no manual refresh anywhere
            extra = MailRecord(
                record_id="m-late",
                mail=make_test_mail(
                    message_id="m-late", account_id="acct-1", subject="Arrived later"
                ),
                auto_urgency=Urgency.INFO,
                analysis=MailAnalysis(summary="Arrived later", urgency=Urgency.INFO),
            )
            await service.storage.save_mail(extra)
            await service.events.emit("mailflow.mail.processed", record=extra)

            for _ in range(60):
                if table.row_count > before:
                    break
                await pilot.pause(0.05)
            assert table.row_count == before + 1, "app did not react to mailflow.mail.processed"
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
            assert "***" in status
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_mail_bulk_buttons_purge_reanalyze_and_save_profile(tmp_path: Path) -> None:
    """The Mail tab's third button row: clear expired mail (recoverable),
    re-analyze everything, and save the preferences the LLM reads."""
    import queue
    from datetime import UTC, datetime, timedelta

    from mailflow.domain import ActionItem, MailAnalysis, MailRecord
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
