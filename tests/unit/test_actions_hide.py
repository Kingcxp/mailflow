"""Dismissal semantics: deleting a mail-derived todo hides it permanently,
even across re-analysis; custom todos are deleted for real."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from mailflow.service import MailFlowService

import pytest
from mailflow.config import MailFlowConfig
from mailflow.domain import ActionItem
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
    async def get_preference(self, k):
        return self.preferences.get(k)

    async def set_preference(self, k, v):
        self.preferences[k] = v

    async def list_mails(self):
        return list(self.mails.values())

    async def list_custom_actions(self):
        return list(self.custom.values())

    async def save_custom_action(self, item):
        self.custom[item.item_id] = item

    async def delete_custom_action(self, item_id):
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


def _mail_item(item_id: str) -> ActionItem:
    return ActionItem(
        item_id=item_id,
        mail_id="m1",
        summary="领学生证",
        action_type="errand",
        due_at=datetime.now(UTC) + timedelta(days=2),
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
