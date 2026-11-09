"""Reply delivery failures and mutations racing with an actual send."""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from typing import Any, cast

import pytest
from mailflow.config import MailAccountConfig
from mailflow.domain import ReplyDraft, ReplyState
from mailflow.replies import ReplyError, ReplySourceError
from mailflow.service import MailFlowService
from mailflow_mail_imap.plugin import IMAPSource

from mailflow_test_support.replies import WaitingSource
from mailflow_test_support.service import RecordingSource
from mailflow_test_support.service import service as service


async def test_failed_send_preserves_body_and_requires_a_new_token(
    service: MailFlowService,
) -> None:
    source = cast(RecordingSource, service.sources["acct-1"])
    source.fail_send = True
    draft = await service.create_reply("m1")
    await service.edit_draft(draft.draft_id, "Re: Hello", "Keep this exact body")
    prepared = await service.prepare_reply(draft.draft_id)
    with pytest.raises(ReplyError) as error:
        await service.confirm_reply(draft.draft_id, prepared.token or "")
    assert error.value.code == "reply.send_failed"
    failed = await service.get_draft(draft.draft_id)
    assert failed is not None and failed.state is ReplyState.FAILED
    assert failed.body == "Keep this exact body" and failed.token is None
    with pytest.raises(PermissionError):
        await service.confirm_reply(draft.draft_id, prepared.token or "")
    source.fail_send = False
    retry = await service.prepare_reply(draft.draft_id)
    assert retry.token and retry.token != prepared.token
    sent = await service.confirm_reply(draft.draft_id, retry.token)
    assert sent.state is ReplyState.SENT and len(source.sent) == 1


@pytest.mark.parametrize("missing", ["account", "disabled", "source", "read_only", "smtp"])
async def test_prepare_checks_sending_account_and_configuration(
    service: MailFlowService, missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = service.sources["acct-1"]
    draft = await service.create_reply("m1")
    expected = {
        "account": "reply.account_missing",
        "disabled": "reply.account_disabled",
        "source": "reply.source_missing",
        "read_only": "reply.source_read_only",
        "smtp": "reply.source_config",
    }[missing]
    if missing == "account":
        service.config.accounts.clear()
    elif missing == "disabled":
        service.config.accounts[0].enabled = False
    elif missing == "source":
        service.sources.clear()
    elif missing == "read_only":
        monkeypatch.setattr(source, "supports_reply", False, raising=False)
    else:

        async def preflight() -> None:
            raise ReplySourceError("smtp_host")

        monkeypatch.setattr(source, "check_reply_ready", preflight, raising=False)
    with pytest.raises(ReplyError) as error:
        await service.prepare_reply(draft.draft_id)
    assert error.value.code == expected
    stored = await service.get_draft(draft.draft_id)
    assert stored is not None and stored.state is ReplyState.DRAFT and stored.token is None


async def test_confirm_rechecks_source_before_consuming_prepared_token(
    service: MailFlowService,
) -> None:
    draft = await service.create_reply("m1")
    prepared = await service.prepare_reply(draft.draft_id)
    service.sources.clear()
    with pytest.raises(ReplyError) as error:
        await service.confirm_reply(draft.draft_id, prepared.token or "")
    assert error.value.code == "reply.source_missing"
    stored = await service.get_draft(draft.draft_id)
    assert stored is not None and stored.state is ReplyState.PREPARED
    assert stored.token == prepared.token


@pytest.mark.parametrize("operation", ["edit", "prepare", "cancel"])
async def test_mutations_wait_for_send_and_cannot_overwrite_sent_state(
    service: MailFlowService, operation: str
) -> None:
    source = WaitingSource()
    service.sources["acct-1"] = source
    draft = await service.create_reply("m1")
    prepared = await service.prepare_reply(draft.draft_id)
    send = asyncio.create_task(service.confirm_reply(draft.draft_id, prepared.token or ""))
    await asyncio.wait_for(source.started.wait(), 2)
    in_flight = await service.get_draft(draft.draft_id)
    assert (
        in_flight is not None and in_flight.state is ReplyState.SENDING and in_flight.token is None
    )
    if operation == "edit":
        mutation = asyncio.create_task(service.edit_draft(draft.draft_id, "changed", "changed"))
    elif operation == "prepare":
        mutation = asyncio.create_task(service.prepare_reply(draft.draft_id))
    else:
        mutation = asyncio.create_task(service.cancel_reply(draft.draft_id))
    await asyncio.sleep(0)
    assert not mutation.done()
    source.release.set()
    assert (await send).state is ReplyState.SENT
    with pytest.raises(ReplyError):
        await mutation
    stored = await service.get_draft(draft.draft_id)
    assert stored is not None and stored.state is ReplyState.SENT and len(source.sent) == 1


async def test_cancelled_confirmation_keeps_send_owned_until_provider_finishes(
    service: MailFlowService,
) -> None:
    source = WaitingSource()
    service.sources["acct-1"] = source
    draft = await service.create_reply("m1")
    prepared = await service.prepare_reply(draft.draft_id)
    send = asyncio.create_task(service.confirm_reply(draft.draft_id, prepared.token or ""))
    await asyncio.wait_for(source.started.wait(), 2)
    send.cancel()
    duplicate = asyncio.create_task(service.confirm_reply(draft.draft_id, prepared.token or ""))
    await asyncio.sleep(0)
    assert not send.done() and not duplicate.done()
    source.release.set()
    results = await asyncio.gather(send, duplicate, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert isinstance(results[1], PermissionError)
    stored = await service.get_draft(draft.draft_id)
    assert stored is not None and stored.state is ReplyState.SENT and len(source.sent) == 1


async def test_failed_final_save_does_not_allow_a_duplicate_send(
    service: MailFlowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_save = service.storage.save_draft

    async def fail_sent(draft: ReplyDraft) -> None:
        if draft.state is ReplyState.SENT:
            raise OSError("private storage failure details")
        await original_save(draft)

    monkeypatch.setattr(service.storage, "save_draft", fail_sent)
    draft = await service.create_reply("m1")
    prepared = await service.prepare_reply(draft.draft_id)
    with pytest.raises(ReplyError) as error:
        await service.confirm_reply(draft.draft_id, prepared.token or "")
    assert error.value.code == "reply.send_unrecorded"
    assert "private storage" not in str(error.value)
    with pytest.raises(PermissionError):
        await service.confirm_reply(draft.draft_id, prepared.token or "")
    with pytest.raises(ReplyError):
        await service.prepare_reply(draft.draft_id)
    stored = await service.get_draft(draft.draft_id)
    assert stored is not None and stored.state is ReplyState.SENDING
    assert len(cast(RecordingSource, service.sources["acct-1"]).sent) == 1


async def test_returned_draft_cannot_mutate_stored_state(service: MailFlowService) -> None:
    draft = await service.create_reply("m1")
    draft.body, draft.state = "unsaved", ReplyState.SENT
    stored = await service.get_draft(draft.draft_id)
    assert stored is not None and stored.state is ReplyState.DRAFT and stored.body == ""
    stored.state = ReplyState.CANCELLED
    assert (await service.prepare_reply(draft.draft_id)).state is ReplyState.PREPARED


@pytest.mark.parametrize("language", ["en", "zh-CN"])
async def test_send_error_uses_translation_without_provider_details(
    service: MailFlowService, language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.i18n.set_language(language)

    async def fail(*_: Any) -> None:
        raise RuntimeError("smtp password=private credential")

    monkeypatch.setattr(service.sources["acct-1"], "send_reply", fail)
    draft = await service.create_reply("m1")
    prepared = await service.prepare_reply(draft.draft_id)
    with pytest.raises(ReplyError) as error:
        await service.confirm_reply(draft.draft_id, prepared.token or "")
    assert str(error.value) == service.t("reply.send_failed")
    assert "credential" not in str(error.value)


async def test_smtp_preflight_happens_without_opening_a_connection() -> None:
    source = IMAPSource(
        MailAccountConfig(
            account_id="a", provider="imap", email="me@example.com", options={"password": "fake"}
        )
    )
    with pytest.raises(ReplySourceError) as error:
        await source.check_reply_ready()
    assert error.value.option == "smtp_host"


async def test_smtp_quit_failure_does_not_turn_accepted_reply_into_retry(
    service: MailFlowService, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[EmailMessage] = []

    class FakeSMTP:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def login(self, *_: Any) -> None:
            pass

        def send_message(self, message: EmailMessage) -> None:
            sent.append(message)

        def quit(self) -> None:
            raise smtplib.SMTPServerDisconnected("QUIT disconnected")

        def close(self) -> None:
            pass

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    service.sources["acct-1"] = IMAPSource(
        MailAccountConfig(
            account_id="acct-1",
            provider="imap",
            email="me@example.com",
            options={"smtp_host": "fake", "password": "fake"},
        )
    )
    from mailflow_mail_imap.plugin import parse_mime

    record = await service.storage.get_mail("m1")
    assert record is not None
    record.mail = parse_mime(
        b"From: Sender <noreply@example.com>\r\n"
        b"Reply-To: Support <support@example.com>\r\n"
        b"To: me@example.com, colleague@example.com\r\n"
        b"Cc: first@example.com, second@example.com\r\n"
        b"Message-ID: <parent@example.com>\r\n"
        b"References: <root@example.com> <parent@example.com>\r\n"
        b"In-Reply-To: <root@example.com>\r\n"
        b"Subject: Hello\r\n\r\nOriginal body",
        "acct-1",
    )
    await service.storage.save_mail(record)
    draft = await service.create_reply("m1")
    assert draft.to.address == "support@example.com"
    assert [address.address for address in record.mail.recipients] == [
        "me@example.com",
        "colleague@example.com",
    ]
    assert [address.address for address in record.mail.cc] == [
        "first@example.com",
        "second@example.com",
    ]
    await service.edit_draft(draft.draft_id, "Re: Hello", "Accepted body")
    prepared = await service.prepare_reply(draft.draft_id)
    confirmed = await service.confirm_reply(draft.draft_id, prepared.token or "")
    assert confirmed.state is ReplyState.SENT and len(sent) == 1
    assert "Accepted body" in str(sent[0])
    assert sent[0]["To"] == "support@example.com"
    assert sent[0]["In-Reply-To"] == "<parent@example.com>"
    assert sent[0]["References"] == "<root@example.com> <parent@example.com>"
    assert sent[0]["Message-ID"]
