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


async def test_overdue_and_undated_tasks_survive_and_completion_can_be_reopened(
    service: MailFlowService,
) -> None:
    overdue = await service.add_action("Past deadline", datetime.now(UTC) - timedelta(days=3))
    undated = await service.add_action("No deadline")
    assert await service.purge_expired_actions() == 0
    assert {i.item_id for i in await service.list_actions()} == {overdue.item_id, undated.item_id}
    await service.edit_action(overdue.item_id, completed=True)
    assert [i.item_id for i in await service.list_actions()] == [undated.item_id]
    assert len(await service.list_actions(include_completed=True)) == 2
    await service.edit_action(overdue.item_id, completed=False)
    assert len(await service.list_actions()) == 2


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
    """Schedule visibility uses normalized summaries and exact overlap."""

    @staticmethod
    def _item(
        item_id: str,
        *,
        mail_id: str,
        summary: str = "毕业论文答辩",
        due_at: datetime | None = None,
        due_end: datetime | None = None,
        action_type: str = "meeting",
    ) -> ActionItem:
        return ActionItem(
            item_id=item_id,
            mail_id=mail_id,
            summary=summary,
            action_type=action_type,
            due_at=due_at or _DUE,
            due_end=due_end,
        )

    async def test_duplicate_points_collapse_but_audit_list_keeps_both(
        self, service: MailFlowService
    ) -> None:
        store = cast(Any, service.storage)
        first = self._item("a", mail_id="mail-1", due_at=_DUE)
        second = self._item("b", mail_id="mail-2", due_at=_DUE, action_type="exam")
        store.mails["mail-1"] = cast(Any, type("R", (), {"action_items": [first]}))
        store.mails["mail-2"] = cast(Any, type("R", (), {"action_items": [second]}))

        listed = await service.list_actions()

        assert [item.item_id for item in listed] == ["a"]
        assert len(await service.list_actions_all()) == 2

    async def test_points_at_different_times_remain_distinct(
        self, service: MailFlowService
    ) -> None:
        first = self._item("a", mail_id="mail-1", due_at=_DUE)
        second = self._item("b", mail_id="mail-2", due_at=_DUE + timedelta(minutes=30))
        store = cast(Any, service.storage)
        store.mails["mail-1"] = cast(Any, type("R", (), {"action_items": [first]}))
        store.mails["mail-2"] = cast(Any, type("R", (), {"action_items": [second]}))

        assert len(await service.list_actions()) == 2

    async def test_point_inside_window_duplicates_but_end_boundary_does_not(self) -> None:
        window = self._item("w", mail_id="m1", due_at=_DUE, due_end=_DUE + timedelta(hours=1))
        inside = self._item("p", mail_id="m2", due_at=_DUE + timedelta(minutes=30))
        boundary = self._item("b", mail_id="m3", due_at=_DUE + timedelta(hours=1))

        assert actions_duplicate(window, inside)
        assert not actions_duplicate(window, boundary)

    async def test_windows_require_positive_overlap(self) -> None:
        first = self._item("a", mail_id="m1", due_at=_DUE, due_end=_DUE + timedelta(hours=1))
        overlap = self._item(
            "b",
            mail_id="m2",
            due_at=_DUE + timedelta(minutes=30),
            due_end=_DUE + timedelta(hours=2),
        )
        boundary = self._item(
            "c",
            mail_id="m3",
            due_at=_DUE + timedelta(hours=1),
            due_end=_DUE + timedelta(hours=2),
        )

        assert actions_duplicate(first, overlap)
        assert not actions_duplicate(first, boundary)

    async def test_whitespace_casefold_and_type_normalization(self) -> None:
        left = self._item(
            "a",
            mail_id="m1",
            summary="  Final   EXAM ",
            due_at=_DUE,
        )
        right = self._item(
            "b",
            mail_id="m2",
            summary="final exam",
            due_at=_DUE,
            action_type="exam",
        )

        assert actions_duplicate(left, right)
        assert dedupe_actions([left, right]) == [left]

        equal_time_later_input = right.model_copy(update={"item_id": "c"})
        assert dedupe_actions([equal_time_later_input, right]) == [equal_time_later_input]

    async def test_blank_summaries_and_meeting_markers_are_not_rewritten(self) -> None:
        blank_left = self._item("a", mail_id="m1", summary="  ")
        blank_right = self._item("b", mail_id="m2", summary="")
        plain = self._item("c", mail_id="m3", summary="人工智能前沿讲座")
        marked = self._item("d", mail_id="m4", summary="[NOTICE] 人工智能前沿讲座")

        assert not actions_duplicate(blank_left, blank_right)
        assert not actions_duplicate(plain, marked)

    async def test_nonpositive_window_end_is_treated_as_a_point(self) -> None:
        point = self._item("a", mail_id="m1", due_at=_DUE, due_end=_DUE)

        assert action_time_window(point) == (_DUE, _DUE)
