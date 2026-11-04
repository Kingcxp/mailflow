"""IMAP and MIME behavior with fake IMAP/SMTP connections and real MIME bytes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from mailflow.domain import (
    MailMessage,
    ReplyDraft,
)


async def poll_imap(source: Any) -> list[MailMessage]:
    mails: list[MailMessage] = []

    async def accept(mail: MailMessage) -> None:
        mails.append(mail)

    await source._poll_once(accept)
    return mails


class TestIMAPMailSource:
    def test_parse_mime_basic(self) -> None:
        from mailflow_mail_imap.plugin import parse_mime

        raw = (
            b"From: Alice <alice@example.com>\r\n"
            b"To: me@example.com\r\n"
            b"Subject: =?utf-8?B?5Lya6K6u6YCa55+l?=\r\n"  # 会议通知
            b"Message-ID: <abc-123@example.com>\r\n"
            b"Date: Mon, 10 Jun 2026 09:00:00 +0800\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Hello body\r\n"
        )
        mail = parse_mime(raw, account_id="acct-1")
        assert mail.message_id == "abc-123@example.com"
        assert mail.sender.address == "alice@example.com"
        assert mail.sender.name == "Alice"
        assert mail.subject == "会议通知"
        assert mail.body_text == "Hello body"
        assert mail.account_id == "acct-1"
        assert mail.date.utcoffset() is not None

    def test_parse_mime_html_preferred(self) -> None:
        from mailflow_mail_imap.plugin import parse_mime

        raw = (
            b"From: a@example.com\r\n"
            b"To: b@example.com\r\n"
            b"Subject: HTML mail\r\n"
            b"Content-Type: multipart/alternative; boundary=x\r\n"
            b"\r\n"
            b"--x\r\nContent-Type: text/plain\r\n\r\nplain text\r\n"
            b"--x\r\nContent-Type: text/html\r\n\r\n<p>html text</p>\r\n"
            b"--x--\r\n"
        )
        mail = parse_mime(raw, account_id="acct-1")
        assert mail.body_text == "plain text"
        assert "<p>html text</p>" in mail.body_html

    async def test_send_reply_via_smtp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mailflow.config import MailAccountConfig
        from mailflow_mail_imap.plugin import IMAPSource

        sent: list[dict[str, Any]] = []

        class FakeSMTP:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                sent.append({"args": args, "kwargs": kwargs, "mail": None})

            def starttls(self) -> None:
                sent[-1]["starttls"] = True

            def login(self, user: str, password: str) -> None:
                sent[-1]["login"] = (user, password)

            def send_message(self, message: Any) -> None:
                sent[-1]["mail"] = message

            def quit(self) -> None:
                pass

        monkeypatch.setattr("mailflow_mail_imap.plugin.smtplib.SMTP_SSL", FakeSMTP)
        account = MailAccountConfig(
            account_id="acct-1",
            provider="imap",
            email="me@example.com",
            options={"preset": "qq", "username": "me@qq.com", "password": "secret"},
        )
        source = IMAPSource(account)
        from mailflow.domain import MailAddress

        draft = ReplyDraft(
            draft_id="d1",
            mail_id="m1",
            account_id="acct-1",
            to=MailAddress(address="them@example.com"),
            subject="Re: Hi",
            body="<p>Dear them,</p>",
        )
        await source.send_reply("m1", draft)
        assert sent and sent[0]["login"] == ("me@qq.com", "secret")
        assert sent[0]["mail"]["To"] == "them@example.com"
        assert "Dear them" in str(sent[0]["mail"].get_body())

    async def test_fetch_once_polls_inbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mailflow.config import MailAccountConfig
        from mailflow_mail_imap.plugin import IMAPSource

        class FakeIMAP:
            uids: ClassVar[list[int]] = [1, 2]

            def __init__(self, host: str, port: int, timeout: float = 0) -> None:
                self.host = host
                self.port = port
                self.logged = False
                self.selected = ""

            def login(self, user: str, password: str) -> None:
                self.logged = (user, password)

            def select(self, folder: str, *, readonly: bool = False) -> tuple[str, list[bytes]]:
                self.selected = folder
                return "OK", []

            def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
                if command == "search":
                    return "OK", [b" ".join(str(u).encode() for u in self.uids)]
                assert command == "fetch"
                uid = int(args[0])
                raw = (
                    f"From: a@example.com\r\nSubject: msg {uid}\r\n"
                    f"Message-ID: <x-{uid}@e>\r\n"
                    "Date: Mon, 10 Jun 2026 09:00:00 +0800\r\n\r\nbody"
                ).encode()
                return "OK", [(b"1 (UID %d RFC822)" % uid, raw)]

            def logout(self) -> None:
                pass

        monkeypatch.setattr("mailflow_mail_imap.plugin.imaplib.IMAP4_SSL", FakeIMAP)
        account = MailAccountConfig(
            account_id="acct-1",
            provider="imap",
            email="me@qq.com",
            options={
                "preset": "qq",
                "username": "me@qq.com",
                "password": "secret",
                "limit": 5,
                "initial_sync": "all",
            },
        )
        source = IMAPSource(account)
        fetched = await poll_imap(source)  # pyright: ignore[reportPrivateUsage]
        assert len(fetched) == 2
        assert fetched[0].subject == "msg 1"
        # incremental poll: no new mails -> nothing fetched
        fetched_again = await poll_imap(source)  # pyright: ignore[reportPrivateUsage]
        assert fetched_again == []
        # a burst beyond the first-poll window is still picked up via UIDs
        FakeIMAP.uids = [1, 2, 3, 4, 5]
        fetched_burst = await poll_imap(source)  # pyright: ignore[reportPrivateUsage]
        assert [m.subject for m in fetched_burst] == ["msg 3", "msg 4", "msg 5"]

    async def test_failed_fetch_does_not_skip_the_mail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient FETCH failure must leave the UID watermark behind the
        mail so the next poll retries it instead of losing it forever."""
        from mailflow.config import MailAccountConfig
        from mailflow_mail_imap.plugin import IMAPSource

        state = {"fail_uid": 2}

        class FlakyIMAP:
            def __init__(self, host: str, port: int, timeout: float = 0) -> None: ...

            def login(self, user: str, password: str) -> None: ...

            def select(self, folder: str, *, readonly: bool = False) -> tuple[str, list[bytes]]:
                return "OK", []

            def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
                if command == "search":
                    return "OK", [b"1 2 3"]
                uid = int(args[0])
                if uid == state["fail_uid"]:
                    return "NO", [None]  # server hiccup on this uid only
                raw = (
                    f"From: a@example.com\r\nSubject: msg {uid}\r\n"
                    f"Message-ID: <x-{uid}@e>\r\n\r\nbody"
                ).encode()
                return "OK", [(b"1", raw)]

            def logout(self) -> None: ...

        monkeypatch.setattr("mailflow_mail_imap.plugin.imaplib.IMAP4_SSL", FlakyIMAP)
        account = MailAccountConfig(
            account_id="acct-1",
            provider="imap",
            options={
                "preset": "qq",
                "username": "u",
                "password": "p",
                "limit": 5,
                "initial_sync": "all",
            },
        )
        source = IMAPSource(account)
        first = await poll_imap(source)  # pyright: ignore[reportPrivateUsage]
        assert [m.subject for m in first] == ["msg 1"]

        state["fail_uid"] = 0  # server recovers
        second = await poll_imap(source)  # pyright: ignore[reportPrivateUsage]
        assert [m.subject for m in second] == ["msg 2", "msg 3"]

    async def test_fetch_history_is_newest_first_and_independent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """History browsing must not consume the live stream watermark."""
        from mailflow.config import MailAccountConfig
        from mailflow_mail_imap.plugin import IMAPSource

        class HistoryIMAP:
            def __init__(self, host: str, port: int, timeout: float = 0) -> None: ...

            def login(self, user: str, password: str) -> None: ...

            def select(self, folder: str, *, readonly: bool = False) -> tuple[str, list[bytes]]:
                return "OK", []

            def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
                if command == "search":
                    return "OK", [b"1 2 3 4 5"]
                uid = int(args[0])
                raw = (
                    f"From: a@example.com\r\nSubject: msg {uid}\r\n"
                    f"Message-ID: <x-{uid}@e>\r\n\r\nbody"
                ).encode()
                return "OK", [(b"1", raw)]

            def logout(self) -> None: ...

        monkeypatch.setattr("mailflow_mail_imap.plugin.imaplib.IMAP4_SSL", HistoryIMAP)
        account = MailAccountConfig(
            account_id="acct-1",
            provider="imap",
            options={
                "preset": "qq",
                "username": "u",
                "password": "p",
                "limit": 2,
                "initial_sync": "all",
            },
        )
        source = IMAPSource(account)
        page = await source.fetch_history(limit=2)
        assert [m.subject for m in page] == ["msg 5", "msg 4"]
        assert [m.subject for m in await source.fetch_history(limit=2, offset=2)] == [
            "msg 3",
            "msg 2",
        ]
        # Browsing left the import watermark untouched: import starts at UID 1.
        assert [m.subject for m in await poll_imap(source)] == [  # pyright: ignore[reportPrivateUsage]
            "msg 1",
            "msg 2",
        ]


class TestIMAPInternalDate:
    """``received_at`` is the server's arrival time (INTERNALDATE), not the
    moment MailFlow fetched the mail — otherwise backfilled history stamps
    every message with the same import moment and the timeline collapses."""

    def test_parse_mime_uses_internal_date_when_given(self) -> None:
        from mailflow_mail_imap.plugin import parse_mime

        raw = (
            b"From: a@example.com\r\nSubject: old\r\n"
            b"Date: Mon, 10 Jun 2026 09:00:00 +0800\r\n\r\nbody"
        )
        arrival = datetime(2026, 6, 15, 1, 15, tzinfo=UTC)
        mail = parse_mime(raw, "acct-1", internal_date=arrival)
        assert mail.received_at == arrival
        assert mail.date == datetime(2026, 6, 10, 1, 0, tzinfo=UTC)

    def test_parse_mime_falls_back_to_fetch_time(self) -> None:
        from mailflow_mail_imap.plugin import parse_mime

        raw = b"From: a@example.com\r\nSubject: live\r\n\r\nbody"
        before = datetime.now(UTC) - timedelta(seconds=5)
        mail = parse_mime(raw, "acct-1")
        assert before <= mail.received_at <= datetime.now(UTC) + timedelta(seconds=5)

    def test_internal_date_parses_server_format(self) -> None:
        from mailflow_mail_imap import plugin as imap_plugin

        response = [
            (
                b'4 (UID 4 INTERNALDATE "15-Jun-2026 09:15:00 +0800" RFC822 {123})',
                b"raw bytes",
            )
        ]
        parsed = imap_plugin._internal_date(response)  # pyright: ignore[reportPrivateUsage]
        assert parsed == datetime(2026, 6, 15, 1, 15, tzinfo=UTC)

    def test_internal_date_tolerates_malformed(self) -> None:
        from mailflow_mail_imap import plugin as imap_plugin

        assert imap_plugin._internal_date([(b"4 (UID 4 RFC822 {5})", b"raw")]) is None  # pyright: ignore[reportPrivateUsage]
        assert imap_plugin._internal_date([(b'4 (INTERNALDATE "garbage")', b"raw")]) is None  # pyright: ignore[reportPrivateUsage]
        assert imap_plugin._internal_date([]) is None  # pyright: ignore[reportPrivateUsage]

    async def test_fetch_history_carries_server_arrival_times(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mailflow.config import MailAccountConfig
        from mailflow_mail_imap.plugin import IMAPSource

        class HistoryIMAP:
            def __init__(self, host: str, port: int, timeout: float = 0) -> None: ...

            def login(self, user: str, password: str) -> None: ...

            def select(self, folder: str, *, readonly: bool = False) -> tuple[str, list[bytes]]:
                return "OK", []

            def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
                if command == "search":
                    return "OK", [b"1 2"]
                uid = int(args[0])
                raw = (
                    f"From: a@example.com\r\nSubject: msg {uid}\r\n"
                    f"Message-ID: <x-{uid}@e>\r\n\r\nbody"
                ).encode()
                stamp = f"1{uid}-Jan-2026 08:00:00 +0000"
                return "OK", [(f'1 (UID {uid} INTERNALDATE "{stamp}" RFC822)'.encode(), raw)]

            def logout(self) -> None: ...

        monkeypatch.setattr("mailflow_mail_imap.plugin.imaplib.IMAP4_SSL", HistoryIMAP)
        account = MailAccountConfig(
            account_id="acct-1",
            provider="imap",
            options={"preset": "qq", "username": "u", "password": "p", "limit": 5},
        )
        source = IMAPSource(account)
        page = await source.fetch_history(limit=2)
        # each mail carries its own server arrival time — not one shared
        # "fetched now" stamp
        stamps = [m.received_at for m in page]
        assert [m.subject for m in page] == ["msg 2", "msg 1"]
        assert stamps[0].year == 2026 and stamps[0].month == 1
        assert stamps[0] != stamps[1]


class TestIMAPBacklogSkipDefault:
    """Default mode: the first poll seeds the watermark WITHOUT emitting the
    existing backlog — only mail arriving after setup gets analyzed."""

    async def test_first_poll_skips_backlog_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mailflow.config import MailAccountConfig
        from mailflow_mail_imap.plugin import IMAPSource

        class SeedIMAP:
            def __init__(self, host: str, port: int, timeout: float = 0) -> None: ...

            def login(self, user: str, password: str) -> None: ...

            def select(self, folder: str, *, readonly: bool = False) -> tuple[str, list[bytes]]:
                return "OK", []

            def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
                if command == "search":
                    return "OK", [b"1 2 3"]
                uid = int(args[0])
                raw = (
                    f"From: a@example.com\r\nSubject: msg {uid}\r\n"
                    f"Message-ID: <x-{uid}@e>\r\n\r\nbody"
                ).encode()
                return "OK", [(b"1", raw)]

            def logout(self) -> None: ...

        monkeypatch.setattr("mailflow_mail_imap.plugin.imaplib.IMAP4_SSL", SeedIMAP)
        account = MailAccountConfig(
            account_id="acct-1",
            provider="imap",
            options={"preset": "qq", "username": "u", "password": "p"},
        )
        source = IMAPSource(account)
        # first poll emits nothing but seeds the watermark at uid 3
        assert await poll_imap(source) == []  # pyright: ignore[reportPrivateUsage]
        assert source._last_uid == 3  # pyright: ignore[reportPrivateUsage]

        # a later poll with a new mail (uid 4) is delivered normally
        class NewMailIMAP(SeedIMAP):
            def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
                if command == "search":
                    return "OK", [b"1 2 3 4"]
                uid = int(args[0])
                raw = (
                    f"From: a@example.com\r\nSubject: msg {uid}\r\n"
                    f"Message-ID: <x-{uid}@e>\r\n\r\nbody"
                ).encode()
                return "OK", [(b"1", raw)]

        monkeypatch.setattr("mailflow_mail_imap.plugin.imaplib.IMAP4_SSL", NewMailIMAP)
        fetched = await poll_imap(source)  # pyright: ignore[reportPrivateUsage]
        assert [m.subject for m in fetched] == ["msg 4"]

    def _uid_handler(self, command: str, *args: Any):
        return "OK", [b"4"]


class TestIMAPHtmlOnlyBody:
    """HTML-only mails (most notification mail) must yield a readable
    plain body: display, keyword scanning and the LLM prompt all read
    body_text."""

    def test_parse_mime_derives_text_from_html(self) -> None:
        from mailflow_mail_imap.plugin import parse_mime

        raw = (
            "From: noreply@example.com\r\n"
            "To: me@example.com\r\n"
            "Subject: html only\r\n"
            "Message-ID: <html-only@e>\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><head><style>p{color:red}</style></head><body>"
            "<p>中转站文件到期提醒</p>"
            "<BR class='break'>第二行</p><ol><li>第一项</li><li>第二项</li></ol>"
            "<a href='https://example.com/details'>点击查看</a>"
            "<script>alert(1)</script>"
            "</body></html>"
        ).encode()
        mail = parse_mime(raw, "acct-1")
        assert "中转站文件到期提醒" in mail.body_text
        assert "点击查看" in mail.body_text
        assert "\n第二行\n\n1. 第一项\n2. 第二项" in mail.body_text
        assert "https://example.com/details" in mail.body_text
        assert "alert" not in mail.body_text
        assert "color:red" not in mail.body_text
        assert mail.body_html.startswith("<html>")


class TestIMAPAttachmentBody:
    """A PDF mislabelled as text/plain is an attachment: its bytes must
    never become body_text (display, keyword scan, LLM prompt)."""

    def test_attachment_bytes_do_not_poison_body(self) -> None:
        from mailflow_mail_imap.plugin import parse_mime

        pdf_head = b"%PDF-1.7\n%\xff\xff\n1 0 obj\n<</Metadata 2 0 R>>\nendobj"
        raw = (
            "From: fapiao@chsi.com.cn\r\n"
            "To: me@example.com\r\n"
            "Subject: invoice\r\n"
            "Message-ID: <pdf-attach@e>\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=B\r\n"
            "\r\n"
            "--B\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><body><p>electronic invoice enclosed</p></body></html>\r\n"
            "--B\r\n"
            "Content-Type: text/plain; name=invoice.pdf\r\n"
            "Content-Disposition: attachment; filename=invoice.pdf\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n" + pdf_head.decode("latin-1") + "\r\n--B--\r\n"
        ).encode("latin-1")
        mail = parse_mime(raw, "acct-1")
        assert "electronic invoice" in mail.body_text
        assert "%PDF" not in mail.body_text
        # the mislabelled pdf is recorded as an attachment with metadata
        assert [a.filename for a in mail.attachments] == ["invoice.pdf"]
        assert mail.attachments[0].content_type == "text/plain"


class TestIMAPImageAttachments:
    """Image parts are recorded with metadata (never binary in the body)
    and inline images keep their Content-ID."""

    def test_image_parts_recorded(self) -> None:
        from mailflow_mail_imap.plugin import parse_mime

        png_head = "\x89PNG\r\n\x1a\n" + "imagedata" * 6
        raw = (
            "From: a@example.com\r\n"
            "To: me@example.com\r\n"
            "Subject: with image\r\n"
            "Message-ID: <img@e>\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=B\r\n"
            "\r\n"
            "--B\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "see the screenshot\r\n"
            "--B\r\n"
            "Content-Type: image/png; name=screenshot.png\r\n"
            "Content-Disposition: attachment; filename=screenshot.png\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n" + png_head + "\r\n--B--\r\n"
        ).encode("latin-1")
        mail = parse_mime(raw, "acct-1")
        assert mail.body_text == "see the screenshot"
        assert len(mail.attachments) == 1
        att = mail.attachments[0]
        assert att.filename == "screenshot.png"
        assert att.content_type == "image/png"
        assert att.size > 0
        assert att.data is not None and len(att.data) == att.size  # available until OCR/storage

    def test_unlabelled_binary_text_part_is_dropped(self) -> None:
        from mailflow_mail_imap.plugin import parse_mime

        raw = (
            "From: fapiao@chsi.com.cn\r\n"
            "To: me@example.com\r\n"
            "Subject: invoice\r\n"
            "Message-ID: <pdf-inline@e>\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=B\r\n"
            "\r\n"
            "--B\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><body><p>invoice body</p></body></html>\r\n"
            "--B\r\n"
            "Content-Type: text/plain\r\n"
            "\r\n"
            "%PDF-1.7 binary bytes here\r\n--B--\r\n"
        ).encode("latin-1")
        mail = parse_mime(raw, "acct-1")
        assert "%PDF" not in mail.body_text
        assert "invoice body" in mail.body_text


class TestIMAPParseFallback:
    def test_unparseable_mime_returns_a_recordable_mail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mailflow_mail_imap import plugin as imap_plugin

        def broken_parser(_: bytes) -> Any:
            raise RuntimeError("malformed header")

        monkeypatch.setattr(imap_plugin, "message_from_bytes", broken_parser)
        mail = imap_plugin.parse_mime(b"not a valid mail", "acct-1")

        assert mail.parse_error == "RuntimeError"
        assert mail.message_id.startswith("unparseable-")
        assert mail.subject == "(unparseable mail)"
        assert mail.body_text == ""
