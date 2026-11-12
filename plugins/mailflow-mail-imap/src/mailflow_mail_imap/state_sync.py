"""Scoped state operations use UID MOVE, never mailbox-wide expunge."""

from __future__ import annotations

import contextlib
import imaplib
import re
from email.parser import BytesHeaderParser
from typing import TYPE_CHECKING, Any, cast

from mailflow.domain import MailMessage, SourceCapabilities, SourceMailState

if TYPE_CHECKING:
    from mailflow_mail_imap.plugin import IMAPSource


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class IMAPState:
    def __init__(self, source: IMAPSource) -> None:
        self.source = source

    def connect(self) -> imaplib.IMAP4:
        return self.source._imap_client()  # pyright: ignore[reportPrivateUsage]

    def folders(self, client: imaplib.IMAP4) -> tuple[list[str], dict[str, str]]:
        status, response = client.list()
        if status != "OK":
            raise OSError("IMAP folder listing failed")
        names: list[str] = []
        roles: dict[str, str] = {"inbox": self.source._folder}  # pyright: ignore[reportPrivateUsage]
        for row in response or []:
            if not isinstance(row, bytes):
                continue
            match = re.match(rb'\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(.+)$', row)
            if not match or b"\\noselect" in match[1].lower():
                continue
            raw = match[2].decode("utf-8", errors="surrogateescape")
            name = (
                raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                if raw.startswith('"') and raw.endswith('"')
                else raw
            )
            names.append(name)
            flags = match[1].lower().split()
            for role in ("trash", "archive", "sent"):
                if ("\\" + role).encode() in flags:
                    roles[role] = name
            if b"\\all" in flags and "archive" not in roles:
                roles["archive"] = name
        for role in ("trash", "archive", "sent"):
            configured = str(self.source._settings.get(role + "_folder", ""))  # pyright: ignore[reportPrivateUsage]
            if configured:
                roles.pop(role, None)
                if configured in names:
                    roles[role] = configured
        return names, roles

    def capabilities(self) -> SourceCapabilities:
        client = self.connect()
        try:
            names, roles = self.folders(client)
            caps = {c.decode() if isinstance(c, bytes) else str(c) for c in client.capabilities}
            move = "MOVE" in caps
            return SourceCapabilities(
                is_read=True,
                starred=True,
                archived=move and "archive" in roles,
                trash=move and "trash" in roles,
                restore=move and roles["inbox"] in names,
                folders=roles,
            )
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    def select(self, client: imaplib.IMAP4, folder: str, *, write: bool = False) -> str:
        status, _ = client.select(quote(folder), readonly=not write)
        if status != "OK":
            raise OSError("IMAP folder unavailable")
        return self.source._validity(client)  # pyright: ignore[reportPrivateUsage]

    def state_at(
        self,
        client: imaplib.IMAP4,
        mail: MailMessage,
        folder: str,
        validity: str,
        uid: str,
        roles: dict[str, str],
    ) -> SourceMailState:
        status, rows = client.uid("FETCH", uid, "(UID FLAGS)")
        if status != "OK":
            raise OSError("IMAP state lookup failed")
        payload = b" ".join(
            part if isinstance(part, bytes) else cast(bytes, part[0])
            for part in rows or []
            if isinstance(part, bytes | tuple)
        )
        if not payload or not re.search(rb"\bUID\s+" + uid.encode() + rb"\b", payload, re.I):
            return SourceMailState(message_id=mail.message_id)
        flags = {f.lower() for f in imaplib.ParseFlags(payload)}
        return SourceMailState(
            message_id=mail.message_id,
            provider_message_id=f"{folder}:{validity}:{uid}",
            known=True,
            deleted=folder == roles.get("trash"),
            is_read=b"\\seen" in flags,
            starred=b"\\flagged" in flags,
            archived=folder == roles.get("archive"),
        )

    def locate(
        self, client: imaplib.IMAP4, mail: MailMessage, names: list[str], roles: dict[str, str]
    ) -> SourceMailState:
        try:
            original, old_validity, old_uid = mail.provider_message_id.rsplit(":", 2)
        except ValueError:
            return SourceMailState(message_id=mail.message_id)
        if original not in names:
            return SourceMailState(message_id=mail.message_id)
        validity = self.select(client, original)
        same_scope = bool(validity and old_validity == validity and old_uid.isdigit())
        if same_scope and original == roles.get("inbox"):
            state = self.state_at(client, mail, original, validity, old_uid, roles)
            if state.known:
                return state
        header = mail.headers.get("Message-ID", "")
        if not header or "\r" in header or "\n" in header:
            return (
                self.state_at(client, mail, original, validity, old_uid, roles)
                if same_scope
                else SourceMailState(message_id=mail.message_id)
            )
        for folder in dict.fromkeys([roles["inbox"], original, *names]):
            validity = self.select(client, folder)
            status, rows = client.uid(
                "SEARCH", cast(Any, None), "HEADER", "Message-ID", quote(header)
            )
            if status != "OK":
                raise OSError("IMAP identity search failed")
            uids: list[bytes] = rows[0].split() if rows and rows[0] else []
            if len(uids) > 1:
                return SourceMailState(message_id=mail.message_id)
            if uids:
                uid = uids[0].decode()
                status, headers = client.uid(
                    "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
                )
                if status != "OK":
                    raise OSError("IMAP identity lookup failed")
                raw_header = b"".join(
                    row[1]
                    for row in headers or []
                    if isinstance(row, tuple) and isinstance(row[1], bytes)
                )
                actual = str(
                    BytesHeaderParser().parsebytes(raw_header).get("Message-ID", "")
                ).strip()
                if actual != header.strip():
                    return SourceMailState(message_id=mail.message_id)
                return self.state_at(client, mail, folder, validity, uid, roles)
        return SourceMailState(message_id=mail.message_id, known=same_scope, deleted=same_scope)

    def read(self, mails: list[MailMessage]) -> list[SourceMailState]:
        client = self.connect()
        try:
            names, roles = self.folders(client)
            return [self.locate(client, mail, names, roles) for mail in mails]
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    def sent(self, message_id: str, payload: bytes | None = None) -> bool | None:
        if not message_id:
            return None
        client = self.connect()
        try:
            _, roles = self.folders(client)
            folder = roles.get("sent")
            if not folder:
                return None
            self.select(client, folder)
            status, rows = client.uid(
                "SEARCH", cast(Any, None), "HEADER", "Message-ID", quote(message_id)
            )
            if status != "OK":
                raise OSError("IMAP sent lookup failed")
            uids: list[bytes] = rows[0].split() if rows and rows[0] else []
            for uid in uids:
                status, headers = client.uid(
                    "FETCH", uid.decode(), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
                )
                if status != "OK":
                    raise OSError("IMAP sent identity lookup failed")
                header = b"".join(
                    row[1]
                    for row in headers or []
                    if isinstance(row, tuple) and isinstance(row[1], bytes)
                )
                if (
                    str(BytesHeaderParser().parsebytes(header).get("Message-ID", "")).strip()
                    == message_id.strip()
                ):
                    return True
            if payload is not None:
                status, _ = client.append(quote(folder), "(\\Seen)", None, payload)
                if status != "OK":
                    raise OSError("IMAP sent copy rejected")
                return True
            return False
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    def change(self, mail: MailMessage, changes: dict[str, Any]) -> SourceMailState:
        client = self.connect()
        try:
            names, roles = self.folders(client)
            state = self.locate(client, mail, names, roles)
            if (
                state.known
                and state.deleted
                and not state.provider_message_id
                and changes == {"deleted": True}
            ):
                return state
            if not state.known or not state.provider_message_id:
                return SourceMailState(message_id=mail.message_id)
            folder, validity, uid = state.provider_message_id.rsplit(":", 2)
            if self.select(client, folder, write=True) != validity:
                return SourceMailState(message_id=mail.message_id)
            for key, flag in (("is_read", "\\Seen"), ("starred", "\\Flagged")):
                if key in changes:
                    status, _ = client.uid(
                        "STORE",
                        uid,
                        "+FLAGS.SILENT" if changes[key] else "-FLAGS.SILENT",
                        "(" + flag + ")",
                    )
                    if status != "OK":
                        raise OSError("IMAP flag update rejected")
            destination = ""
            if "deleted" in changes:
                destination = (
                    roles.get("trash", "")
                    if changes["deleted"]
                    else str(changes.get("restore_folder") or roles["inbox"])
                )
            elif "archived" in changes:
                destination = roles.get("archive", "") if changes["archived"] else roles["inbox"]
            if destination and destination != folder:
                caps = {c.decode() if isinstance(c, bytes) else str(c) for c in client.capabilities}
                if "MOVE" not in caps or destination not in names:
                    raise NotImplementedError("safe UID MOVE unavailable")
                status, data = client.uid("MOVE", uid, quote(destination))
                if status != "OK":
                    raise OSError("IMAP move rejected")
                _, response = client.response("COPYUID")
                raw = b" ".join(
                    r for r in [*(data or []), *(response or [])] if isinstance(r, bytes)
                )
                match = re.search(rb"(?:COPYUID\s+)?(\d+)\s+(\d+)\s+(\d+)", raw)
                if match and match[2].decode() == uid:
                    validity, uid = match[1].decode(), match[3].decode()
                    if self.select(client, destination) != validity:
                        return SourceMailState(message_id=mail.message_id)
                    return self.state_at(client, mail, destination, validity, uid, roles)
                return self.locate(client, mail, names, roles)
            if ("deleted" in changes or changes.get("archived")) and not destination:
                raise NotImplementedError("target folder not configured")
            return self.state_at(client, mail, folder, validity, uid, roles)
        finally:
            with contextlib.suppress(Exception):
                client.logout()
