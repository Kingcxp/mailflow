"""Dismissal semantics: deleting a mail-derived todo hides it permanently,
even across re-analysis; overdue tasks remain until the user completes or deletes them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from mailflow.service import MailFlowService

import pytest
from mailflow.config import MailFlowConfig
from mailflow.domain import (
    ActionItem,
    ActionOrigin,
    MailAddress,
    MailMessage,
    MailRecord,
    action_time_window,
    actions_duplicate,
    dedupe_actions,
)
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.plugins import PluginManager
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService


class _Store:
    def __init__(self) -> None:
        self.mails: dict[str, Any] = {}
        self.custom: dict[str, ActionItem] = {}
        self.preferences: dict[str, str] = {}

    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
    async def get_preference(self, k: str) -> str | None:
        return self.preferences.get(k)

    async def set_preference(self, k: str, v: str) -> None:
        self.preferences[k] = v

    async def list_mails(self) -> list[Any]:
        return list(self.mails.values())

    async def list_custom_actions(self) -> list[ActionItem]:
        return list(self.custom.values())

    async def save_custom_action(self, item: ActionItem) -> None:
        self.custom[item.item_id] = item

    async def delete_custom_action(self, item_id: str) -> bool:
        return self.custom.pop(item_id, None) is not None


@pytest.fixture
def service() -> MailFlowService:

    svc = MailFlowService(
        config=MailFlowConfig(),
        registry=ComponentRegistry(),
        plugin_manager=PluginManager(),
        storage=cast(Any, _Store()),
        sources={},
        router=cast(Any, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )
    return svc


_DUE = datetime(2097, 11, 2, 6, 0, tzinfo=UTC)


def _mail_item(item_id: str) -> ActionItem:
    """Same natural identity (mail id + due time + type) for every id —
    re-analysis of the same mail reproduces the due time, and Linux clock
    resolution would otherwise make the two now() calls differ."""
    return ActionItem(
        item_id=item_id,
        mail_id="m1",
        summary="领学生证",
        action_type="errand",
        due_at=_DUE,
    )


async def test_mail_todo_delete_hides_across_reanalysis(service: MailFlowService) -> None:
    store = cast(Any, service.storage)
    # first analysis produced this item (id A)
    original = _mail_item("aaa")
    record = cast(Any, type("R", (), {"action_items": [original]}))
    store.mails["m1"] = record
    assert len(await service.list_actions()) == 1

    # user deletes it → hidden by natural key
    assert await service.delete_action("aaa") is True
    assert await service.list_actions() == []

    # re-analysis generates a replacement with a NEW id but same identity
    replacement = _mail_item("bbb")
    store.mails["m1"] = cast(Any, type("R", (), {"action_items": [replacement]}))
    assert await service.list_actions() == []


async def test_bulk_delete_reports_what_really_moved(service: MailFlowService) -> None:
    """A confirmed bulk delete counts real moves: an id that is already gone
    must not be reported as deleted, and must not abort the rest."""
    store = cast(Any, service.storage)

    async def delete_mail(record_id: str, **kwargs: Any) -> None:
        store.mails.pop(record_id, None)

    async def get_mail(record_id: str) -> Any:
        return store.mails.get(record_id)

    store.delete_mail = delete_mail
    store.get_mail = get_mail
    for record_id in ("m1", "m2"):
        store.mails[record_id] = MailRecord(
            record_id=record_id,
            received_at=_DUE,
            mail=MailMessage(
                message_id=record_id,
                account_id="a",
                subject="mail",
                sender=MailAddress(address="sender@example.com"),
                date=_DUE,
                received_at=_DUE,
            ),
        )

    # m-missing is gone; m1 and m2 must still be removed
    assert await service.delete_mails(["m1", "m-missing", "m2"]) == 2
    assert store.mails == {}


async def test_overdue_and_undated_tasks_survive_until_permanent_completion(
    service: MailFlowService,
) -> None:
    overdue = await service.add_action("Past deadline", datetime.now(UTC) - timedelta(days=3))
    undated = await service.add_action("No deadline")
    assert await service.purge_expired_actions() == 0
    assert {i.item_id for i in await service.list_actions()} == {overdue.item_id, undated.item_id}
    await service.edit_action(overdue.item_id, completed=True)
    assert [i.item_id for i in await service.list_actions()] == [undated.item_id]
    assert [i.item_id for i in await service.list_actions(include_completed=True)] == [
        undated.item_id
    ]
    assert await service.storage.get_preference("actions.plans") is None
    with pytest.raises(ValueError):
        await service.edit_action(overdue.item_id, completed=False)


async def test_custom_todo_delete_is_real(service: MailFlowService) -> None:
    item = ActionItem(
        item_id="custom-1",
        mail_id="",
        summary="自己加的",
        action_type="errand",
        due_at=datetime.now(UTC) + timedelta(days=1),
    )
    store = cast(Any, service.storage)
    store.custom[item.item_id] = item
    assert await service.delete_action("custom-1") is True
    assert await service.list_actions() == []


async def test_legacy_import_remains_an_editable_deletable_task(service: MailFlowService) -> None:
    item = ActionItem(
        item_id="meeting-1",
        mail_id="m1",
        summary="Research meeting",
        action_type="meeting",
        due_at=datetime.now(UTC) + timedelta(days=1),
        origin=ActionOrigin.CUSTOM,
    )
    item = ActionItem.model_validate({**item.model_dump(mode="json"), "origin": "legacy-import"})
    assert item.origin is ActionOrigin.CUSTOM
    store = cast(Any, service.storage)
    store.custom[item.item_id] = item
    changed = await service.edit_action(item.item_id, summary="Updated task")
    assert changed.summary == "Updated task"

    assert await service.delete_action(item.item_id) is True
    assert store.custom == {}


class TestEventDedupe:
    """Only the same source and task details can identify a repeated action."""

    @staticmethod
    def _item(item_id: str, **changes: Any) -> ActionItem:
        return ActionItem.model_validate(
            {
                "item_id": item_id,
                "mail_id": "mail-1",
                "summary": "Review contract",
                "action_type": "errand",
                "due_at": _DUE,
                **changes,
            }
        )

    async def test_independent_tasks_stay_visible_and_can_be_deleted(
        self, service: MailFlowService
    ) -> None:
        from mailflow_test_support.commands import make_record

        store = cast(Any, service.storage)
        for due in (None, _DUE):
            store.preferences.clear()
            first = self._item("a", due_at=due, notes="Client A")
            second = self._item("b", mail_id="mail-2", due_at=due, notes="Client B")
            third = self._item("c", due_at=due, notes="Client C")
            record = make_record("mail-1", action=first)
            assert record.analysis is not None
            record.analysis.action_items.append(third)
            store.mails = {
                "mail-1": record,
                "mail-2": make_record("mail-2", action=second),
            }
            assert {item.item_id for item in await service.list_actions()} == {"a", "b", "c"}
            assert await service.delete_action("b")
            assert {item.item_id for item in await service.list_actions()} == {"a", "c"}
            assert await service.delete_action("a")
            assert [item.item_id for item in await service.list_actions()] == ["c"]

    def test_duplicate_detection_respects_scope_details_and_input_order(self) -> None:
        first = self._item("a", summary="  Review   CONTRACT ")
        duplicate = self._item("b")
        assert actions_duplicate(first, duplicate)
        assert dedupe_actions([first, duplicate]) == [first]
        assert dedupe_actions([duplicate, first]) == [duplicate]
        for changes in (
            {"mail_id": "another-mail"},
            {"action_type": "meeting"},
            {"notes": "Another client"},
            {"location": "Another office"},
            {"url": "https://example.com/another-contract"},
            {"due_at": _DUE + timedelta(minutes=30)},
            {"completed_at": _DUE},
            {"summary": "[NOTICE] Review contract"},
        ):
            assert not actions_duplicate(first, duplicate.model_copy(update=changes))
        blank = first.model_copy(update={"summary": " "})
        assert not actions_duplicate(blank, blank.model_copy(update={"item_id": "b"}))

    def test_duplicate_windows_require_positive_overlap(self) -> None:
        window = self._item("a", due_end=_DUE + timedelta(hours=1))
        inside = self._item("b", due_at=_DUE + timedelta(minutes=30))
        boundary = self._item("c", due_at=_DUE + timedelta(hours=1))
        overlap = inside.model_copy(update={"due_end": _DUE + timedelta(hours=2)})
        next_window = boundary.model_copy(update={"due_end": _DUE + timedelta(hours=2)})
        assert actions_duplicate(window, inside)
        assert actions_duplicate(window, overlap)
        assert not actions_duplicate(window, boundary)
        assert not actions_duplicate(window, next_window)
        point = self._item("p", due_end=_DUE - timedelta(hours=1))
        assert action_time_window(point) == (_DUE, _DUE)
