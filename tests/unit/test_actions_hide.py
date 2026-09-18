"""Dismissal semantics: deleting a mail-derived todo hides it permanently,
even across re-analysis; custom todos are deleted for real. Spent schedule
entries are retired automatically a day after they ended."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from mailflow.service import MailFlowService

import pytest
from mailflow.config import MailFlowConfig
from mailflow.domain import ActionItem, ActionOrigin
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


_DUE = datetime.now(UTC) + timedelta(days=2)


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


async def test_spent_entries_are_retired_only_after_a_day(service: MailFlowService) -> None:
    """The sweep is a day behind the due time, so an entry the user is
    looking at (or one whose reminder just fired) is never taken away."""
    store = cast(Any, service.storage)
    now = datetime.now(UTC)
    record = cast(
        Any,
        type(
            "R",
            (),
            {
                "action_items": [
                    ActionItem(
                        item_id="just-passed",
                        mail_id="m1",
                        summary="刚刚过去",
                        action_type="errand",
                        due_at=now - timedelta(hours=2),
                    ),
                    ActionItem(
                        item_id="long-gone",
                        mail_id="m2",
                        summary="上周的",
                        action_type="errand",
                        due_at=now - timedelta(days=3),
                    ),
                    ActionItem(
                        item_id="upcoming",
                        mail_id="m3",
                        summary="还没到",
                        action_type="errand",
                        due_at=now + timedelta(days=1),
                    ),
                ],
            },
        ),
    )
    store.mails["m1"] = record

    assert await service.purge_expired_actions() == 1
    remaining = {item.item_id for item in await service.list_actions()}
    assert remaining == {"just-passed", "upcoming"}


async def test_a_running_window_is_kept_until_it_closes(service: MailFlowService) -> None:
    """An event with an end time is over only when the window closed —
    deleting a running meeting drops the reminder the user is relying on."""
    store = cast(Any, service.storage)
    now = datetime.now(UTC)
    record = cast(
        Any,
        type(
            "R",
            (),
            {
                "action_items": [
                    ActionItem(
                        item_id="running",
                        mail_id="m1",
                        summary="进行中的会议",
                        action_type="meeting",
                        due_at=now - timedelta(hours=6),
                        due_end=now + timedelta(hours=1),
                    )
                ],
            },
        ),
    )
    store.mails["m1"] = record

    assert await service.purge_expired_actions() == 0
    assert [item.item_id for item in await service.list_actions()] == ["running"]


async def test_a_swept_mail_entry_stays_hidden_across_reanalysis(
    service: MailFlowService,
) -> None:
    """A swept mail-derived entry is dismissed by its natural key, so the next
    analysis of the same mail cannot resurrect it."""
    store = cast(Any, service.storage)
    old = ActionItem(
        item_id="old-a",
        mail_id="m1",
        summary="过期的",
        action_type="errand",
        due_at=datetime.now(UTC) - timedelta(days=2),
    )
    store.mails["m1"] = cast(Any, type("R", (), {"action_items": [old]}))
    assert await service.purge_expired_actions() == 1
    assert await service.list_actions() == []

    # re-analysis produces the same entry under a fresh id
    replacement = old.model_copy(update={"item_id": "old-b"})
    store.mails["m1"] = cast(Any, type("R", (), {"action_items": [replacement]}))
    assert await service.list_actions() == []


async def test_spent_custom_and_seminar_entries_are_deleted_for_real(
    service: MailFlowService,
) -> None:
    """User todos and imported seminars are not mail-owned: the sweep removes
    them from storage instead of only hiding them."""
    store = cast(Any, service.storage)
    now = datetime.now(UTC)
    spent = ActionItem(
        item_id="todo-old",
        mail_id="",
        summary="过期的待办",
        action_type="errand",
        due_at=now - timedelta(days=2),
    )
    spent_seminar = ActionItem(
        item_id="seminar-old",
        mail_id="m1",
        summary="上周的研讨会",
        action_type="seminar",
        due_at=now - timedelta(days=4),
        origin=ActionOrigin.SEMINAR,
    )
    kept = ActionItem(
        item_id="todo-new",
        mail_id="",
        summary="下周的待办",
        action_type="errand",
        due_at=now + timedelta(days=7),
    )
    for item in (spent, spent_seminar, kept):
        store.custom[item.item_id] = item

    assert await service.purge_expired_actions() == 2
    assert set(store.custom) == {"todo-new"}


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


async def test_imported_seminar_delete_is_real(service: MailFlowService) -> None:
    item = ActionItem(
        item_id="seminar-1",
        mail_id="m1",
        summary="Research seminar",
        action_type="seminar",
        due_at=datetime.now(UTC) + timedelta(days=1),
        origin=ActionOrigin.SEMINAR,
    )
    store = cast(Any, service.storage)
    store.custom[item.item_id] = item

    assert await service.delete_action(item.item_id) is True
    assert store.custom == {}
