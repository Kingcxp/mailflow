"""IMAP/SMTP mail source with built-in provider presets.

Standard library only (imaplib / smtplib / email). Choose a preset via
``options.preset`` (``qq``, ``163``, ``outlook``, ``gmail``) or configure a
generic school/work server with explicit host/port options; every preset
can be overridden field by field.

Credentials come from the account config (``username`` / ``password``),
usually as ``${ENV_VAR}`` placeholders; never log them.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import logging
import smtplib
from datetime import UTC, datetime
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage, Message
from email.utils import formatdate, parseaddr, parsedate_to_datetime
from typing import Any, cast

from mailflow.config import MailAccountConfig
from mailflow.contracts import MailEmitter
from mailflow.domain import ComponentKind, MailAddress, MailMessage
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.mail.imap")

PRESETS: dict[str, dict[str, Any]] = {
    "qq": {
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_ssl": True,
    },
    "163": {
        "imap_host": "imap.163.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "smtp_ssl": True,
    },
    "outlook": {
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_ssl": False,
    },
    "gmail": {
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_ssl": False,
    },
}


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            try:
                decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                decoded.append(chunk.decode("utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return "".join(decoded)


def _mail_address(raw: str | None) -> MailAddress:
    name, address = parseaddr(raw or "")
    return MailAddress(name=_decode(name), address=address)


def _extract_body(message: Message) -> tuple[str, str]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            content = bytes(payload).decode(charset, errors="replace")
        except (LookupError, TypeError):
            content = bytes(payload).decode("utf-8", errors="replace")
        if part.get_content_subtype() == "html":
            html_parts.append(content)
        else:
            text_parts.append(content)
    return "\n".join(text_parts).strip(), "\n".join(html_parts).strip()


def parse_mime(raw: bytes, account_id: str, provider: str = "imap") -> MailMessage:
    """Convert one raw RFC-822 message into a normalized MailMessage."""
    message = message_from_bytes(raw)
    subject = _decode(message.get("Subject")) or "(no subject)"
    sender = _mail_address(message.get("From"))
    recipients = [_mail_address(value) for value in (message.get_all("To") or [])]
    cc = [_mail_address(value) for value in (message.get_all("Cc") or [])]
    message_id = str(message.get("Message-ID") or "").strip("<>")
    date_raw = message.get("Date")
    try:
        date = parsedate_to_datetime(date_raw) if date_raw else datetime.now(UTC)
    except (TypeError, ValueError):
        date = datetime.now(UTC)
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    body_text, body_html = _extract_body(message)
    return MailMessage(
        message_id=message_id,
        account_id=account_id,
        subject=subject,
        sender=sender,
        recipients=recipients,
        cc=cc,
        date=date,
        received_at=datetime.now(UTC),
        body_text=body_text,
        body_html=body_html,
        provider=provider,
    )


def _settings_for(account: MailAccountConfig) -> dict[str, Any]:
    options = account.options or {}
    settings: dict[str, Any] = dict(PRESETS.get(str(options.get("preset", "")), {}))
    for key in (
        "imap_host",
        "imap_port",
        "imap_ssl",
        "smtp_host",
        "smtp_port",
        "smtp_ssl",
        "imap_folder",
    ):
        if options.get(key) is not None:
            settings[key] = options[key]
    return settings


class IMAPSource:
    """Polls an IMAP INBOX and sends replies over SMTP."""

    def __init__(self, account: MailAccountConfig) -> None:
        self._account = account
        self._settings = _settings_for(account)
        self._interval = int(account.options.get("interval_seconds", 300))
        self._limit = int(account.options.get("limit", 20))
        self._seen: set[str] = set()
        self._last_uid: int | None = None
        self._username = str(account.options.get("username") or account.email)
        self._password = str(account.options.get("password") or "")

    def _imap_client(self) -> imaplib.IMAP4:
        host = str(self._settings.get("imap_host", ""))
        port = int(self._settings.get("imap_port", 993))
        if not host:
            raise ValueError(
                f"account {self._account.account_id!r}: no imap_host configured "
                "(set options.preset to qq/163/outlook/gmail or provide imap_host)"
            )
        client = (
            imaplib.IMAP4_SSL(host, port)
            if self._settings.get("imap_ssl", True)
            else imaplib.IMAP4(host, port)
        )
        client.login(self._username, self._password)
        folder = str(self._settings.get("imap_folder", "INBOX"))
        client.select(folder)
        return client

    def _fetch_once(self) -> list[MailMessage]:
        client = self._imap_client()
        try:
            _status, data = client.uid("search", cast(Any, None), "ALL")
            all_uids = sorted(
                {int(u) for u in (data[0] or b"").split() if u.isdigit()},
            )
            if self._last_uid is not None:
                # Incremental: only mails that arrived after the previous poll.
                wanted = [u for u in all_uids if u > self._last_uid]
            else:
                # First poll: the most recent ``limit`` mails (avoid flooding;
                # a non-positive limit would slice to the whole mailbox)
                wanted = all_uids[-max(1, self._limit) :]
            messages: list[MailMessage] = []
            for uid_int in wanted:
                _status, fetch = client.uid("fetch", str(uid_int), "(RFC822)")
                if not fetch or fetch[0] is None:
                    # Leave the watermark behind this uid so a transient
                    # server error does not drop the mail permanently.
                    break
                raw = fetch[0][1]
                mail = parse_mime(bytes(raw), self._account.account_id, provider="imap")
                # Advance only once the mail is in hand: an exception above
                # retries the same uid on the next poll instead of skipping it.
                self._last_uid = uid_int
                if mail.normalized_message_id() not in self._seen:
                    self._seen.add(mail.normalized_message_id())
                    messages.append(mail)
            if len(self._seen) > 10000:
                # UID tracking already guarantees single fetch; the set is
                # only a defense against servers that repeat UIDs.
                self._seen.clear()
            return messages
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    def _fetch_history(self, limit: int, offset: int) -> list[MailMessage]:
        """Newest-first window over the mailbox, independent of the poll
        watermark: browsing history must never consume the live stream."""
        client = self._imap_client()
        try:
            _status, data = client.uid("search", cast(Any, None), "ALL")
            all_uids = sorted({int(u) for u in (data[0] or b"").split() if u.isdigit()})
            newest_first = list(reversed(all_uids))
            window = newest_first[offset : offset + limit] if limit > 0 else []
            messages: list[MailMessage] = []
            for uid_int in window:
                _status, fetch = client.uid("fetch", str(uid_int), "(RFC822)")
                if not fetch or fetch[0] is None:
                    continue
                messages.append(
                    parse_mime(bytes(fetch[0][1]), self._account.account_id, provider="imap")
                )
            return messages
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    async def fetch_history(self, limit: int = 50, offset: int = 0) -> list[MailMessage]:
        return await asyncio.to_thread(self._fetch_history, limit, offset)

    async def run(self, emit: MailEmitter, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                for mail in await asyncio.to_thread(self._fetch_once):
                    await emit(mail)
            except Exception as exc:
                logger.warning("imap fetch failed for %r: %s", self._account.account_id, exc)
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)

    async def send_reply(self, mail_id: str, draft: Any) -> None:
        await asyncio.to_thread(self._send_smtp, draft)

    def _send_smtp(self, draft: Any) -> None:
        host = str(self._settings.get("smtp_host", ""))
        port = int(self._settings.get("smtp_port", 465))
        if not host:
            raise ValueError(f"account {self._account.account_id!r}: no smtp_host configured")
        message = EmailMessage()
        message["From"] = self._account.email or self._username
        message["To"] = draft.to.address
        message["Subject"] = draft.subject
        message["Date"] = formatdate(localtime=True)
        body = str(getattr(draft, "body", "") or "")
        stripped = body.lstrip()
        looks_like_html = stripped.startswith("<") and "</" in body
        if looks_like_html:
            # real HTML drafts ship with a plain-text alternative built from
            # the same content, so non-HTML clients never see an empty body
            from mailflow.letters import html_to_text

            message.set_content(html_to_text(body) or "(see HTML version)")
            message.add_alternative(body, subtype="html")
        else:
            message.set_content(body)
        use_ssl = bool(self._settings.get("smtp_ssl", port == 465))
        if use_ssl:
            client: Any = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            client = smtplib.SMTP(host, port, timeout=30)
            client.starttls()
        try:
            client.login(self._username, self._password)
            client.send_message(message)
        finally:
            client.quit()

    async def close(self) -> None:
        pass


PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-mail-imap",
    name="IMAP Mail Source",
    version="0.1.0",
    description="IMAP/SMTP mail source with presets for QQ, 163, Outlook, Gmail and generic hosts",
    kinds=[ComponentKind.MAIL_SOURCE],
)


class MailPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_source("imap", IMAPSource)


plugin = MailPlugin()

__all__ = ["PRESETS", "IMAPSource", "MailPlugin", "parse_mime", "plugin"]
