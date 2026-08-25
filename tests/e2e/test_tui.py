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
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Select,
    Static,
    TabbedContent,
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
            "general": {"timezone": "UTC", "workers": 2},
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

    for attempt in range(2):
        try:
            select.value = value
            await pilot.pause(0.15)
            return
        except QueryError:
            if attempt == 1:
                raise
            await pilot.pause(0.3)


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
            from mailflow_tui.app import MailPane

            await app.query_one(MailPane).refresh_mail()

            table = cast(DataTable[Any], app.query_one("#mail-table", DataTable))
            assert table.row_count == 3
            # search filters the table
            search = app.query_one("#mail-search", Input)
            search.value = "promotion"
            await pilot.pause(0.05)
            assert table.row_count == 1

            search.value = ""
            await pilot.pause(0.05)
            assert table.row_count == 3
            # select the urgent mail row: its urgency cell carries the contract color
            urgent_index = next(
                i for i in range(table.row_count) if "urgent" in str(table.get_row_at(i)[0])
            )
            urgent_cell_text = str(table.get_row_at(urgent_index)[0])
            assert "■" in urgent_cell_text
            assert "urgent" in urgent_cell_text

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

            lang_card = next(
                card for card in app.query(OptionCard) if card.spec.key == "general.language"
            )
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
            # has actually been applied (the remount may swap cards late)
            search = app.query_one("#settings-search", Input)
            search.value = "reminder_hour"
            filtered: set[str] = set()
            for _ in range(40):
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
            market_table = cast(DataTable[Any], app.query_one("#market-table", DataTable))
            assert market_table.row_count == 1
            market_rows = " ".join(str(cell) for cell in market_table.get_row_at(0))
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
            for _ in range(200):
                target = tmp_path / "mailflow-demo-wizard"
                if (target / "plugin.json").is_file():
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
            readme = app.screen.query_one("#market-detail-readme", Markdown)  # pyright: ignore[reportUnknownMemberType]
            content = str(getattr(readme, "_markdown", ""))  # pyright: ignore[reportUnknownMemberType]
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
