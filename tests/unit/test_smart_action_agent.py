"""The smart-action loop: bounded round trips, staged writes, cancellation.

The model is scripted turn by turn; what is under test is the loop itself —
that a tool result reaches the next turn, that nothing mutating happens
before confirmation, that a runaway model is stopped, and that cancellation
propagates instead of being swallowed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMCompletion, LLMRouter, MailMessage, ToolCall
from mailflow.domain import ActionItem, MailAddress, MailRecord, Urgency
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService
from mailflow.tools import (  # pyright: ignore[reportPrivateUsage]
    _MAX_TOOL_STEPS,  # pyright: ignore[reportPrivateUsage]
    _explicit_delete_intent,  # pyright: ignore[reportPrivateUsage]
    run_agent,
)

ADDRESS = MailAddress(name="Sender", address="sender@example.com")


class MemoryStorage:
    def __init__(self) -> None:
        self.mails: dict[str, MailRecord] = {}
        self.preferences: dict[str, str] = {}
        self.custom_actions: dict[str, ActionItem] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def save_mail(self, record: MailRecord) -> None:
        self.mails[record.record_id] = record

    async def get_mail(self, record_id: str) -> MailRecord | None:
        return self.mails.get(record_id)

    async def list_mails(self, limit: int | None = None) -> list[MailRecord]:
        ordered = sorted(self.mails.values(), key=lambda r: r.mail.received_at, reverse=True)
        return ordered if limit is None else ordered[:limit]

    async def count_mails(self) -> int:
        return len(self.mails)

    async def delete_mail(self, record_id: str, *, refresh_deleted_at: bool = False) -> None:
        self.mails.pop(record_id, None)

    async def get_preference(self, key: str) -> str | None:
        return self.preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value

    async def save_custom_action(self, item: ActionItem) -> None:
        self.custom_actions[item.item_id] = item

    async def list_custom_actions(self) -> list[ActionItem]:
        return list(self.custom_actions.values())

    async def delete_custom_action(self, item_id: str) -> bool:
        return self.custom_actions.pop(item_id, None) is not None


def _mail(record_id: str, *, subject: str, body: str) -> MailMessage:
    when = datetime(2026, 1, 1, 9, tzinfo=UTC)
    return MailMessage(
        message_id=record_id,
        account_id="acct-1",
        subject=subject,
        sender=ADDRESS,
        recipients=[],
        cc=[],
        date=when,
        received_at=when,
        body_text=body,
        body_html=f"<p>{body}</p>",
        provider="fake",
    )


def make_service(router: Any) -> MailFlowService:
    config = MailFlowConfig()
    config.llms = [LLMConfig(llm_id="llm-1")]
    return MailFlowService(
        config=config,
        registry=ComponentRegistry(),
        plugin_manager=cast(Any, None),
        storage=cast(Any, MemoryStorage()),
        sources={},
        router=cast(LLMRouter, router),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )


async def seed(service: MailFlowService) -> None:
    storage = cast(Any, service.storage)
    await storage.save_mail(
        MailRecord(
            record_id="seminar-1",
            mail=_mail("seminar-1", subject="Seminar invitation", body="A seminar on Friday"),
            auto_urgency=Urgency.INFO,
        )
    )
    await storage.save_mail(
        MailRecord(
            record_id="invoice-1",
            mail=_mail("invoice-1", subject="Invoice", body="Please pay the invoice"),
            auto_urgency=Urgency.IMPORTANT,
        )
    )


def call(
    name: str, arguments: dict[str, Any] | None = None, *, call_id: str = "c1"
) -> LLMCompletion:
    """One model turn that asks for a tool."""
    return LLMCompletion(
        text="", tool_calls=[ToolCall(call_id=call_id, name=name, arguments=arguments or {})]
    )


def text(value: str) -> LLMCompletion:
    """One model turn that answers in prose (the loop's exit)."""
    return LLMCompletion(text=value)


class ScriptedRouter:
    """Replays scripted turns and records the conversation it was shown."""

    def __init__(self, turns: list[LLMCompletion]) -> None:
        self.turns = list(turns)
        self.calls: list[list[dict[str, Any]]] = []
        self.tool_arguments: list[Any] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCompletion:
        self.calls.append(list(messages))
        self.tool_arguments.append(kwargs.get("tools"))
        if not self.turns:
            return text("nothing left to do")
        return self.turns.pop(0)


class TestReadLoop:
    async def test_tool_result_reaches_the_next_turn_and_the_result(self) -> None:
        router = ScriptedRouter(
            [
                call("find_mail", {"contains": "seminar"}),
                text("Found the seminar invitation."),
            ]
        )
        service = make_service(router)
        await seed(service)

        result = await run_agent(service, cast(Any, router), "show me the seminar mail")

        assert result.tool_steps == ['find_mail {"contains": "seminar"}']
        assert result.final_text == "Found the seminar invitation."
        assert result.pending == []
        assert [record.record_id for record in result.records] == ["seminar-1"]
        assert result.total_mails == 2
        # the second turn really carried the tool result back
        second_turn = router.calls[1]
        assert second_turn[-1]["role"] == "tool"
        assert second_turn[-1]["tool_call_id"] == "c1"
        assert "seminar-1" in second_turn[-1]["content"]
        # the assistant turn replays in the OpenAI wire shape
        assert second_turn[-2]["tool_calls"][0]["function"]["name"] == "find_mail"
        # and every turn was offered the tool schemas
        assert all(specs for specs in router.tool_arguments)

    async def test_results_include_every_match_in_source_order(self) -> None:
        router = ScriptedRouter([call("find_mail", {"contains": "seminar"}), text("Found all.")])
        service = make_service(router)
        storage = cast(Any, service.storage)
        for index in range(45):
            when = datetime(2026, 1, 1, 9, index, tzinfo=UTC)
            mail = _mail(f"seminar-{index}", subject=f"Seminar {index}", body="Event")
            mail = mail.model_copy(update={"date": when, "received_at": when})
            await storage.save_mail(
                MailRecord(record_id=f"seminar-{index}", mail=mail, auto_urgency=Urgency.INFO)
            )

        result = await run_agent(service, cast(Any, router), "show all seminar mail")

        assert len(result.records) == 45
        assert [record.record_id for record in result.records] == [
            f"seminar-{index}" for index in reversed(range(45))
        ]
        assert "matched: 45 mail(s)" in router.calls[1][-1]["content"]

    async def test_delete_tool_attempt_without_explicit_intent_is_rejected(self) -> None:
        router = ScriptedRouter(
            [call("delete_mail", {"record_ids": ["invoice-1"]}), text("No deletion staged.")]
        )
        service = make_service(router)
        await seed(service)

        result = await run_agent(service, cast(Any, router), "show me every invoice email")

        assert result.pending == []
        assert await service.count_mails() == 2
        assert router.calls[1][-1]["content"] == (
            "error: mail deletion requires an explicit delete request"
        )

    async def test_several_tool_calls_in_one_turn_all_run(self) -> None:
        router = ScriptedRouter(
            [
                LLMCompletion(
                    text="",
                    tool_calls=[
                        ToolCall(call_id="c1", name="find_mail", arguments={"contains": "seminar"}),
                        ToolCall(call_id="c2", name="list_actions", arguments={}),
                    ],
                ),
                text("Both done."),
            ]
        )
        service = make_service(router)
        await seed(service)

        result = await run_agent(service, cast(Any, router), "seminar mail and my schedule")

        assert result.tool_steps == [
            'find_mail {"contains": "seminar"}',
            "list_actions {}",
        ]
        roles = [m["role"] for m in router.calls[1]]
        assert roles.count("tool") == 2

    async def test_a_tool_error_is_fed_back_and_the_loop_continues(self) -> None:
        router = ScriptedRouter(
            [
                call("delete_mail", {"record_ids": ["ghost"]}),
                call("find_mail", {"contains": "seminar"}, call_id="c2"),
                text("Recovered."),
            ]
        )
        service = make_service(router)
        await seed(service)

        result = await run_agent(service, cast(Any, router), "delete the ghost mail")

        assert result.final_text == "Recovered."
        assert result.pending == []
        error_turn = router.calls[1][-1]
        assert error_turn["role"] == "tool"
        assert error_turn["content"].startswith("error:")
        assert await service.count_mails() == 2

    async def test_unknown_tool_is_reported_without_stopping(self) -> None:
        router = ScriptedRouter([call("teleport"), text("I cannot do that.")])
        service = make_service(router)
        await seed(service)

        result = await run_agent(service, cast(Any, router), "teleport the mail")

        assert result.final_text == "I cannot do that."
        assert router.calls[1][-1]["content"] == "unknown tool: teleport"


@pytest.mark.parametrize(
    ("instruction", "allowed"),
    [
        ("find all seminar emails", False),
        ("clean up old email", False),
        ("move the invoice to trash", True),
        ("删除匹配的邮件", True),
        ("把邮件删掉", True),
        ("把邮件移入回收站", True),
        ("do not delete these messages", False),
        ("不要删除这些邮件", False),
    ],
)
def test_explicit_delete_intent_uses_only_approved_terms(instruction: str, allowed: bool) -> None:
    assert _explicit_delete_intent(instruction) is allowed


class TestStagedWrites:
    async def test_delete_is_only_staged_and_storage_is_untouched(self) -> None:
        router = ScriptedRouter(
            [
                call("find_mail", {"contains": "invoice"}),
                call("delete_mail", {"record_ids": ["invoice-1"]}, call_id="c2"),
                text("Staged the invoice mail."),
            ]
        )
        service = make_service(router)
        await seed(service)

        result = await run_agent(service, cast(Any, router), "delete the invoice mail")

        assert [pending.tool for pending in result.pending] == ["delete_mail"]
        assert result.pending[0].record_ids == ["invoice-1"]
        assert result.pending[0].summary_key
        assert await service.count_mails() == 2  # the model never deleted anything
        assert await service.list_actions_all() == []

    async def test_non_delete_instruction_cannot_stage_mail_deletion(self) -> None:
        router = ScriptedRouter(
            [
                call("find_mail", {"contains": "invoice"}),
                call("delete_mail", {"record_ids": ["invoice-1"]}, call_id="c2"),
                text("I found the invoice mail."),
            ]
        )
        service = make_service(router)
        await seed(service)

        result = await run_agent(service, cast(Any, router), "find the invoice mail")

        assert result.pending == []
        assert await service.count_mails() == 2
        assert (
            "error: mail deletion requires an explicit delete request"
            in router.calls[2][-1]["content"]
        )

    async def test_schedule_event_is_staged_with_a_future_time(self) -> None:
        router = ScriptedRouter(
            [
                call(
                    "schedule_event",
                    {"title": "Colloquium", "starts_at": "2099-10-15T14:00:00+08:00"},
                ),
                text("Staged it."),
            ]
        )
        service = make_service(router)

        result = await run_agent(service, cast(Any, router), "add the colloquium")

        assert len(result.pending) == 1
        assert result.pending[0].tool == "schedule_event"
        assert result.pending[0].arguments["starts_at"] == "2099-10-15T06:00:00+00:00"
        assert await service.list_actions_all() == []


class TestLoopBounds:
    async def test_a_runaway_model_stops_at_the_step_bound(self) -> None:
        router = ScriptedRouter([call("list_actions", {}, call_id=f"c{i}") for i in range(50)])
        service = make_service(router)

        result = await run_agent(service, cast(Any, router), "loop forever")

        assert len(router.calls) == _MAX_TOOL_STEPS
        assert len(result.tool_steps) == _MAX_TOOL_STEPS
        # the user is told the loop stopped rather than shown an empty answer
        assert "12" in result.final_text

    async def test_progress_reports_ordered_bounded_phases(self) -> None:
        router = ScriptedRouter([call("find_mail", {"contains": "seminar"}), text("Done.")])
        service = make_service(router)
        await seed(service)
        reports: list[tuple[str, int, int, Any]] = []

        def record(stage: str, done: int, total: int, detail: Any) -> None:
            reports.append((stage, done, total, detail))

        await run_agent(service, cast(Any, router), "seminar mail", progress=record)

        assert [stage for stage, *_ in reports] == [
            "llm",
            "tool",
            "tool_done",
            "llm",
            "finalizing",
        ]
        assert all(total == _MAX_TOOL_STEPS for _, _, total, _ in reports)
        assert reports[0][3] == ("smart_llm", {"step": 1})
        key, params = reports[1][3]
        assert key == "smart_tool"
        assert params["tool"] == "find_mail"
        assert len(params["args"]) <= 240
        key, params = reports[2][3]
        assert key == "smart_tool_done"
        assert params["tool"] == "find_mail"
        assert len(params["result"]) <= 240
        assert reports[-1][3] == ("smart_action_finalizing", {})

    async def test_a_failing_progress_callback_does_not_abort_the_work(self) -> None:
        router = ScriptedRouter([call("find_mail", {"contains": "seminar"}), text("Done.")])
        service = make_service(router)
        await seed(service)

        def broken(stage: str, done: int, total: int, detail: Any) -> None:
            raise RuntimeError("the host blew up while drawing")

        result = await run_agent(service, cast(Any, router), "seminar mail", progress=broken)

        assert result.final_text == "Done."


class TestCancellation:
    async def test_cancelling_mid_loop_propagates(self) -> None:
        class BlockingRouter:
            def __init__(self) -> None:
                self.started = asyncio.Event()

            async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCompletion:
                self.started.set()
                await asyncio.sleep(30)  # the test cancels long before this
                return text("never")

        router = BlockingRouter()
        service = make_service(router)
        task = asyncio.create_task(run_agent(service, cast(Any, router), "anything"))
        await asyncio.wait_for(router.started.wait(), timeout=5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_service_smart_action_cancels_the_same_way(self) -> None:
        class BlockingRouter:
            def __init__(self) -> None:
                self.started = asyncio.Event()

            async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCompletion:
                self.started.set()
                await asyncio.sleep(30)
                return text("never")

        router = BlockingRouter()
        service = make_service(router)
        task = asyncio.create_task(service.smart_action("do something"))
        await asyncio.wait_for(router.started.wait(), timeout=5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
