"""Unit tests for the shared command router."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from mailflow.commands import CommandRouter
from mailflow.config import MailFlowConfig
from mailflow.contracts import LLMRouter, MailMessage, ReplyDraft
from mailflow.domain import (
    ActionItem,
    MailAddress,
    MailAnalysis,
    MailRecord,
    TrashRecord,
    Urgency,
)
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService

ADDRESS = MailAddress(name="Sender", address="sender@example.com")


def commands_service(
    config: MailFlowConfig | None = None, config_path: str | None = None
) -> MailFlowService:
    """Build a service with the given config (used by config-set tests)."""
    service = MailFlowService(
        config=config or MailFlowConfig(),
        registry=ComponentRegistry(),
        plugin_manager=cast(Any, FakePluginManager()),
        storage=cast(Any, MemoryStorage()),
        sources={},
        router=cast(LLMRouter, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )
    if config_path:
        service.config_path = Path(config_path)
    return service


class MemoryStorage:
    def __init__(self) -> None:
        self.mails: dict[str, MailRecord] = {}
        self.trash: dict[str, TrashRecord] = {}
        self.drafts: dict[str, ReplyDraft] = {}
        self.preferences: dict[str, str] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def save_mail(self, record: MailRecord) -> None:
        self.mails[record.record_id] = record

    async def get_mail(self, record_id: str) -> MailRecord | None:
        return self.mails.get(record_id)

    async def list_mails(self, limit: int | None = None) -> list[MailRecord]:
        return list(self.mails.values())[:limit]

    async def count_mails(self) -> int:
        return len(self.mails)

    async def set_manual_urgency(
        self, record_id: str, urgency: Urgency | None
    ) -> MailRecord | None:
        record = self.mails.get(record_id)
        if record is None:
            return None
        record.manual_urgency = urgency
        return record

    async def delete_mail(self, record_id: str) -> None:
        record = self.mails.pop(record_id, None)
        if record is not None:
            self.trash[record_id] = TrashRecord(
                record_id=record_id,
                mail=record.mail,
                auto_urgency=record.auto_urgency,
                manual_urgency=record.manual_urgency,
                analysis=record.analysis,
                processor_notes=record.processor_notes,
                deleted_at=datetime.now(UTC),
                expires_at=datetime.now(UTC),
            )

    async def list_trash(self) -> list[TrashRecord]:
        return list(self.trash.values())

    async def restore_from_trash(self, record_id: str) -> MailRecord | None:
        item = self.trash.pop(record_id, None)
        if item is None:
            return None
        record = item.to_mail_record()
        self.mails[record_id] = record
        return record

    async def purge_trash(self, before: datetime) -> int:
        return 0

    async def cleanup_mail(self, before: datetime) -> int:
        return 0

    async def save_draft(self, draft: ReplyDraft) -> None:
        self.drafts[draft.draft_id] = draft

    async def get_draft(self, draft_id: str) -> ReplyDraft | None:
        return self.drafts.get(draft_id)

    async def delete_draft(self, draft_id: str) -> None:
        self.drafts.pop(draft_id, None)

    async def get_preference(self, key: str) -> str | None:
        return self.preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value


class FakePluginManager:
    def snapshots(self, registry: ComponentRegistry) -> list[Any]:
        return []


def make_record(
    record_id: str = "m1",
    urgency: Urgency = Urgency.IMPORTANT,
    subject: str = "Exam on Friday",
    action: ActionItem | None = None,
) -> MailRecord:
    mail = MailMessage(
        message_id=record_id,
        account_id="acct-1",
        subject=subject,
        sender=ADDRESS,
        recipients=[],
        cc=[],
        date=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
        received_at=datetime(2026, 6, 1, 8, 5, tzinfo=UTC),
        body_text="Original body content",
        body_html="<p>Original body content</p>",
        provider="fake",
    )
    analysis = MailAnalysis(
        summary="Bring your student ID",
        urgency=urgency,
        reason="exam requires identification",
        reply_required=True,
        suggested_reply="I will attend.",
        action_items=[action] if action else [],
    )
    return MailRecord(record_id=record_id, mail=mail, auto_urgency=urgency, analysis=analysis)


@pytest.fixture
async def router() -> tuple[CommandRouter, MemoryStorage]:
    storage = MemoryStorage()
    await storage.save_mail(make_record())
    await storage.save_mail(make_record(record_id="m2", urgency=Urgency.AD, subject="Sale!"))
    await storage.save_mail(
        make_record(
            record_id="m3",
            urgency=Urgency.URGENT,
            subject="Exam",
            action=ActionItem(
                item_id="a1",
                mail_id="m3",
                summary="Final calculus exam",
                action_type="exam",
                due_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
                notes="Bring student ID and calculator",
            ),
        )
    )

    class NoopSource:
        async def run(self, emit: Any, stop_event: Any) -> None:
            await stop_event.wait()

        async def send_reply(self, mail_id: str, draft: ReplyDraft) -> None:
            pass

        async def close(self) -> None:
            pass

    service = MailFlowService(
        config=MailFlowConfig(),
        registry=ComponentRegistry(),
        plugin_manager=cast(Any, FakePluginManager()),
        storage=cast(Any, storage),
        sources={"acct-1": NoopSource()},
        router=cast(LLMRouter, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )
    commands = CommandRouter(service)
    return commands, storage


class TestCommandRouter:
    async def test_unknown_command(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, _ = router
        response = await commands.execute("bogus x")
        assert not response.ok
        assert "bogus" in response.text

    async def test_empty_line(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, _ = router
        response = await commands.execute("")
        assert response.text == ""

    async def test_help_lists_topics(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, _ = router
        response = await commands.execute("help")
        assert response.ok
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
            "runtime",
        ):
            assert topic in response.text

    async def test_help_topic(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, _ = router
        response = await commands.execute("help mail")
        assert response.ok
        assert "mail" in response.text

    async def test_mail_list(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, _ = router
        response = await commands.execute("mail list")
        assert response.ok
        assert "m1" in response.text
        assert "m3" in response.text
        assert "important" in response.text
        assert "urgent" in response.text

    async def test_mail_show_original_body(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, _ = router
        response = await commands.execute("mail show m1")
        assert response.ok
        assert "Bring your student ID" in response.text
        assert "Original body content" in response.text
        assert "sender@example.com" in response.text

    async def test_mail_show_unknown(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, _ = router
        response = await commands.execute("mail show ghost")
        assert not response.ok
        assert "ghost" in response.text

    async def test_mail_urgency_set_and_reset(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, storage = router
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
        assert "m2" in response.text
        response = await commands.execute("trash restore m2")
        assert response.ok
        assert "m2" in storage.mails
        assert "m2" not in storage.trash

    async def test_action_list_and_show(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, _ = router
        response = await commands.execute("action list")
        assert response.ok
        assert "a1" in response.text
        assert "exam" in response.text
        assert "Final calculus exam" in response.text
        assert "m3" in response.text  # source mail backlink
        response = await commands.execute("action show a1")
        assert response.ok
        assert "Bring student ID and calculator" in response.text

    async def test_plugin_account_llm_adapter_commands(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, _ = router
        assert (await commands.execute("plugin list")).ok
        assert (await commands.execute("adapter list")).ok
        assert (await commands.execute("account list")).ok
        assert (await commands.execute("llm list")).ok
        assert (await commands.execute("llm bindings")).ok
        assert (await commands.execute("runtime")).ok

    async def test_reply_flow_through_commands(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, storage = router
        created = await commands.execute("reply create m1")
        assert created.ok
        draft_id = created.text.split()[1]
        edited = await commands.execute(f"reply edit {draft_id} 'Re: Exam' 'I will attend.'")
        assert edited.ok
        prepared = await commands.execute(f"reply prepare {draft_id}")
        assert prepared.ok
        token = prepared.text.split("token: ")[-1].split(" ")[0]
        confirmed = await commands.execute(f"reply confirm {draft_id} {token}")
        assert confirmed.ok
        assert storage.drafts[draft_id].state.value == "sent"
        # wrong token rejected
        response = await commands.execute(f"reply confirm {draft_id} wrong")
        assert not response.ok

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

    async def test_config_get(self, router: tuple[CommandRouter, MemoryStorage]) -> None:
        commands, _ = router
        response = await commands.execute("config get general.reminder_hour")
        assert response.ok
        assert "8" in response.text
        response = await commands.execute("config get no.such.key")
        assert not response.ok

    async def test_config_set_persists_to_file(self, tmp_path: Path) -> None:
        from mailflow.config import MailFlowConfig

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

    async def test_config_set_requires_config_file(
        self, router: tuple[CommandRouter, MemoryStorage]
    ) -> None:
        commands, _ = router
        response = await commands.execute("config set general.reminder_hour 9")
        assert not response.ok
        assert "config file" in response.text

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
        from mailflow.plugins import PluginInfo

        service = commands_service(MailFlowConfig(), config_path=str(tmp_path / "config.toml"))
        service.plugin_manager = cast(Any, FakePluginManager())

        class KnownManager(FakePluginManager):
            def enabled_infos(self):
                return [PluginInfo(plugin_id="fake-plugin")]

        service.plugin_manager = cast(Any, KnownManager())
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
        commands, _ = router
        response = await commands.execute("plugin uninstall ghost")
        assert not response.ok
        assert "not found in any repository" in response.text
