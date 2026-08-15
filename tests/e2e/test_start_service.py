"""End-to-end tests exercising only the public ``start_service`` entry point.

Deterministic components are registered through the normal Pluggy hooks:
the fake mail source and LLM backends come from a test plugin module, while
the real rules/llm-importance/sqlite/console-notify plugins provide the rest.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest
from mailflow.commands import CommandRouter
from mailflow.config import (
    LLMConfig,
    MailAccountConfig,
    MailFlowConfig,
    NotifierConfig,
)
from mailflow.contracts import LLMCompletion, MailSource
from mailflow.domain import MailRecord, Urgency
from mailflow.plugins import PluginInfo, PluginManager
from mailflow.registry import PluginRegistrar
from mailflow.service import MailFlowService, start_service
from mailflow_notify_console.plugin import plugin as notify_plugin
from mailflow_processor_llm_importance.plugin import plugin as llm_processor_plugin
from mailflow_processor_rules.plugin import plugin as rules_plugin
from mailflow_storage_sqlite.plugin import plugin as storage_plugin
from mailflow_testkit.fakes import FakeMailSource, make_mail

EXAM_JSON = """{
  "summary": "Final calculus exam on June 10 at 09:00, bring student ID",
  "urgency": "urgent",
  "reason": "mandatory exam requiring preparation",
  "reply_required": false,
  "suggested_reply": "",
  "action_items": [
    {
      "summary": "Attend final calculus exam",
      "action_type": "exam",
      "due_at": "2026-06-10T09:00:00+00:00",
      "due_end": "2026-06-10T11:00:00+00:00",
      "notes": "Bring student ID"
    }
  ],
  "notes": ""
}"""


class CapturingLLM:
    """Serves the exam JSON; fails when configured as the primary."""

    backend_id = "test-llm"

    def __init__(self, *, fail: bool = False, captured: list[Any]) -> None:
        self.fail = fail
        self.captured = captured

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMCompletion:
        self.captured.append(messages)
        if self.fail:
            raise RuntimeError("primary llm down")
        return LLMCompletion(text=EXAM_JSON, model="exam-model")


class CapturingNotifier:
    def __init__(self, notified: list[MailRecord]) -> None:
        self.notified = notified

    async def notify(self, record: MailRecord) -> None:
        self.notified.append(record)


class E2EPlugin:
    """Registers the deterministic source, LLM and notifier components."""

    def __init__(self, captured: list[Any], notified: list[MailRecord]) -> None:
        self.captured = captured
        self.notified = notified

    def mailflow_plugin_info(self) -> PluginInfo:
        return PluginInfo(
            plugin_id="mailflow-e2e-test",
            name="E2E Test Components",
            version="0.0.0",
            description="deterministic source/llm/notifier for end-to-end tests",
        )

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        def source_factory(account: MailAccountConfig) -> MailSource:
            mail = make_mail(
                message_id="e2e-1",
                account_id=account.account_id,
                subject="Final calculus exam",
                body_text="The final exam is on June 10 at 09:00. Bring your student ID.",
            )
            return FakeMailSource([mail])

        def llm_factory(llm_config: LLMConfig) -> CapturingLLM:
            return CapturingLLM(fail=(llm_config.llm_id == "primary"), captured=self.captured)

        def notifier_factory(notifier_config: NotifierConfig) -> CapturingNotifier:
            return CapturingNotifier(self.notified)

        registrar.add_source("test-source", source_factory)
        registrar.add_llm("test-llm", llm_factory)
        registrar.add_notifier("test-capture", notifier_factory)


def build_config(db_path: Path) -> MailFlowConfig:
    return MailFlowConfig.model_validate(
        {
            "general": {"timezone": "UTC", "workers": 2},
            "storage": {"provider": "sqlite", "path": str(db_path)},
            "accounts": [
                {"account_id": "acct-1", "provider": "test-source", "email": "me@example.com"}
            ],
            "llms": [
                {"llm_id": "primary", "provider": "test-llm", "model": "m1"},
                {"llm_id": "backup", "provider": "test-llm", "model": "m2"},
            ],
            "processors": [
                {"processor_id": "rules", "provider": "rules", "priority": 10},
                {
                    "processor_id": "llm-importance",
                    "provider": "llm-importance",
                    "priority": 20,
                    "llm": "primary",
                    "fallback_llms": ["backup"],
                },
            ],
            "notifiers": [
                {"notifier_id": "capture", "provider": "test-capture", "minimum_urgency": "info"},
                {"notifier_id": "console", "provider": "console", "minimum_urgency": "important"},
            ],
            "logging": {"console": False, "file": False, "jsonl": False},
        }
    )


async def wait_until(predicate: Any, timeout_seconds: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met within timeout")


@pytest.mark.asyncio
async def test_full_service_flow(tmp_path: Path) -> None:
    captured: list[Any] = []
    notified: list[MailRecord] = []
    manager = PluginManager(build_config(tmp_path / "unused.db"))
    e2e_plugin = E2EPlugin(captured, notified)
    for plugin in (
        e2e_plugin,
        rules_plugin,
        llm_processor_plugin,
        storage_plugin,
        notify_plugin,
    ):
        assert manager.register(plugin) is not None

    service: MailFlowService | None = None
    try:
        service = await start_service(
            build_config(tmp_path / "e2e.db"),
            plugin_manager=manager,
            discover_plugins=False,
            enable_logging=False,
        )
        assert service.started is True
        CommandRouter(service)

        # -- pipeline: fake source -> queue -> rules+llm -> storage -------------

        async def mails_processed() -> bool:
            return await service.count_mails() == 1

        await wait_until(mails_processed)
        record = await service.get_mail("e2e-1")
        assert record is not None
        assert record.analysis is not None
        assert record.analysis.urgency is Urgency.URGENT
        assert record.analysis.summary.startswith("Final calculus exam")
        assert record.analysis.backend == "test-llm"
        assert len(record.action_items) == 1
        item = record.action_items[0]
        assert item.action_type == "exam"
        assert item.notes == "Bring student ID"
        assert item.mail_id == "e2e-1"
        assert item.due_at.tzinfo is not None

        # -- llm fallback: primary failed, backup handled -----------------------
        # exactly two chat calls for one mail: primary attempt + backup success
        assert len(captured) == 2

        # -- notifier invoked with the computed analysis ------------------------
        await wait_until(lambda: len(notified) == 1)
        assert notified[0].record_id == "e2e-1"

        # -- snapshot maps components to their providing plugins ----------------
        snapshot = service.snapshot()
        assert any(p.plugin_id == "mailflow-e2e-test" for p in snapshot.plugins)
        account = next(a for a in snapshot.accounts if a.account_id == "acct-1")
        assert account.provider == "test-source"
        binding = next(b for b in snapshot.processors if b.processor_id == "llm-importance")
        assert binding.plugin_id == "mailflow-processor-llm-importance"
        assert binding.llm_id == "primary"
        assert binding.fallback_llm_ids == ["backup"]
        llm_snapshot = next(item for item in snapshot.llms if item.llm_id == "primary")
        assert llm_snapshot.backend == "test-llm"

        # -- manual urgency override and reset restores automatic ----------------
        changed = await service.set_mail_urgency("e2e-1", Urgency.AD)
        assert changed is not None
        assert changed.effective_urgency is Urgency.AD
        assert changed.auto_urgency is Urgency.URGENT  # automatic preserved
        reset = await service.set_mail_urgency("e2e-1", None)
        assert reset is not None
        assert reset.effective_urgency is Urgency.URGENT  # reset restores automatic

        # -- reply workflow: prepare + confirm calls the matching source --------
        draft = await service.create_reply("e2e-1")
        with pytest.raises(PermissionError):
            await service.confirm_reply(draft.draft_id, "wrong-token")
        prepared = await service.prepare_reply(draft.draft_id)
        assert prepared.token is not None
        confirmed = await service.confirm_reply(draft.draft_id, prepared.token)
        assert confirmed.state.value == "sent"
        source: FakeMailSource = service.sources["acct-1"]  # type: ignore[assignment]
        assert len(source.sent_replies) == 1
        assert source.sent_replies[0][0] == "e2e-1"

        # -- command router shows mail with summary and original body ------------
        assert service.commands is not None
        response = await service.commands.execute("mail list")
        assert response.ok
        assert "e2e-1" in response.text
        show = await service.commands.execute("mail show e2e-1")
        assert show.ok
        assert "Final calculus exam" in show.text  # summary
        assert "Bring your student ID" in show.text  # original body
        actions = await service.commands.execute("action list")
        assert actions.ok
        assert "exam" in actions.text

        # -- language switch persists to storage --------------------------------
        await service.set_language("zh-CN")
        assert await service.get_language() == "zh-CN"
        assert await service.storage.get_preference("language") == "zh-CN"

        # -- trash/restore -------------------------------------------------------
        assert await service.delete_mail("e2e-1") is True
        assert await service.get_mail("e2e-1") is None
        trash = await service.list_trash()
        assert len(trash) == 1
        restored = await service.restore_mail("e2e-1")
        assert restored is not None
        assert restored.record_id == "e2e-1"
        assert await service.get_mail("e2e-1") is not None
    finally:
        if service is not None:
            await service.stop()


@pytest.mark.asyncio
async def test_language_persists_across_restart(tmp_path: Path) -> None:
    """The chosen language survives a full stop/start cycle on the same db."""
    captured: list[Any] = []
    notified: list[MailRecord] = []
    manager = PluginManager(build_config(tmp_path / "unused.db"))
    e2e_plugin = E2EPlugin(captured, notified)
    for plugin in (e2e_plugin, rules_plugin, llm_processor_plugin, storage_plugin, notify_plugin):
        assert manager.register(plugin) is not None

    config = build_config(tmp_path / "persist.db")
    first = await start_service(
        config, plugin_manager=manager, discover_plugins=False, enable_logging=False
    )
    try:
        await first.set_language("zh-CN")
    finally:
        await first.stop()

    second = await start_service(
        config, plugin_manager=manager, discover_plugins=False, enable_logging=False
    )
    try:
        assert await second.get_language() == "zh-CN"
    finally:
        await second.stop()
