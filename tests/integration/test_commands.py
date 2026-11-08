"""Shared commands through the service, permissions and in-memory storage."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from mailflow.commands import CommandRouter
from mailflow.config import MailFlowConfig
from mailflow.contracts import LLMRouter
from mailflow.domain import (
    Urgency,
)
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.plugins import PluginManager
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService
from mailflow.subscriptions import Subscriptions

from mailflow_test_support.commands import MemoryStorage, commands_service, make_record
from mailflow_test_support.commands import router as router


class TestCommandRouter:
    async def test_unknown_command(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, _ = router
        response = await commands.execute("bogus x")
        assert not response.ok
        assert "bogus" in response.text

    async def test_help_lists_topics(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, _ = router
        response = await commands.execute("help")
        assert response.ok
        # the topic list is paged, so every topic is checked across its pages
        pages = [response.text]
        for page in (2, 3):
            more = await commands.execute(f"help --page {page}")
            if more.ok:
                pages.append(more.text)
        for topic in (
            "mail",
            "action",
            "plugin",
            "adapter",
            "account",
            "llm",
            "reply",
            "lang",
            "trash",
            "clean",
            "runtime",
        ):
            assert any(topic in text for text in pages), topic

    async def test_mail_smart_search_chat(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        """`mail search <need>` runs the LLM finder and prints numbered
        summary-headlined rows the same way `mail list` does."""
        commands, _ = router
        from mailflow.config import LLMConfig

        commands.service.config.llms = [LLMConfig(llm_id="llm-1")]

        class SmartRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                user = messages[-1]["content"]
                # the plan call has no candidate listing; batches do
                if "subject=" not in user:

                    class Plan:
                        text = "{}"

                    return Plan()

                class Reply:
                    text = '```json\n["m1"]\n```'

                return Reply()

        commands.service.router = cast(LLMRouter, SmartRouter())
        response = await commands.execute("mail search the student ID mail")
        assert response.ok
        assert "#1" in response.text
        assert "Bring your student ID" in response.text
        # the number resolves for show
        shown = await commands.execute("mail show 1")
        assert shown.ok
        assert "Bring your student ID" in shown.text

    async def test_mail_smart_search_no_llm(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, _ = router
        commands.service.config.llms = []
        response = await commands.execute("mail search anything")
        assert not response.ok
        assert "LLM" in response.text

    async def test_mail_smart_search_reports_unavailable_batches(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        """A failed model batch must not be presented as no matching mail."""
        commands, _ = router
        from mailflow.config import LLMConfig

        commands.service.config.llms = [LLMConfig(llm_id="llm-1")]

        class FailingRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                if messages[-1]["content"].startswith("Reply with"):

                    class Warmup:
                        text = "ok"

                    return Warmup()
                raise TimeoutError("endpoint timed out")

        commands.service.router = cast(LLMRouter, FailingRouter())
        response = await commands.execute("mail search the student ID mail")

        assert not response.ok
        assert "could not check" in response.text

    async def test_mail_show_original_body(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, _ = router
        response = await commands.execute("mail show 1")
        assert response.ok
        assert "Bring your student ID" in response.text
        assert "Original body content" in response.text
        assert "sender@example.com" in response.text
        assert not (await commands.execute("mail show ghost")).ok
        assert not (await commands.execute("mail show ' '")).ok

    async def test_mail_urgency_set_and_reset(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, storage = router
        assert not (await commands.execute("mail urgency m1 improtant")).ok
        assert storage.mails["m1"].manual_urgency is None
        response = await commands.execute("mail urgency m1 urgent")
        assert response.ok
        assert storage.mails["m1"].manual_urgency is Urgency.URGENT
        response = await commands.execute("mail urgency m1 auto")
        assert response.ok
        assert storage.mails["m1"].manual_urgency is None
        assert storage.mails["m1"].effective_urgency is Urgency.IMPORTANT

    async def test_mail_delete_and_trash_restore(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, storage = router
        response = await commands.execute("mail delete m2")
        assert response.ok
        assert "m2" not in storage.mails
        response = await commands.execute("trash list")
        assert response.ok
        # the listing is id-free now: the deleted mail is row #1 and the
        # raw id is never shown (numbers are the interface)
        assert "#1" in response.text
        assert "m2" not in response.text
        response = await commands.execute("trash restore m2")
        assert response.ok
        assert "m2" in storage.mails
        assert "m2" not in storage.trash

    async def test_action_add_list_and_show(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, storage = router
        shown = await commands.execute("action show 1")
        assert shown.ok and "Final calculus exam" in shown.text and "m3" in shown.text
        response = await commands.execute(
            'action add "Pick up parcel" --due "2026-06-20 15:30" --type errand --notes "gate 3"'
        )
        assert response.ok, response.text
        assert "Pick up parcel" in response.text
        item_id = next(iter(storage.custom_actions))
        assert storage.custom_actions[item_id].mail_id == ""  # user-created marker
        assert storage.custom_actions[item_id].action_type == "errand"
        assert storage.custom_actions[item_id].notes == "gate 3"
        # the user item appears in action list/show with "user" as the source
        response = await commands.execute("action list")
        assert response.ok
        assert "Pick up parcel" in response.text
        assert "user" in response.text
        response = await commands.execute(f"action show {item_id}")
        assert response.ok
        assert "Pick up parcel" in response.text
        assert (await commands.execute(f"action delete {item_id[:10]}")).ok
        assert item_id not in storage.custom_actions
        assert not (await commands.execute(f"action delete {item_id}")).ok
        assert (await commands.execute("action delete a1")).ok
        assert "Final calculus exam" not in (await commands.execute("action list")).text

    async def test_action_add_validation(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, storage = router
        response = await commands.execute('action add "no due"')
        assert response.ok
        undated = next(iter(storage.custom_actions.values()))
        assert undated.due_at is None
        assert (await commands.execute(f"action complete {undated.item_id}")).ok
        assert "no due" not in (await commands.execute("action list")).text
        assert "no due" in (await commands.execute("action list --state completed")).text
        assert (await commands.execute(f"action reopen {undated.item_id}")).ok
        assert (await commands.execute(f"action delete {undated.item_id}")).ok
        # malformed due time
        response = await commands.execute('action add "bad" --due "not-a-time"')
        assert not response.ok
        assert "invalid due time" in response.text
        # multi-word summary without --type/--notes
        response = await commands.execute(
            'action add "Renew passport at the office" --due "2026-07-01 09:00"'
        )
        assert response.ok
        item = next(iter(storage.custom_actions.values()))
        assert item.summary == "Renew passport at the office"
        assert item.action_type == "errand"  # default type

    async def test_reply_flow_through_commands(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, storage = router
        created = await commands.execute("reply create #1")
        assert created.ok
        draft_id = created.text.split()[1]
        assert storage.drafts[draft_id].mail_id == "m1"
        edited = await commands.execute(
            f"reply edit {draft_id} 'Re: Exam' '**urgent** note <right>Li Si</right>'"
        )
        assert edited.ok
        assert "<b>urgent</b>" in storage.drafts[draft_id].body
        shown = await commands.execute(f"reply show {draft_id}")
        assert shown.ok and "urgent" in shown.text and "Li Si" in shown.text
        assert "<b>" not in shown.text
        prepared = await commands.execute(f"reply prepare {draft_id}")
        assert prepared.ok
        token = prepared.text.split("token: ")[-1].split(" ")[0]
        confirmed = await commands.execute(f"reply confirm {draft_id} {token}")
        assert confirmed.ok
        assert storage.drafts[draft_id].state.value == "sent"
        # wrong token rejected
        response = await commands.execute(f"reply confirm {draft_id} wrong")
        assert not response.ok

    async def test_reply_compose_letter_templates(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, storage = router
        cn = await commands.execute("reply compose m1 cn")
        assert cn.ok, cn.text
        cn_id = cn.text.split()[2]
        draft = storage.drafts[cn_id]
        assert "尊敬的" in draft.body
        assert "text-align:right" in draft.body  # right-aligned signature block
        en = await commands.execute("reply compose m1 en")
        assert en.ok
        en_draft = storage.drafts[en.text.split()[2]]
        assert "Dear" in en_draft.body
        # unknown template rejected
        bad = await commands.execute("reply compose m1 ja")
        assert not bad.ok
        assert "unknown letter template" in bad.text

    async def test_lang_get_and_set(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, storage = router
        response = await commands.execute("lang get")
        assert response.ok
        assert "en" in response.text
        response = await commands.execute("lang set zh-CN")
        assert response.ok
        assert storage.preferences["language"] == "zh-CN"
        response = await commands.execute("lang set klingon")
        assert not response.ok

    async def test_config_list_shows_options_and_secrets_redacted(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, _ = router
        # give the service an llm with a secret so redaction is exercised
        from mailflow.config import MailFlowConfig

        service = commands.service
        service.config = MailFlowConfig.model_validate(
            {"llms": [{"llm_id": "l1", "base_url": "https://x", "api_key": "sk-topsecret"}]}
        )
        response = await commands.execute("config list")
        assert response.ok
        assert "general.reminder_hour" in response.text
        assert "required" in response.text.lower() or "yes" in response.text
        assert "sk-topsecret" not in response.text
        assert "llms[].api_key*" in response.text  # secret marker

    async def test_config_set_persists_to_file(self, tmp_path: Path) -> None:
        from mailflow.config import MailFlowConfig, NotifierConfig

        from mailflow_test_support.runtime import RecordingNotifier

        config = MailFlowConfig()
        path = tmp_path / "config.toml"
        config_path = str(path)
        service = commands_service(config, config_path)
        commands = CommandRouter(service)
        response = await commands.execute("config set general.reminder_hour 9")
        assert response.ok
        assert "9" in response.text
        from mailflow.config import load_config

        reloaded = load_config(path)
        assert reloaded.general.reminder_hour == 9
        # invalid value is rejected and the file is untouched
        response = await commands.execute("config set general.reminder_hour oops")
        assert not response.ok
        assert load_config(path).general.reminder_hour == 9
        notifier = RecordingNotifier()
        await service.runtime.reconfigure_notifiers(
            [notifier],
            [NotifierConfig(notifier_id="fake", provider="fake", minimum_urgency=Urgency.INFO)],
        )
        response = await commands.execute("config set general.mail_notifications false")
        assert response.ok and not load_config(path).general.mail_notifications
        await service.process_mail(make_record("muted").mail, notify=True)
        assert not notifier.notified
        response = await commands.execute("config set general.mail_notifications true")
        assert response.ok and load_config(path).general.mail_notifications
        await service.process_mail(make_record("enabled").mail, notify=True)
        assert len(notifier.notified) == 1
        await service.stop()

    async def test_config_set_requires_config_file(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, _ = router
        response = await commands.execute("config set general.reminder_hour 9")
        assert not response.ok
        assert response.text == commands.service.t("errors.config_required")

    async def test_plugin_search_filters_market(
        self, router: tuple[CommandRouter, MemoryStorage], tmp_path: Path
    ) -> None:
        import json as jsonlib

        from mailflow.config import PluginRepositoryConfig
        from mailflow.plugin_market import PluginMarket, Repository

        (tmp_path / "notifier" / "mailflow-searchable").mkdir(parents=True)
        (tmp_path / "processor" / "mailflow-other").mkdir(parents=True)
        (tmp_path / "index.json").write_text(
            jsonlib.dumps(
                {
                    "name": "local",
                    "schema": 2,
                    "categories": [
                        {"id": "notifier", "path": "notifier"},
                        {"id": "processor", "path": "processor"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "notifier" / "mailflow-searchable" / "plugin.json").write_text(
            jsonlib.dumps(
                {
                    "id": "mailflow-searchable",
                    "name": "Searchable Plugin",
                    "description": "handles webhook delivery",
                    "categories": ["notifier"],
                    "package": "mailflow-searchable",
                    "source": "x",
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "processor" / "mailflow-other" / "plugin.json").write_text(
            jsonlib.dumps(
                {
                    "id": "mailflow-other",
                    "name": "Other Plugin",
                    "description": "completely different thing",
                    "categories": ["processor"],
                    "package": "mailflow-other",
                    "source": "x",
                }
            ),
            encoding="utf-8",
        )
        path = tmp_path
        commands, _ = router
        service = commands.service
        service.config.plugins.repositories.append(
            PluginRepositoryConfig(name="local", url=path.as_uri())
        )
        service.market = PluginMarket([Repository("local", path.as_uri())])
        response = await commands.execute("plugin search webhook")
        assert response.ok
        assert "mailflow-searchable" in response.text
        assert "mailflow-other" not in response.text
        response = await commands.execute("plugin search webhook processor")
        assert response.ok
        assert "mailflow-searchable" not in response.text  # category mismatch

    async def test_plugin_enable_disable_persists(self, tmp_path: Path) -> None:
        from mailflow.plugin_api import define_plugin

        service = commands_service(MailFlowConfig(), config_path=str(tmp_path / "config.toml"))
        from mailflow.plugin_market import PluginMarket

        service.market = PluginMarket([])  # keep the ghost lookup offline
        plugin = define_plugin("fake-plugin")

        @plugin.notifier("fake-notifier")
        class FakeNotifier:  # pyright: ignore[reportUnusedClass]
            def __init__(self, config: Any) -> None:
                pass

            async def notify(self, record: Any) -> None:
                pass

        service.plugin_manager.register(plugin.build())
        commands = CommandRouter(service)
        response = await commands.execute("plugin disable fake-plugin")
        assert response.ok
        assert "fake-plugin" in service.config.plugins.disabled
        response = await commands.execute("plugin enable fake-plugin")
        assert response.ok
        assert "fake-plugin" not in service.config.plugins.disabled
        response = await commands.execute("plugin disable ghost")
        assert not response.ok
        assert "not loaded or installed" in response.text

    async def test_plugin_uninstall_unknown(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        from mailflow.plugin_market import PluginMarket

        commands, _ = router
        # no repositories configured: the lookup must fail offline instead of
        # fetching the default marketplace over the network
        commands.service.market = PluginMarket([])
        response = await commands.execute("plugin uninstall ghost")
        assert not response.ok
        assert "not found in any repository" in response.text


class TestMarketLocalization:
    """Localized plugin metadata and rich markdown rendering."""

    def _repo(self, tmp_path: Path, language: str) -> tuple[CommandRouter, MailFlowService]:
        import json as jsonlib

        from mailflow.config import PluginRepositoryConfig
        from mailflow.plugin_market import PluginMarket, Repository

        (tmp_path / "notifier" / "mailflow-l10n").mkdir(parents=True)
        (tmp_path / "index.json").write_text(
            jsonlib.dumps(
                {
                    "name": "local",
                    "schema": 2,
                    "categories": [{"id": "notifier", "path": "notifier"}],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "notifier" / "mailflow-l10n" / "plugin.json").write_text(
            jsonlib.dumps(
                {
                    "id": "mailflow-l10n",
                    "name": "L10n Plugin",
                    "version": "1.0.0",
                    "description": "English summary",
                    "categories": ["notifier"],
                    "package": "mailflow-l10n",
                    "source": "x",
                    "author": "Test Author",
                    "updated": "2026-07-01",
                    "descriptions": {"zh-CN": "中文简介"},
                    "readme": 'English readme with <span style="color:#ff5500">orange</span> and **bold**.',
                    "readmes": {
                        "zh-CN": '中文 readme，<span style="color:red">红色</span>与 ~~删除线~~。'
                    },
                }
            ),
            encoding="utf-8",
        )
        service = commands_service()
        service.i18n = I18n(language=language)
        service.config.plugins.repositories.append(
            PluginRepositoryConfig(name="local", url=tmp_path.as_uri())
        )
        service.market = PluginMarket([Repository("local", tmp_path.as_uri())])
        return CommandRouter(service), service

    async def test_market_show_uses_localized_readme(self, tmp_path: Path) -> None:
        commands, _ = self._repo(tmp_path, "zh-CN")
        response = await commands.execute("plugin market show mailflow-l10n")
        assert response.ok
        assert "Test Author" in response.text
        assert "2026-07-01" in response.text
        assert "中文 readme" in response.text
        assert "English readme" not in response.text

    async def test_market_show_falls_back_to_english(self, tmp_path: Path) -> None:
        commands, _ = self._repo(tmp_path, "en")
        response = await commands.execute("plugin market show mailflow-l10n")
        assert response.ok
        assert "English readme" in response.text
        assert "中文 readme" not in response.text

    async def test_market_list_uses_localized_description(self, tmp_path: Path) -> None:
        commands, _ = self._repo(tmp_path, "zh-CN")
        response = await commands.execute("plugin market list")
        assert response.ok
        assert "中文简介" in response.text

    async def test_markdown_renders_span_color_and_strike(self) -> None:
        from mailflow.commands import _markdown_spans  # pyright: ignore[reportPrivateUsage]

        spans = _markdown_spans(
            'Before <span style="color:#ff5500">orange</span> and **bold** and ~~strike~~.'
        )
        text = "".join(s.text for s in spans)
        assert "orange" in text and "bold" in text and "strike" in text
        styles = {s.style for s in spans}
        assert "#ff5500" in styles
        assert any("bold" in s.style for s in spans)
        assert any("strike" in s.style for s in spans)

    async def test_markdown_span_color_is_bounded(self) -> None:
        from mailflow.commands import _markdown_spans  # pyright: ignore[reportPrivateUsage]

        spans = _markdown_spans('A <span style="color:red">red</span> plain.')
        joined = "".join(s.text for s in spans)
        assert "red" in joined and "plain" in joined
        red_runs = [s for s in spans if s.style == "red"]
        assert len(red_runs) == 1
        assert red_runs[0].text.strip() == "red"


class TestPaginationAndFeedback:
    async def _many_mails_router(self) -> tuple[CommandRouter, MemoryStorage]:
        storage = MemoryStorage()
        for index in range(12):
            await storage.save_mail(
                make_record(
                    record_id=f"mail-{index:02d}-abcdef123456",
                    subject=f"Subject {index:02d}",
                )
            )
        service = MailFlowService(
            config=MailFlowConfig(),
            registry=ComponentRegistry(),
            plugin_manager=PluginManager(),
            storage=cast(Any, storage),
            sources={},
            router=cast(LLMRouter, None),
            pipeline=PipelineEngine([]),
            notifiers=[],
            notifier_configs=[],
            events=EventBus(),
            i18n=I18n(),
        )
        return CommandRouter(service), storage

    async def test_mail_list_pagination_and_query(self) -> None:
        commands, _ = await self._many_mails_router()
        page1 = await commands.execute("mail list --page 1")
        assert page1.ok
        assert "1/2" in page1.text
        assert "#10" in page1.text  # rows 1..10 on page 1
        assert "#11" not in page1.text  # page 2 content
        page2 = await commands.execute("mail list --page 2")
        assert page2.ok
        assert "2/2" in page2.text
        assert "#11" in page2.text
        assert "#10" not in page2.text
        assert (await commands.execute("mail list 2")).text == page2.text
        filtered = await commands.execute("mail list --query 'subject 05'")
        assert filtered.ok
        # the query matches the raw subject; the row shows the LLM summary
        # and the id tail is gone (numbers identify rows)
        assert "Mails (1)" in filtered.text
        assert "#1" in filtered.text
        # exactly one mail matched: nothing else on the page
        assert "Subject 00" not in filtered.text

    async def test_mail_commands_accept_prefix_ids(self) -> None:
        commands, storage = await self._many_mails_router()
        shown = await commands.execute("mail show mail-00")  # unique prefix
        assert shown.ok
        assert "Subject 00" in shown.text
        updated = await commands.execute("mail urgency mail-11-abcdef urgent")  # unique prefix
        assert updated.ok
        assert storage.mails["mail-11-abcdef123456"].manual_urgency is Urgency.URGENT

    async def test_feedback_records_and_guidelines(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, storage = router
        response = await commands.execute("feedback m1 'Promotions are noise; I never need them'")
        assert response.ok
        assert storage.preferences["feedback.m1"] == "Promotions are noise; I never need them"
        assert "feedback.guidelines" not in storage.preferences
        remembered = await commands.execute(
            "feedback m1 'Promotions are noise; I never need them' --remember"
        )
        assert remembered.ok
        guidelines = storage.preferences["feedback.guidelines"]
        assert "m1: Promotions are noise" in guidelines
        # shown in mail detail
        shown = await commands.execute("mail show m1")
        assert shown.ok
        assert "Promotions are noise" in shown.text
        # unknown mail rejected
        assert not (await commands.execute("feedback ghost reason")).ok


class TestLocalPluginInstall:
    async def test_install_batch_of_plugin_folders(
        self,
        tmp_path: Path,
        router: tuple[CommandRouter, MemoryStorage],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commands, _ = router
        root = tmp_path / "plugins"
        for plugin_id in ("alpha", "beta"):
            folder = root / plugin_id
            folder.mkdir(parents=True)
            (folder / "plugin.json").write_text(
                f'{{"id": "{plugin_id}", "name": "{plugin_id}"}}', encoding="utf-8"
            )
        calls: list[str] = []

        async def fake_install(plugin: Any, *, check: bool = True) -> str:
            calls.append(plugin.id)
            return "ok"

        monkeypatch.setattr(commands.service.market, "install", fake_install)
        response = await commands.execute(f"plugin install {str(root).replace(chr(92), chr(47))}")
        assert response.ok, response.text
        assert calls == ["alpha", "beta"]

    async def test_install_no_plugins_found(
        self,
        tmp_path: Path,
        router: tuple[CommandRouter, MemoryStorage],
    ) -> None:
        commands, _ = router
        empty = tmp_path / "empty"
        empty.mkdir()
        response = await commands.execute(f"plugin install {str(empty).replace(chr(92), chr(47))}")
        assert not response.ok
        assert "no plugins found" in response.text


class TestMailWipe:
    async def test_wipe_confirmed_clears_mails_and_trash(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, storage = router
        denied = await commands.execute("mail wipe")
        assert not denied.ok and len(storage.mails) == 3
        await commands.execute("mail delete m1")
        assert len(storage.trash) >= 1
        response = await commands.execute("mail wipe CONFIRM")
        assert response.ok
        assert len(storage.mails) == 0
        assert len(storage.trash) == 0


class TestCleanCommands:
    """`clean records` is the recoverable clear; `clean trash` destroys it."""

    async def test_records_moves_mail_and_actions_to_the_trash(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, storage = router
        await commands.execute("mail delete m1")
        # a mail-derived entry and a user todo: both must leave the schedule
        await commands.execute('action add --summary "Submit the form" --due "2099-05-01 09:00"')
        assert len(storage.custom_actions) == 1

        response = await commands.execute("clean records")

        assert response.ok
        assert len(storage.mails) == 0
        assert len(storage.custom_actions) == 0
        # nothing was destroyed: every mail is recoverable from the trash
        assert len(storage.trash) == 3
        assert not (await commands.execute("clean trash")).ok
        assert len(storage.trash) == 3
        assert (await commands.execute("clean trash CONFIRM")).ok
        assert storage.trash == {}

    async def test_usage_is_reported_for_a_bare_or_unknown_subcommand(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, storage = router
        for line in ("clean", "clean nonsense", "clean records extra"):
            response = await commands.execute(line)
            assert not response.ok, line
            assert "records" in response.text and "trash" in response.text, line
        assert len(storage.mails) == 3  # nothing ran


@pytest.mark.asyncio
async def test_chat_help_is_sectioned() -> None:
    """help is a per-topic manual: every chunk opens with a '【…】' title
    (lifted into the forward-node name) and explains commands with usage,
    meaning and an example."""
    from mailflow.config import MailFlowConfig
    from mailflow.i18n import I18n

    cfg = MailFlowConfig()
    cfg.general.command_prefix = "/"
    service = commands_service(cfg)
    service.config = cfg
    service.i18n = I18n()
    chunks = await service.command_dispatch(
        "/mailflow help",
        context=__import__("mailflow.permissions", fromlist=["LOCAL_CONTEXT"]).LOCAL_CONTEXT,
    )
    assert isinstance(chunks, list) and chunks
    for chunk in chunks:
        assert "\n" in chunk  # every section has body beyond the title
    joined = "\n".join(chunks)
    for expected in (
        "mail list",
        "reply confirm",
        "action add",
        "mailflow subscribe",
        "token",
    ):
        assert expected in joined, expected


async def test_command_dispatch_prefix_filtering() -> None:
    """Chat messages only dispatch when they start with the prefix."""
    from mailflow.config import MailFlowConfig
    from mailflow.domain import CommandResponse

    cfg = MailFlowConfig()
    cfg.general.command_prefix = "/"
    service = commands_service(cfg)
    service.config = cfg
    from mailflow.i18n import I18n as _I18n

    service.i18n = _I18n()

    calls: list[str] = []

    class FakeRouter:
        async def execute(self, line: str) -> CommandResponse:
            calls.append(line)
            return CommandResponse.plain(f"ok:{line}")

    service.commands = FakeRouter()
    # bare commands no longer execute: everything lives in the mailflow
    # namespace, the reply teaches the corrected form instead
    bare = await service.command_dispatch("/mail list")
    assert bare is None
    assert await service.command_dispatch("/weather Shanghai") is None
    assert await service.command_dispatch("/mailflower help") is None
    hinted = await service.command_dispatch("mailflow unsubscribe")
    assert hinted is not None and "mailflow help" in hinted
    assert await service.command_dispatch("hello") is None
    # only the namespace dispatches; non-subscription commands route
    # straight to the shared router (their own semantics apply)
    assert await service.command_dispatch("/mailflow mail list") == "ok:mail list"
    assert calls == ["mail list"]
    # subscription commands without a chat get the context hint
    hinted = await service.command_dispatch("/mailflow subscribe")
    assert hinted is not None and "group chat" in hinted
    assert calls == ["mail list"]

    # custom prefix
    cfg.general.command_prefix = "!"
    assert await service.command_dispatch("!mailflow mail list") == "ok:mail list"
    assert await service.command_dispatch("/mail list") is None


@pytest.mark.asyncio
async def test_mailflow_command_crash_returns_error_reply() -> None:
    """An internal crash must never reach the user as silence. A broken
    internal state (reported live: subscribe/status/unsubscribe 500ing on
    a running instance with no visible trace — bot_server logged the
    failure at DEBUG) now yields a visible error reply and a logged
    traceback."""
    from mailflow.config import MailFlowConfig

    cfg = MailFlowConfig()
    service = commands_service(cfg)
    service.config = cfg
    from mailflow.i18n import I18n as _I18n

    service.i18n = _I18n()

    class BrokenSubscriptions(Subscriptions):
        async def subscribers(self, provider: str, instance_id: str) -> list[str]:
            raise RuntimeError("subscription storage unavailable")

    service.subscriptions = BrokenSubscriptions(service.storage)
    reply = await service.command_dispatch(
        "/mailflow status",
        sender="10001",
        chat_id="888",
        chat_type="group",
        provider="napcat",
        instance_id="qq-bot-1",
    )
    assert reply is not None
    assert reply == service.t("chat.error_reply")


@pytest.mark.asyncio
async def test_mailflow_hourly_toggle() -> None:
    """'hourly on/off' toggles a persisted per-chat preference; bare
    'hourly' reads the state; non-admins are rejected."""
    from mailflow.config import MailFlowConfig, NotifierConfig

    cfg = MailFlowConfig()
    cfg.notifiers = [
        NotifierConfig(
            notifier_id="napcat-1",
            provider="onebot",
            options={
                "admins": ["10001"],
                "targets": ["group:888"],
                "http_url": "http://x",
                "hourly_summary": True,
            },
        )
    ]
    service = commands_service(cfg)
    service.config = cfg
    from mailflow.i18n import I18n as _I18n

    service.i18n = _I18n()
    service.commands = None
    store: dict[str, str] = {}

    class FakeSubs:
        async def add(self, p: str, i: str, c: str) -> bool:
            return True

        async def remove(self, p: str, i: str, c: str) -> bool:
            return True

        async def subscribers(self, p: str, i: str) -> list[str]:
            return []

    service.subscriptions = FakeSubs()  # pyright: ignore[reportAttributeAccessIssue]

    class S:
        async def get_preference(self, k: str) -> str | None:
            return store.get(k)

        async def set_preference(self, k: str, v: str) -> None:
            store[k] = v

    service.storage = S()  # pyright: ignore[reportAttributeAccessIssue]

    async def rebuild() -> None:
        pass

    service._rebuild_notifiers = rebuild  # type: ignore[method-assign]

    async def sync(*args: Any, **kwargs: Any) -> None:
        pass

    service._sync_subscription_targets = sync  # type: ignore[method-assign]

    async def call(text: str, sender: str = "10001") -> str:
        result = await service.command_dispatch(
            text,
            sender=sender,
            chat_id="888",
            chat_type="group",
            provider="napcat",
            instance_id="napcat-1",
        )
        return str(result)

    assert "OFF" in await call("/mailflow hourly") or "未生效" in await call("/mailflow hourly")
    cfg.general.hourly_summary = True
    from mailflow.config import LLMConfig

    cfg.llms = [LLMConfig(llm_id="test")]
    await call("/mailflow hourly off")
    assert "enabled" in await call("/mailflow hourly on") or "已开启" in await call(
        "/mailflow hourly on"
    )
    assert await call("/mailflow hourly on")  # twice: idempotent
    assert "ON" in await call("/mailflow hourly") or "已开启" in await call("/mailflow hourly")
    assert store["hourly_summary.chat.onebot.napcat-1.888"] == "on"
    assert "OFF" in await call("/mailflow hourly off") or "已关闭" in await call(
        "/mailflow hourly off"
    )
    assert store["hourly_summary.chat.onebot.napcat-1.888"] == "off"
    # non-admin rejected
    rejected = await call("/mailflow hourly on", sender="99999")
    assert "administrator" in rejected or "管理员" in rejected
    # bad arg
    assert "Usage" in await call("/mailflow hourly maybe") or "用法" in await call(
        "/mailflow hourly maybe"
    )


@pytest.mark.asyncio
async def test_mailflow_subscribe_requires_admin() -> None:
    from mailflow.config import MailFlowConfig, NotifierConfig

    cfg = MailFlowConfig()
    cfg.general.command_prefix = "/"
    cfg.notifiers = [
        NotifierConfig(
            notifier_id="napcat-1",
            provider="onebot",
            options={"admins": ["10001"], "http_url": "http://x"},
        )
    ]
    service = commands_service(cfg)
    service.config = cfg
    from mailflow.i18n import I18n as _I18n

    service.i18n = _I18n()
    service.commands = None

    class FakeSubs:
        def __init__(self) -> None:
            self.added: list[tuple[str, str, str]] = []
            self.removed: list[tuple[str, str, str]] = []

        async def add(self, p: str, i: str, c: str) -> bool:
            self.added.append((p, i, c))
            return True

        async def remove(self, p: str, i: str, c: str) -> bool:
            self.removed.append((p, i, c))
            return True

        async def subscribers(self, p: str, i: str) -> list[str]:
            return []

    service.subscriptions = FakeSubs()  # pyright: ignore[reportAttributeAccessIssue]

    async def no_reload() -> None:
        pass

    service.reload_runtime = no_reload  # type: ignore[method-assign]

    # non-admin cannot subscribe
    reply = await service.command_dispatch(
        "/mailflow subscribe",
        sender="99999",
        chat_id="group-1",
        chat_type="group",
        provider="napcat",
        instance_id="napcat-1",
    )
    assert reply is not None and "administrator" in reply

    # admin can subscribe; targets synced into the notifier config
    reply = await service.command_dispatch(
        "/mailflow subscribe",
        sender="10001",
        chat_id="group-1",
        chat_type="group",
        provider="napcat",
        instance_id="napcat-1",
    )
    assert reply is not None and "pushed to this chat" in reply
    assert service.subscriptions.added == [  # pyright: ignore[reportUnknownMemberType,reportAttributeAccessIssue]
        ("napcat", "napcat-1", "group-1")
    ]
    # OneBot targets must be prefixed (user: / group:); a bare id is
    # malformed and silently dropped by the notifier
    from mailflow.notifications import targets_of

    assert "group:group-1" in targets_of(service.config.notifiers[0])

    # help: section-grouped chunks, every section carries mailflow commands
    help_chunks = await service.command_dispatch(
        "/mailflow help", sender="10001", provider="napcat", instance_id="napcat-1"
    )
    assert isinstance(help_chunks, list) and help_chunks
    joined = "\n".join(help_chunks)
    assert "mailflow subscribe" in joined
    assert "mailflow mail list" in joined
    assert "reply confirm" in joined


@pytest.mark.asyncio
async def test_mailflow_unsubscribe_removes_target() -> None:
    from mailflow.config import MailFlowConfig, NotifierConfig

    cfg = MailFlowConfig()
    cfg.general.command_prefix = "/"
    cfg.notifiers = [
        NotifierConfig(
            notifier_id="napcat-1",
            provider="onebot",
            options={
                "admins": ["10001"],
                "targets": ["group:group-1"],
                "http_url": "http://x",
            },
        )
    ]
    service = commands_service(cfg)
    service.config = cfg
    from mailflow.i18n import I18n as _I18n

    service.i18n = _I18n()
    service.commands = None

    class FakeSubs:
        async def add(self, p: str, i: str, c: str) -> bool:
            return True

        async def remove(self, p: str, i: str, c: str) -> bool:
            return True

        async def subscribers(self, p: str, i: str) -> list[str]:
            return []

    service.subscriptions = FakeSubs()  # pyright: ignore[reportAttributeAccessIssue]

    async def no_reload() -> None:
        pass

    service.reload_runtime = no_reload  # type: ignore[method-assign]

    reply = await service.command_dispatch(
        "/mailflow unsubscribe",
        sender="10001",
        chat_id="group-1",
        chat_type="group",
        provider="napcat",
        instance_id="napcat-1",
    )
    assert reply is not None and "cancelled" in reply
    from mailflow.notifications import targets_of

    assert "group:group-1" not in targets_of(service.config.notifiers[0])


async def test_filtered_mail_numbers_survive_new_arrivals_and_other_users() -> None:
    from datetime import UTC, datetime

    from mailflow.permissions import PermissionContext, permission_scope

    service = commands_service()
    commands = CommandRouter(service)
    await service.storage.save_mail(make_record("m1", subject="Target"))
    user = PermissionContext("alice", "telegram", "bot", "chat", True)
    other = PermissionContext("bob", "telegram", "bot", "chat", True)
    with permission_scope(user):
        listing = await commands.execute("mail list Bring")
        assert "#1" in listing.text and "m1" in listing.text
    new = make_record("new")
    new.mail.received_at = datetime.now(UTC)
    await service.storage.save_mail(new)
    with permission_scope(other):
        await commands.execute("mail list")
    with permission_scope(user):
        shown = await commands.execute("mail show #1")
        assert "Bring your student ID" in shown.text
        assert (await commands.execute("mail delete #1")).ok
    assert await service.storage.get_mail("m1") is None
    assert await service.storage.get_mail("new") is not None


@pytest.mark.parametrize("provider,chat_id", [("telegram", "42"), ("openwechat", "@@group")])
async def test_unsubscribe_disables_equivalent_fixed_and_subscribed_targets(
    provider: str, chat_id: str
) -> None:
    from mailflow.config import NotifierConfig
    from mailflow.notifications import targets_of
    from mailflow.permissions import LOCAL_CONTEXT

    config = MailFlowConfig(
        notifiers=[
            NotifierConfig(
                notifier_id="bot",
                provider=provider,
                options={
                    "targets": [f"group:{chat_id}"],
                    "chat_id": chat_id,
                    "bot_token": "fake",
                    "gateway_url": "http://fake",
                },
            )
        ]
    )
    service = commands_service(config)
    identity: dict[str, Any] = {
        "provider": provider,
        "instance_id": "bot",
        "chat_id": chat_id,
        "chat_type": "group",
        "context": LOCAL_CONTEXT,
    }
    # Use the actual platform identity, with a granted role, throughout dispatch.
    from mailflow.permissions import PermissionContext

    identity["context"] = PermissionContext("admin", provider, "bot", chat_id, True)
    await service.command_dispatch("/mailflow subscribe", **identity)
    assert len(targets_of(service.config.notifiers[0])) == 1
    await service.command_dispatch("/mailflow unsubscribe", **identity)
    assert targets_of(service.config.notifiers[0]) == []
    assert service.config.notifiers[0].options["chat_id"] == chat_id
    await service.command_dispatch("/mailflow subscribe", **identity)
    assert len(targets_of(service.config.notifiers[0])) == 1
