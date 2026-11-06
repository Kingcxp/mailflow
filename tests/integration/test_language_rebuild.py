"""Interface language changes affect generated content and explicit failed-mail retries."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mailflow.config import LLMConfig, MailFlowConfig, ProcessorConfig
from mailflow.contracts import LLMCompletion
from mailflow.domain import MailAnalysis, MailRecord, Urgency
from mailflow.mail_query import MailQuery
from mailflow.plugin_api import define_plugin
from mailflow.plugins import PluginManager
from mailflow.service import MailFlowService, start_service
from mailflow_storage_sqlite.plugin import plugin as storage_plugin
from mailflow_testkit.fakes import make_mail
from mailflow_tui.labels import analysis_status


class Model:
    def __init__(self) -> None:
        self.languages: list[str] = []
        self.fail = False
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages: list[dict[str, Any]]) -> LLMCompletion:
        prompt = "\n".join(str(message.get("content", "")) for message in messages)
        match = re.search(r"language:\s*(zh-CN|en)\b", prompt)
        assert match is not None, "Generated content must request the current language"
        language = match.group(1)
        self.languages.append(language)
        if self.block:
            self.entered.set()
            await self.release.wait()
        if self.fail:
            raise RuntimeError("fake model unavailable")
        summary, reason, reply, action = (
            ("请提交报告", "需要处理", "好的，我会提交。", "提交报告")
            if language == "zh-CN"
            else (
                "Please submit the report",
                "Action required",
                "I will submit it.",
                "Submit report",
            )
        )
        return LLMCompletion(
            text=json.dumps(
                {
                    "summary": summary,
                    "reason": reason,
                    "urgency": "important",
                    "suggested_reply": reply,
                    "action_items": [
                        {
                            "summary": action,
                            "action_type": "errand",
                            "due_at": "2099-01-01T09:00:00+00:00",
                            "must_do": True,
                        }
                    ],
                }
            )
        )


async def open_service(
    model: Model, db: Path, *, config_path: Path | None = None
) -> MailFlowService:
    class Backend:
        backend_id = "language-test"

        def __init__(self, config: LLMConfig) -> None:
            pass

        async def chat(
            self,
            messages: list[dict[str, Any]],
            *,
            temperature: Any = None,
            options: Any = None,
            tools: Any = None,
        ) -> LLMCompletion:
            return await model.complete(messages)

    config = MailFlowConfig()
    config.storage.path = str(db)
    config.general.summary_language = "en"  # Legacy overrides must not freeze output in English.
    config.llms = [LLMConfig(llm_id="main", provider="language-test", model="local")]
    config.processors = [
        ProcessorConfig(
            processor_id="llm-importance",
            provider="llm-importance",
            llm="main",
            options={"language": "en"},
            retries=0,
        )
    ]
    plugin = define_plugin("mailflow-language-test")
    plugin.llm("language-test")(Backend)
    manager = PluginManager(config)
    manager.register(plugin.build())
    manager.register(storage_plugin)
    return await start_service(
        config,
        config_path=config_path,
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
        background=False,
    )


@pytest.mark.parametrize("entry", ["settings", "language_command", "config_command"])
async def test_language_switch_marks_old_results_failed_until_explicit_retry(
    tmp_path: Path,
    entry: str,
) -> None:
    model = Model()
    path = tmp_path / "config.toml" if entry != "language_command" else None
    service = await open_service(model, tmp_path / "language.db", config_path=path)
    try:
        record = await service.process_mail(make_mail())
        assert record is not None and record.analysis is not None
        assert record.analysis.language == "en" and record.analysis_state == "ready"
        old_analysis = record.analysis.model_copy(deep=True)
        old_action = old_analysis.action_items[0]
        old_action.completed_at = datetime(2026, 11, 1, tzinfo=UTC)
        old_action.reminder_enabled = False
        old_action.snoozed_until = datetime(2099, 1, 1, tzinfo=UTC)
        await service.storage.save_mail(record.model_copy(update={"analysis": old_analysis}))
        await service.set_mail_urgency(record.record_id, Urgency.URGENT)
        if entry == "settings":
            await service.set_setting("general.language", "zh-CN")
        elif entry == "config_command":
            await service.set_config_value("general.language", "zh-CN")
        else:
            await service.set_language("zh-CN")
        assert await service.get_language() == service.config.general.language == "zh-CN"
        assert model.languages == ["en"]  # Switching never schedules analysis.
        failed = await service.list_failed_mails()
        assert [mail.record_id for mail in failed] == [record.record_id]
        assert (await service.query_mails(MailQuery(state="failed"))).total == 1
        assert failed[0].analysis == old_analysis
        assert failed[0].analysis_state == "failed"
        assert analysis_status(service, failed[0]) == service.t("mail.analysis_language_changed")
        assert service.t("mail.analysis_language_changed") != "mail.analysis_language_changed"
        assert (await service.create_reply(record.record_id)).body == ""

        model.fail = True
        retained = await service.process_mail(failed[0].mail, force=True)
        assert retained is not None and retained.analysis == old_analysis
        assert retained.analysis_state == "failed" and retained.analysis_error_code
        model.fail = False
        for mail in await service.list_failed_mails():
            await service.process_mail(mail.mail, force=True)
        updated = await service.get_mail(record.record_id)
        assert updated is not None and updated.analysis is not None
        assert updated.analysis.language == "zh-CN" and updated.analysis.summary == "请提交报告"
        assert (
            updated.analysis.reason == "需要处理"
            and updated.analysis.suggested_reply == "好的，我会提交。"
        )
        action = updated.action_items[0]
        assert action.summary == "提交报告" and action.item_id == old_action.item_id
        assert action.completed_at == old_action.completed_at
        assert action.snoozed_until == old_action.snoozed_until and not action.reminder_enabled
        assert action.reminder_key == old_action.reminder_key
        assert updated.manual_urgency == Urgency.URGENT
        assert updated.analysis_state == "ready" and updated.analysis_error_code == ""
        assert await service.list_failed_mails() == []
        assert (await service.query_mails(MailQuery(state="failed"))).total == 0
    finally:
        await service.stop()


async def test_startup_and_trash_restore_keep_language_failures_without_auto_analysis(
    tmp_path: Path,
) -> None:
    model = Model()
    db = tmp_path / "language.db"
    service = await open_service(model, db)
    try:
        generated = await service.process_mail(make_mail())
        assert generated is not None
        await service.delete_mail(generated.record_id)
        legacy = MailRecord(
            record_id="legacy",
            mail=make_mail(message_id="legacy"),
            analysis=MailAnalysis(summary="Old unlabelled summary", urgency=Urgency.INFO),
        )
        await service.storage.save_mail(legacy)
        await service.set_language("zh-CN")
    finally:
        await service.stop()

    service = await open_service(model, db)
    try:
        assert await service.get_language() == service.config.general.language == "zh-CN"
        assert model.languages == ["en"]
        failed = await service.list_failed_mails()
        assert [record.record_id for record in failed] == ["legacy"]
        assert failed[0].analysis == legacy.analysis
        restored = await service.restore_mail(generated.record_id)
        assert restored is not None and restored.analysis_state == "failed"
        assert restored.analysis_error_code == "mail.analysis_language_changed"
        await service.delete_mail(restored.record_id)
        trash = await service.list_trash()
        assert trash[0].to_mail_record().analysis_error_code == restored.analysis_error_code
        await service.restore_mail(restored.record_id)
        for record in await service.list_failed_mails():
            await service.process_mail(record.mail, force=True)
        assert await service.list_failed_mails() == []
        assert model.languages == ["en", "zh-CN", "zh-CN"]
    finally:
        await service.stop()


async def test_old_language_analysis_finishing_after_switch_stays_retryable(tmp_path: Path) -> None:
    model = Model()
    model.block = True
    service = await open_service(model, tmp_path / "language.db")
    task = asyncio.create_task(service.process_mail(make_mail()))
    try:
        await asyncio.wait_for(model.entered.wait(), timeout=2)
        await service.set_setting("general.language", "zh-CN")
        assert not task.done()  # Ordinary language settings do not cancel an active request.
        model.release.set()
        record = await asyncio.wait_for(task, timeout=2)
        assert record is not None and record.analysis is not None
        assert record.analysis.language == "en" and record.analysis_state == "failed"
        assert [mail.record_id for mail in await service.list_failed_mails()] == [record.record_id]
        model.block = False
        updated = await service.process_mail(record.mail, force=True)
        assert updated is not None and updated.analysis is not None
        assert updated.analysis.language == "zh-CN" and updated.analysis_state == "ready"
    finally:
        model.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.stop()
