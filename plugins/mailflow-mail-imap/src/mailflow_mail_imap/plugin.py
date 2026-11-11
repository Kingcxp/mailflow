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
import hashlib
import imaplib
import inspect
import json
import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage, Message
from email.utils import formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from typing import Any, cast

from mailflow.config import MailAccountConfig
from mailflow.contracts import MailEmitter, SyncCursorError
from mailflow.domain import (
    MAX_ATTACHMENT_BYTES,
    Attachment,
    ComponentKind,
    MailAddress,
    MailMessage,
    ReplyDraft,
)
from mailflow.forms import FormField
from mailflow.mail_body import html_to_text
from mailflow.notifications import option_enabled
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar
from mailflow.replies import ReplySourceError

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


def _extract_body(
    message: Message,
) -> tuple[str, str, list[Attachment]]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        payload = part.get_payload(decode=True)
        is_text = part.get_content_maintype() == "text"
        # an attachment mislabelled as text/plain (PDF invoices arrive this
        # way) is not body prose — its bytes would poison display, keyword
        # scanning and the LLM prompt
        is_body_candidate = is_text and disposition != "attachment" and not filename
        if not is_body_candidate:
            # record every attachment (image/pdf/office/...) with metadata;
            # keep bounded bytes until OCR has read them; storage strips them later
            attachments.append(
                Attachment(
                    filename=filename or "unnamed",
                    content_type=part.get_content_type(),
                    size=len(bytes(payload)) if payload else 0,
                    content_id=part.get("Content-ID"),
                    data=bytes(payload)
                    if payload and len(bytes(payload)) <= MAX_ATTACHMENT_BYTES
                    else None,
                )
            )
            continue
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
    body_text = "\n".join(text_parts).strip()
    body_html = "\n".join(html_parts).strip()
    from mailflow.domain import looks_binary

    if looks_binary(body_text):
        # undetectable-at-parse binary bytes (PDF/zip) — drop them; the
        # html part (when present) becomes the readable body instead
        body_text = ""
    if not body_text and body_html:
        # HTML-only mails (most notification mail) must still yield a
        # readable plain body for display, analysis and keyword scanning
        body_text = html_to_text(body_html)
    return body_text, body_html, attachments


def _unparseable_mail(raw: bytes, account_id: str, provider: str, error: Exception) -> MailMessage:
    """Retain a malformed RFC-822 payload instead of blocking later UIDs."""
    now = datetime.now(UTC)
    digest = hashlib.sha256(raw).hexdigest()[:24]
    return MailMessage(
        message_id=f"unparseable-{digest}",
        account_id=account_id,
        subject="(unparseable mail)",
        sender=MailAddress(address=""),
        recipients=[],
        cc=[],
        date=now,
        received_at=now,
        provider=provider,
        parse_error=type(error).__name__,
    )


def _apply_image_text(mail: MailMessage, *, enabled: bool) -> MailMessage:
    """Use the shared attachment budget and failure-tolerant OCR path."""
    from mailflow.ocr import apply_image_text

    return apply_image_text(mail, enabled=enabled)


_INTERNALDATE_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _internal_date(response: Any) -> datetime | None:
    """Extract INTERNALDATE from a UID FETCH response tuple.

    imaplib returns the untagged data as ``(b'1234 (UID 5 INTERNALDATE
    "01-Jan-2026 09:15:00 +0800" RFC822 {size})', b'...')``. The timestamp
    is the server's arrival time — the honest `received_at` for mail
    fetched long after delivery. Malformed or absent values return None
    (the caller falls back to the fetch time).
    """
    try:
        metadata = response[0][0]
    except (IndexError, TypeError):
        return None
    if not isinstance(metadata, bytes):
        return None
    match = re.search(rb'INTERNALDATE\s+"([^"]+)"', metadata, re.IGNORECASE)
    if match is None:
        return None
    tokens = match.group(1).decode("ascii", "replace").replace("-", " ").split()
    if len(tokens) != 5:
        return None
    day, month_name, year, clock, offset = tokens
    month = _INTERNALDATE_MONTHS.get(month_name[:3].casefold())
    if month is None or offset[:1] not in {"+", "-"}:
        return None
    try:
        hour, minute, second = (int(part) for part in clock.split(":"))
        shift = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        if offset[0] == "-":
            shift = -shift
        return datetime(
            int(year), month, int(day), hour, minute, second, tzinfo=timezone(shift)
        ).astimezone(UTC)
    except (ValueError, IndexError):
        return None


def _parse_mime(
    raw: bytes,
    account_id: str,
    provider: str,
    *,
    ocr_enabled: bool = False,
    internal_date: datetime | None = None,
) -> MailMessage:
    """Convert one raw RFC-822 message into a normalized MailMessage.

    ``internal_date`` is the IMAP server's own arrival timestamp
    (INTERNALDATE) — the closest thing to "when this mail reached the
    mailbox". Without it the fetch time is used: that is honest for live
    polls (arrival ≈ fetch) but wrong for backfilled history, where it
    would stamp every historical mail with today's import moment.
    """
    message = message_from_bytes(raw)
    subject = _decode(message.get("Subject")) or "(no subject)"
    sender = _mail_address(message.get("From"))
    reply_to = _mail_address(message.get("Reply-To"))
    recipients = [
        MailAddress(name=_decode(name), address=address)
        for name, address in getaddresses(message.get_all("To") or [])
        if address
    ]
    cc = [
        MailAddress(name=_decode(name), address=address)
        for name, address in getaddresses(message.get_all("Cc") or [])
        if address
    ]
    message_id = str(message.get("Message-ID") or "").strip("<>")
    date_raw = message.get("Date")
    try:
        date = parsedate_to_datetime(date_raw) if date_raw else datetime.now(UTC)
    except (TypeError, ValueError):
        date = datetime.now(UTC)
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    received_at = internal_date.astimezone(UTC) if internal_date is not None else datetime.now(UTC)
    body_text, body_html, attachments = _extract_body(message)
    return _apply_image_text(
        MailMessage(
            message_id=message_id,
            account_id=account_id,
            subject=subject,
            sender=sender,
            reply_to=reply_to if reply_to.address else None,
            recipients=recipients,
            cc=cc,
            date=date,
            received_at=received_at,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
            provider=provider,
            in_reply_to=message.get("In-Reply-To"),
            headers={
                name: str(message[name])
                for name in ("Message-ID", "In-Reply-To", "References")
                if message.get(name)
            },
        ),
        enabled=ocr_enabled,
    )


def parse_mime(
    raw: bytes,
    account_id: str,
    provider: str = "imap",
    *,
    ocr_enabled: bool = False,
    internal_date: datetime | None = None,
) -> MailMessage:
    """Normalize raw RFC-822 data, retaining unexpected parser failures."""
    try:
        return _parse_mime(
            raw, account_id, provider, ocr_enabled=ocr_enabled, internal_date=internal_date
        )
    except Exception as exc:
        # Never log raw RFC-822 data: headers and bodies can contain secrets.
        logger.warning("MIME parse fallback for account %r: %s", account_id, type(exc).__name__)
        return _unparseable_mail(raw, account_id, provider, exc)


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


@dataclass
class _FetchedBatch:
    messages: list[tuple[int, MailMessage]]
    uidvalidity: str
    baseline: int
    history_until: int = 0
    error: str = ""
    sync_total: int = 0


class IMAPSource:
    """Fetch without consuming; acknowledge each UID after the runtime accepts it."""

    provider = "imap"

    def __init__(self, account: MailAccountConfig) -> None:
        self._account = account
        self._settings = _settings_for(account)
        self._interval = max(0.01, float(account.options.get("interval_seconds", 300)))
        self._limit = max(1, int(account.options.get("limit", 20)))
        self._status_callback: Any = None
        self._last_poll_error = ""
        self._last_uid: int | None = None
        self._uidvalidity = ""
        self._checkpoint_scope = ""
        self._watermark_save: Any = None
        self._history_until = 0
        self._session_history_until: int | None = None
        self._checkpoint_invalid = False
        self._resync_mode: str | None = None
        self._resync_days = 30
        self.sync_progress: dict[str, Any] = {
            "sync_active": False,
            "sync_total": 0,
            "sync_received": 0,
        }
        self._ocr_images = bool(account.options.get("ocr_images", False))
        self._username = str(account.options.get("username") or account.email)
        self._password = str(account.options.get("password") or "")
        self._folder = str(self._settings.get("imap_folder", "INBOX"))
        self._scope = hashlib.sha256(
            json.dumps(
                [
                    self._settings.get("imap_host"),
                    self._settings.get("imap_port", 993),
                    self._username,
                    self._folder,
                ]
            ).encode()
        ).hexdigest()[:24]

    def reset_sync(self, mode: str, days: int = 30) -> None:
        if mode not in {"new", "recent", "all"}:
            raise ValueError(mode)
        self._resync_mode, self._resync_days = mode, max(1, days)
        self._last_uid = None
        self._uidvalidity = self._checkpoint_scope = ""
        self._session_history_until = None
        self._checkpoint_invalid = False
        self.sync_progress.update(sync_active=False, sync_total=0, sync_received=0)

    def set_status_callback(self, callback: Any) -> None:
        self._status_callback = callback

    def set_watermark_store(self, load: Any, save: Any) -> None:
        """Restore a scoped UID/UIDVALIDITY checkpoint; accept old numeric UIDs."""
        self._watermark_save = save
        try:
            stored = load()
            if stored is None:
                return
            if isinstance(stored, (str, int)) and str(stored).isdigit():
                self._last_uid = int(stored)
                return
            checkpoint = json.loads(str(stored))
            self._history_until = int(checkpoint.get("history_until", 0))
            self._last_uid = int(checkpoint["uid"])
            self._uidvalidity = str(checkpoint.get("uidvalidity", ""))
            self._checkpoint_scope = str(checkpoint.get("scope", ""))
        except (TypeError, ValueError, KeyError) as exc:
            self._checkpoint_invalid = True
            logger.warning("invalid IMAP checkpoint for %r: %s", self._account.account_id, exc)

    def _imap_client(self) -> imaplib.IMAP4:
        host = str(self._settings.get("imap_host", ""))
        port = int(self._settings.get("imap_port", 993))
        if not host:
            raise ValueError(f"account {self._account.account_id!r}: no imap_host configured")
        client = (
            imaplib.IMAP4_SSL(host, port, timeout=20.0)
            if self._settings.get("imap_ssl", True)
            else imaplib.IMAP4(host, port, timeout=20.0)
        )
        try:
            client.login(self._username, self._password)
            status, _ = client.select(self._folder, readonly=True)
            if status != "OK":
                raise RuntimeError(f"cannot select IMAP folder {self._folder!r}: {status}")
        except BaseException:
            with contextlib.suppress(Exception):
                client.logout()
            raise
        return client

    @staticmethod
    def _validity(client: imaplib.IMAP4) -> str:
        response = getattr(client, "response", None)
        if response is None:
            return ""
        _status, data = response("UIDVALIDITY")
        return bytes(data[0]).decode("ascii", "replace") if data and data[0] else ""

    def _read_mail(self, client: imaplib.IMAP4, uid: int, validity: str) -> MailMessage:
        status, fetched = client.uid("fetch", str(uid), "(INTERNALDATE BODY.PEEK[])")
        raw = next(
            (
                part[1]
                for part in fetched or []
                if isinstance(part, tuple)
                and len(cast(tuple[Any, ...], part)) >= 2
                and isinstance(part[1], bytes)
            ),
            None,
        )
        if status != "OK" or raw is None:
            raise RuntimeError(f"IMAP FETCH UID {uid} failed: {status}")
        mail = parse_mime(
            raw,
            self._account.account_id,
            provider=self.provider,
            ocr_enabled=False,  # OCR runs only after the runtime saves the original.
            internal_date=_internal_date(fetched),
        )
        mail.provider_message_id = f"{self._folder}:{validity}:{uid}"
        if not mail.message_id:
            # The raw message also survives UID resets and missing Date headers;
            # the parser's best-effort timestamp is not a stable identity.
            mail.message_id = f"imap-{hashlib.sha256(raw).hexdigest()}"
        return mail

    def _search(self, client: imaplib.IMAP4) -> list[int]:
        status, data = client.uid("search", cast(Any, None), "ALL")
        if status != "OK" or not data:
            raise RuntimeError(f"IMAP SEARCH failed: {status}")
        return sorted({int(uid) for uid in (data[0] or b"").split() if uid.isdigit()})

    def _fetch_once(self) -> _FetchedBatch:
        # This worker thread never changes the live cursor. Cancellation can
        # abandon its result safely, even while a socket is still connecting.
        client = self._imap_client()
        try:
            uids = self._search(client)
            validity = self._validity(client)
            changed = bool(
                (self._checkpoint_scope and self._checkpoint_scope != self._scope)
                or (self._uidvalidity and validity and validity != self._uidvalidity)
            )
            initial_sync = self._resync_mode or self._account.options.get("initial_sync", "new")
            import_history = initial_sync in {"all", "recent"}
            catch_up_enabled = option_enabled(self._account.options, "catch_up_missed", False)
            if (
                changed
                or self._checkpoint_invalid
                or (
                    catch_up_enabled
                    and self._last_uid is not None
                    and (not self._checkpoint_scope or not self._uidvalidity or not validity)
                )
            ):
                raise SyncCursorError()
            if self._session_history_until is None and not import_history:
                catch_up = (
                    catch_up_enabled
                    and self._last_uid is not None
                    and self._checkpoint_scope == self._scope
                    and bool(validity)
                    and self._uidvalidity == validity
                )
                # Only an explicit choice and a valid scoped checkpoint may
                # receive offline mail. These messages remain quiet history.
                baseline = (
                    self._last_uid
                    if catch_up and self._last_uid is not None
                    else max(self._last_uid or 0, max(uids, default=0))
                )
            elif self._last_uid is not None:
                baseline = self._last_uid
            elif initial_sync == "all":
                baseline = 0
            elif initial_sync == "recent":
                from datetime import timedelta

                since = (
                    datetime.now(UTC)
                    - timedelta(
                        days=self._resync_days
                        if self._resync_mode
                        else int(self._account.options.get("initial_days", 30))
                    )
                ).strftime("%d-%b-%Y")
                status, recent = client.uid("search", cast(Any, None), "SINCE", since)
                if status != "OK":
                    raise RuntimeError("IMAP history search failed")
                recent_uids = [int(uid) for uid in (recent[0] or b"").split() if uid.isdigit()]
                baseline = min(recent_uids) - 1 if recent_uids else max(uids, default=0)
            else:
                # Deliberately excluded setup backlog; even an empty mailbox
                # needs baseline 0 so its very first new message is ingested.
                baseline = max(uids, default=0)
            # Explicit imports stay separate from new arrivals. The legacy
            # analyze_backlog option never starts an implicit history batch.
            history_until = (
                max(uids, default=0)
                if changed or self._session_history_until is None
                else self._session_history_until
            )
            messages: list[tuple[int, MailMessage]] = []
            fetch_error = ""
            for uid in [uid for uid in uids if uid > baseline][: self._limit]:
                try:
                    mail = self._read_mail(client, uid, validity)
                    if uid <= history_until:
                        mail.ingestion = "import" if import_history else "catch_up"
                        mail.include_past_actions = bool(
                            self._account.options.get("include_past_actions", False)
                        )
                    messages.append((uid, mail))
                except Exception as exc:
                    # Return the successful prefix. Do not consume this UID or
                    # fetch past it; it must be retried on the next connection.
                    logger.warning("IMAP account %r UID %d: %s", self._account.account_id, uid, exc)
                    fetch_error = type(exc).__name__
                    break
            return _FetchedBatch(
                messages,
                validity,
                baseline,
                history_until,
                fetch_error,
                sum(baseline < uid <= history_until for uid in uids),
            )
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    async def _commit(self, uid: int, validity: str) -> None:
        value = json.dumps(
            {
                "uid": uid,
                "uidvalidity": validity,
                "scope": self._scope,
                "history_until": self._history_until,
            }
        )
        if self._watermark_save is not None:
            result = self._watermark_save(value)
            if inspect.isawaitable(result):
                await result
        self._last_uid, self._uidvalidity = uid, validity
        self._checkpoint_scope = self._scope

    async def _poll_once(self, emit: MailEmitter) -> int:
        batch = await asyncio.to_thread(self._fetch_once)
        self._last_poll_error = batch.error
        self._history_until = batch.history_until
        if self._session_history_until is None:
            self.sync_progress.update(sync_total=batch.sync_total, sync_received=0)
        self.sync_progress["sync_active"] = batch.sync_total > 0
        if (
            self._last_uid != batch.baseline
            or self._uidvalidity != batch.uidvalidity
            or self._checkpoint_scope != self._scope
        ):
            await self._commit(batch.baseline, batch.uidvalidity)
        self._session_history_until = batch.history_until
        self._resync_mode = None
        for uid, mail in batch.messages:
            await emit(mail)
            await self._commit(uid, batch.uidvalidity)
            if uid <= batch.history_until:
                self.sync_progress["sync_received"] += 1
                self.sync_progress["sync_active"] = uid < batch.history_until
                if self._status_callback is not None:
                    self._status_callback(True, 1, "")
        return len(batch.messages)

    def _fetch_history(self, limit: int, offset: int) -> list[MailMessage]:
        client = self._imap_client()
        try:
            uids = list(reversed(self._search(client)))
            validity = self._validity(client)
            messages: list[MailMessage] = []
            for uid in uids[max(0, offset) : max(0, offset) + max(0, limit)]:
                messages.append(self._read_mail(client, uid, validity))
            return messages
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    def _download_attachment(self, mail: MailMessage, index: int) -> bytes:
        client = self._imap_client()
        try:
            folder, validity, uid = mail.provider_message_id.rsplit(":", 2)
            if folder != self._folder:
                raise ValueError("mail folder changed")
            if validity != self._validity(client):
                message_id = mail.headers.get("Message-ID") or mail.message_id
                message_id = message_id.replace("\r", "").replace("\n", "").replace('"', "")
                status, data = client.uid(
                    "search", cast(Any, None), "HEADER", "Message-ID", f'"{message_id}"'
                )
                if status != "OK" or not data or not data[0]:
                    raise FileNotFoundError("source mail is no longer available")
                uid = data[0].split()[-1].decode("ascii")
            status, fetched = client.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next(
                (
                    part[1]
                    for part in fetched or []
                    if isinstance(part, tuple) and isinstance(part[1], bytes)
                ),
                None,
            )
            if status != "OK" or raw is None:
                raise FileNotFoundError("source mail is no longer available")
            parsed = parse_mime(raw, mail.account_id, provider=self.provider)
            if parsed.message_id != mail.message_id and not mail.message_id.startswith("imap-"):
                raise FileNotFoundError("source identity changed")
            message = message_from_bytes(raw)
            attachments = [
                part
                for part in message.walk()
                if part.get_content_maintype() != "multipart"
                and not (
                    part.get_content_maintype() == "text"
                    and part.get_content_disposition() != "attachment"
                    and not part.get_filename()
                )
            ]
            payload = attachments[index].get_payload(decode=True)
            if not isinstance(payload, bytes):
                raise ValueError("attachment has no payload")
            return payload
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    async def fetch_attachment(self, mail: MailMessage, index: int) -> bytes:
        return await asyncio.to_thread(self._download_attachment, mail, index)

    async def fetch_history(self, limit: int = 50, offset: int = 0) -> list[MailMessage]:
        return await asyncio.to_thread(self._fetch_history, limit, offset)

    async def run(self, emit: MailEmitter, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            count = 0
            try:
                count = await self._poll_once(emit)
                if self._status_callback is not None:
                    self._status_callback(not self._last_poll_error, count, self._last_poll_error)
            except asyncio.CancelledError:
                raise
            except SyncCursorError:
                self.sync_progress["sync_active"] = False
                if self._status_callback is not None:
                    self._status_callback(False, 0, "sync.cursor_invalid")
                return
            except Exception as exc:
                if self._status_callback is not None:
                    self._status_callback(False, 0, type(exc).__name__)
                logger.warning("IMAP account %r poll failed: %s", self._account.account_id, exc)
            if not self._last_poll_error and count >= self._limit:
                await asyncio.sleep(0)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)

    async def check_reply_ready(self) -> None:
        for option, value in (
            ("smtp_host", self._settings.get("smtp_host")),
            ("username", self._username),
            ("password", self._password),
        ):
            if not value:
                raise ReplySourceError(option)
        try:
            port = int(self._settings.get("smtp_port", 465))
        except (ValueError, TypeError):
            raise ReplySourceError("smtp_port") from None
        if not 1 <= port <= 65535:
            raise ReplySourceError("smtp_port")

    async def send_reply(self, mail_id: str, draft: ReplyDraft) -> None:
        await self.check_reply_ready()
        await asyncio.to_thread(self._send_smtp, draft)

    def _send_smtp(self, draft: ReplyDraft) -> None:
        host = str(self._settings.get("smtp_host", ""))
        port = int(self._settings.get("smtp_port", 465))
        if not host:
            raise ValueError(f"account {self._account.account_id!r}: no smtp_host configured")
        message = EmailMessage()
        message["From"] = self._account.email or self._username
        message["To"] = draft.to.address
        message["Subject"] = draft.subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid()
        if draft.in_reply_to:
            message["In-Reply-To"] = draft.in_reply_to
        if draft.references:
            message["References"] = " ".join(draft.references)
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
            # A failed QUIT after SMTP accepted the message is not a failed
            # delivery; surfacing it as one would invite a duplicate retry.
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                client.close()

    async def close(self) -> None:
        self._session_history_until = None


def account_form(preset: str = "qq") -> tuple[FormField, ...]:
    return (
        FormField(
            "preset",
            kind="select",
            default=preset,
            choices=("qq", "163", "gmail", "outlook", "custom"),
        ),
        FormField("username"),
        FormField("password", kind="password", required=True, secret=True),
        FormField("imap_host"),
        FormField("imap_port", kind="number", default=993),
        FormField("imap_ssl", kind="boolean", default=True),
        FormField("imap_folder", default="INBOX"),
        FormField("smtp_host"),
        FormField("smtp_port", kind="number", default=465),
        FormField("smtp_ssl", kind="boolean", default=True),
        FormField(
            "initial_sync",
            kind="select",
            default="new",
            choices=("new", "recent", "all"),
            label_key="ux.initial_sync",
        ),
        FormField("initial_days", kind="number", default=30, label_key="ux.initial_days"),
        FormField(
            "catch_up_missed",
            kind="boolean",
            default=False,
            label_key="ux.catch_up_missed",
            description_key="ux.catch_up_missed_help",
        ),
        FormField(
            "include_past_actions",
            kind="boolean",
            default=False,
            label_key="ux.include_past_actions",
        ),
        FormField(
            "catch_up_summary", kind="boolean", default=True, label_key="sync.summary_option"
        ),
        FormField("interval_seconds", kind="number", default=300),
        FormField("limit", kind="number", default=20),
    )


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
        registrar.add_form_fields(ComponentKind.MAIL_SOURCE, "imap", account_form())


plugin = MailPlugin()

__all__ = ["PRESETS", "IMAPSource", "MailPlugin", "parse_mime", "plugin"]
