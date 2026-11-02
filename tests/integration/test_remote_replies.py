"""The REST adapter and local service share durable reply states and tokens."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
import pytest
from mailflow.domain import ReplyState
from mailflow.i18n import I18n
from mailflow.replies import ReplyError
from mailflow.service import MailFlowService
from mailflow_server.app import create_app
from mailflow_server.client import RemoteClient
from mailflow_tui.remote import RemoteServiceAdapter

from mailflow_test_support.replies import WaitingSource
from mailflow_test_support.service import RecordingSource
from mailflow_test_support.service import service as service


def remote_adapter(
    service: MailFlowService, monkeypatch: pytest.MonkeyPatch
) -> RemoteServiceAdapter:
    service.config.server.username, service.config.server.password = "admin", "fake"
    transport = httpx.ASGITransport(app=create_app(service))
    client_class = httpx.AsyncClient

    def fake_client(**kwargs: Any) -> httpx.AsyncClient:
        return client_class(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    return RemoteServiceAdapter(
        RemoteClient("http://fake", "admin", "fake"), {"language": "en"}, I18n()
    )


async def test_local_and_remote_edits_share_state_and_one_confirmation(
    service: MailFlowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = remote_adapter(service, monkeypatch)
    draft = await remote.create_reply("m1")
    edited = await remote.edit_draft(draft.draft_id, "Re: Hello", "Remote body")
    local = await service.get_draft(draft.draft_id)
    assert local == edited
    prepared = await service.prepare_reply(draft.draft_id)
    assert (await remote.get_draft(draft.draft_id)) == prepared
    results = await asyncio.gather(
        remote.confirm_reply(draft.draft_id, prepared.token or ""),
        service.confirm_reply(draft.draft_id, prepared.token or ""),
        return_exceptions=True,
    )
    assert sum(isinstance(result, PermissionError) for result in results) == 1
    stored = await remote.get_draft(draft.draft_id)
    assert stored is not None and stored.state is ReplyState.SENT
    assert stored == await service.get_draft(draft.draft_id)
    assert len(cast(RecordingSource, service.sources["acct-1"]).sent) == 1


async def test_remote_observes_sending_and_cannot_cancel_in_flight_reply(
    service: MailFlowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = WaitingSource()
    service.sources["acct-1"] = source
    remote = remote_adapter(service, monkeypatch)
    draft = await remote.create_reply("m1")
    prepared = await remote.prepare_reply(draft.draft_id)
    send = asyncio.create_task(remote.confirm_reply(draft.draft_id, prepared.token or ""))
    await asyncio.wait_for(source.started.wait(), 2)
    in_flight = await remote.get_draft(draft.draft_id)
    assert in_flight is not None and in_flight.state is ReplyState.SENDING
    assert in_flight == await service.get_draft(draft.draft_id)
    cancel = asyncio.create_task(remote.cancel_reply(draft.draft_id))
    await asyncio.sleep(0)
    assert not cancel.done()
    source.release.set()
    assert (await send).state is ReplyState.SENT
    with pytest.raises(ReplyError):
        await cancel
    assert len(source.sent) == 1


async def test_remote_failure_keeps_body_and_translates_the_same_error_code(
    service: MailFlowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = cast(RecordingSource, service.sources["acct-1"])
    source.fail_send = True
    remote = remote_adapter(service, monkeypatch)
    remote.i18n.set_language("zh-CN")
    draft = await remote.create_reply("m1")
    await remote.edit_draft(draft.draft_id, "Re: Hello", "Saved remotely")
    prepared = await remote.prepare_reply(draft.draft_id)
    with pytest.raises(ReplyError) as error:
        await remote.confirm_reply(draft.draft_id, prepared.token or "")
    assert str(error.value) == remote.t("reply.send_failed")
    failed = await remote.get_draft(draft.draft_id)
    assert failed is not None and failed.state is ReplyState.FAILED
    assert failed.body == "Saved remotely" and failed == await service.get_draft(draft.draft_id)
    source.fail_send = False
    retry = await remote.prepare_reply(draft.draft_id)
    assert (await remote.confirm_reply(draft.draft_id, retry.token or "")).state is ReplyState.SENT


async def test_remote_cancel_invalidates_prepared_token(
    service: MailFlowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = remote_adapter(service, monkeypatch)
    draft = await remote.create_reply("m1")
    prepared = await remote.prepare_reply(draft.draft_id)
    cancelled = await remote.cancel_reply(draft.draft_id)
    assert cancelled.state is ReplyState.CANCELLED
    assert cancelled == await service.get_draft(draft.draft_id)
    with pytest.raises(PermissionError):
        await remote.confirm_reply(draft.draft_id, prepared.token or "")
    assert await remote.get_draft("missing") is None
