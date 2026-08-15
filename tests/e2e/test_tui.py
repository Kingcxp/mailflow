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
from textual.widgets import Button, DataTable, Input, Select

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
    from mailflow_processor_llm_importance.plugin import plugin as llm_processor_plugin
    from mailflow_processor_rules.plugin import plugin as rules_plugin

    manager.register(rules_plugin)
    manager.register(llm_processor_plugin)
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
            select.value = "ad"
            await pilot.pause(0.05)
            record: MailRecord | None = await service.get_mail("m-id")
            assert record is not None
            assert record.manual_urgency is Urgency.AD
            # language switch persists
            lang_select = cast(Select[Any], app.query_one("#language-select", Select))
            lang_select.value = "zh-CN"
            await pilot.pause(0.05)
            assert await service.get_language() == "zh-CN"
            # the tab titles and headers re-translate on language change
            from textual.widgets import TabbedContent

            tabs = app.query_one(TabbedContent)
            tab_label = tabs.get_tab("tab-mail")  # pyright: ignore[reportUnknownMemberType]
            assert str(tab_label.label) == "邮件"  # pyright: ignore[reportUnknownMemberType]
            # settings tab lists every config option; secrets are redacted
            from mailflow_tui.app import SettingsPane

            settings = app.query_one(SettingsPane)
            await settings.refresh_config()
            config_table = cast(DataTable[Any], app.query_one("#config-table", DataTable))
            assert config_table.row_count > 30
            all_rows = "\n".join(
                " ".join(str(cell) for cell in config_table.get_row_at(i))
                for i in range(config_table.row_count)
            )
            assert "general.reminder_hour" in all_rows
            assert "sk-tui-secret" not in all_rows
            assert "llms[].api_key*" in all_rows
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
