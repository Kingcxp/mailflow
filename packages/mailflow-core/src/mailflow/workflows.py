"""Shared user workflows for terminal, API and chat hosts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from mailflow.domain import (
    MAX_ATTACHMENT_BYTES,
    Attachment,
    FileReference,
    MailAddress,
    ReplyDraft,
    ReplyState,
    to_utc,
)
from mailflow.errors import translated_error
from mailflow.mail_body import mail_body_text
from mailflow.mail_query import MailQuery
from mailflow.permissions import current_context, normalize_provider, require_admin

if TYPE_CHECKING:
    from mailflow.service import MailFlowService


def actor_key() -> str:
    context = current_context()
    return hashlib.sha256(
        json.dumps(
            [
                normalize_provider(context.provider),
                context.instance_id,
                context.chat_id,
                context.sender,
            ]
        ).encode()
    ).hexdigest()


def addresses(raw: Any) -> list[MailAddress]:
    from email.utils import getaddresses

    if isinstance(raw, list):
        return [
            row
            if isinstance(row, MailAddress)
            else MailAddress.model_validate(row)
            if isinstance(row, dict)
            else MailAddress.parse(str(row))
            for row in cast(list[Any], raw)
        ]
    return [
        MailAddress(name=name, address=address)
        for name, address in getaddresses([str(raw or "")])
        if address
    ]


class UserWorkflows:
    def __init__(self, service: MailFlowService) -> None:
        self.service = service
        self.lock = asyncio.Lock()

    async def read_value(self, key: str, default: Any = None) -> Any:
        raw = await self.service.storage.get_preference(key)
        return json.loads(raw) if raw else default

    async def write_value(self, key: str, value: Any) -> None:
        await self.service.storage.set_preference(key, json.dumps(value, ensure_ascii=False))

    def error(self, key: str, **params: Any) -> ValueError:
        return translated_error(ValueError, self.service.t, key, **params)

    async def call(self, action: str, values: dict[str, Any]) -> Any:
        from mailflow.connection_check import check_connections
        from mailflow.notification_policy import preferences

        if action == "notification_preferences":
            return await preferences(self.service, values)
        if action == "connection_check":
            return await check_connections(self.service, values)
        if action == "send_file":
            from mailflow.chat_files import send_file

            return await send_file(self.service, values)
        if action == "reply_reconcile":
            return (await self.service.reconcile_reply(str(values.get("draft_id", "")))).model_dump(
                mode="json"
            )
        if action == "assistant_cancel":
            return await self.service.cancel_smart_plan(str(values.get("plan_id", "")))
        if action == "source_status":
            return await self.service.source_sync.status(values)
        if action == "source_refresh":
            require_admin(self.service.t)
            await self.service.source_sync.refresh(values)
            return await self.service.source_sync.status({})
        if action == "source_retry":
            return await self.service.source_sync.retry(values)
        if action in {"source_prepare", "source_preview"}:
            from mailflow.domain import PendingOperation

            require_admin(self.service.t)
            operation = str(values.get("operation", "delete"))
            if operation not in {"delete", "restore"}:
                raise self.error("common.unsupported")
            ids = list(dict.fromkeys(str(i) for i in values.get("record_ids", [])))
            rows = (
                await self.service.list_mails()
                if operation == "delete"
                else [t.to_mail_record() for t in await self.service.list_trash()]
            )
            records = [r for r in rows if r.record_id in ids]
            if len(records) != len(ids) or not records:
                raise self.error("reply.mail_not_found")
            for record in records:
                source = self.service.source_sync.source(record)
                if source is None:
                    raise self.error("experience.source_off")
                capabilities = await source.source_capabilities()
                if not getattr(capabilities, "trash" if operation == "delete" else "restore"):
                    raise self.error("experience.source_unsupported")
            source_preview_text = (
                self.service.t("experience.source_confirmation")
                + "\n"
                + "\n".join(
                    f"{r.mail.account_id} · {r.mail.subject} [{r.record_id}]" for r in records
                )
            )
            pending = PendingOperation(
                tool="source_mail",
                arguments={"record_ids": ids, "operation": operation},
                record_ids=ids,
                summary_key="experience.source_preview",
                summary_params={"preview": source_preview_text},
            )
            if action == "source_preview":
                return pending.model_dump(mode="json")
            return self.service.prepare_operations([pending]).model_dump(mode="json")
        if action == "source_confirm":
            require_admin(self.service.t)
            plan_id = str(values.get("plan_id", ""))
            plan = self.service._smart_plans.get(plan_id)  # pyright: ignore[reportPrivateUsage]
            return await self.service.apply_smart_plan(plan_id, plan[2] if plan else [])
        if action.startswith("backup_"):
            from pathlib import Path

            from mailflow.backup import export_backup, preview_backup, restore_backup

            require_admin(self.service.t)
            identity = current_context()
            if identity.provider not in {"local", "server"} and identity.chat_id != identity.sender:
                raise self.error("experience.backup_private")
            if action in {"backup_export", "backup_send"}:
                async with self.service._backup_lock:  # pyright: ignore[reportPrivateUsage]
                    payload = await export_backup(self.service)
                file = {
                    "filename": "mailflow-backup.zip",
                    "content_type": "application/zip",
                    "data": base64.b64encode(payload).decode("ascii"),
                }
                if action == "backup_send":
                    from mailflow.chat_files import deliver_file

                    return await deliver_file(self.service, file)
                return file
            if action in {"backup_preview", "backup_restore"}:
                payload = base64.b64decode(str(values.get("data", "")), validate=True)
                preview = await preview_backup(self.service, payload)
                if action == "backup_preview":
                    return preview
                directory = (
                    self.service.config_path.parent if self.service.config_path else Path.cwd()
                )
                async with self.service._backup_lock:  # pyright: ignore[reportPrivateUsage]
                    return await restore_backup(
                        self.service, str(directory / "backups" / "uploaded.zip"), payload=payload
                    )
        handlers = {
            "filters": self.filters,
            "save_filter": self.save_filter,
            "delete_filter": self.delete_filter,
            "thread": self.thread,
            "compose": self.compose,
            "upload": self.upload,
            "files": self.files,
            "attachments": self.attachments,
            "attachment_download": self.attachment_download,
            "file_download": self.file_download,
            "edit_message": self.edit_message,
            "actions": self.actions,
            "plan_control": self.plan_control,
            "assistant_history": self.service.assistant_history.read,
            "assistant_history_change": self.service.assistant_history.change,
        }
        handler = handlers.get(action)
        if handler is None:
            raise self.error("common.unsupported")
        return await handler(values)

    async def filters(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        rows = await self.read_value("filters." + actor_key(), [])
        accounts = {a.account_id for a in self.service.config.accounts}
        return [
            dict(
                row,
                available=not row["query"].get("account_id")
                or row["query"]["account_id"] in accounts,
            )
            for row in rows
        ]

    async def save_filter(self, values: dict[str, Any]) -> dict[str, Any]:
        name = str(values.get("name", "")).strip()
        if not name:
            raise self.error("experience.name_required")
        query = MailQuery.model_validate(values.get("query", {})).model_copy(
            update={"offset": 0, "limit": 200}
        )
        row = {"name": name, "query": query.model_dump(mode="json")}
        async with self.lock:
            rows = await self.filters({})
            await self.write_value(
                "filters." + actor_key(), [r for r in rows if r["name"] != name] + [row]
            )
        return row

    async def delete_filter(self, values: dict[str, Any]) -> bool:
        async with self.lock:
            rows = await self.filters({})
            kept = [r for r in rows if r["name"] != values.get("name")]
            await self.write_value("filters." + actor_key(), kept)
        return len(kept) != len(rows)

    async def thread(self, values: dict[str, Any]) -> dict[str, Any]:
        record = await self.service.get_mail(str(values.get("record_id", "")))
        if record is None:
            raise self.error("reply.mail_not_found")
        mails = [
            r
            for r in await self.service.list_mails()
            if r.mail.account_id == record.mail.account_id
        ]
        drafts = [
            d
            for d in await self.service.list_drafts()
            if d.account_id == record.mail.account_id and d.state == ReplyState.SENT
        ]
        parents: dict[str, str] = {}

        def root(key: str) -> str:
            parents.setdefault(key, key)
            while parents[key] != key:
                parents[key] = parents[parents[key]]
                key = parents[key]
            return key

        def join(keys: list[str]) -> None:
            if keys:
                first = root(keys[0])
                for key in keys[1:]:
                    parents[root(key)] = first

        for row in mails:
            mail = row.mail
            headers = {k.lower(): v for k, v in mail.headers.items()}
            ids = re.findall(
                r"<([^<>\s]+)>",
                " ".join(
                    [
                        headers.get("message-id", ""),
                        headers.get("references", ""),
                        headers.get("in-reply-to", ""),
                        mail.in_reply_to or "",
                    ]
                ),
            )
            if "@" in mail.message_id:
                ids.append(mail.message_id.strip("<>"))
            keys = [row.record_id, *("rfc:" + i for i in ids)]
            if mail.thread_id:
                keys.append("thread:" + mail.provider + ":" + mail.thread_id)
            join(keys)
        for draft in drafts:
            join(
                [
                    "sent:" + draft.draft_id,
                    *([draft.mail_id] if draft.mail_id and draft.kind != "forward" else []),
                    *(
                        "rfc:" + i.strip("<>")
                        for i in [draft.message_id, draft.in_reply_to, *draft.references]
                        if i
                    ),
                ]
            )
        selected = root(record.record_id)
        entries = await self.service.runtime.action_store.read(deduplicate=False)
        result: list[dict[str, Any]] = []
        for row in mails:
            if root(row.record_id) == selected:
                result.append(
                    {
                        "kind": "mail",
                        "id": row.record_id,
                        "subject": row.mail.subject,
                        "at": row.mail.date.isoformat(),
                        "sender": row.mail.sender.display,
                        "body": mail_body_text(row.mail),
                        "tasks": [
                            entry.item.model_dump(mode="json")
                            for entry in entries
                            if (entry.record.record_id if entry.record else entry.item.mail_id)
                            == row.record_id
                        ],
                    }
                )
        for draft in drafts:
            if root("sent:" + draft.draft_id) == selected:
                result.append(
                    {
                        "kind": "sent",
                        "id": draft.draft_id,
                        "subject": draft.subject,
                        "at": draft.updated_at.isoformat(),
                        "sender": draft.account_id,
                        "body": draft.body,
                        "tasks": [],
                    }
                )
        result.sort(key=lambda r: to_utc(datetime.fromisoformat(r["at"])))
        return {"account_id": record.mail.account_id, "messages": result}

    async def upload(self, values: dict[str, Any]) -> dict[str, Any]:
        require_admin(self.service.t)
        try:
            data = base64.b64decode(str(values.get("data", "")), validate=True)
        except ValueError:
            raise self.error("experience.file_invalid") from None
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise self.error("experience.file_too_large")
        name = str(values.get("filename", "attachment")).replace("\\", "/").split("/")[-1]
        reference = FileReference(
            file_id=uuid4().hex,
            filename=name or "attachment",
            size=len(data),
            content_type=str(values.get("content_type", "application/octet-stream")),
        )
        key = "files." + actor_key()
        async with self.lock:
            rows = await self.read_value(key, {})
            content = {
                "filename": reference.filename,
                "content_type": reference.content_type,
                "data": base64.b64encode(data).decode("ascii"),
            }
            for row in rows.values():
                if all(row.get(k) == v for k, v in content.items()):
                    return FileReference.model_validate(row).model_dump()
            rows[reference.file_id] = {
                **reference.model_dump(),
                **content,
            }
            await self.write_value(key, rows)
        return reference.model_dump()

    async def files(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        require_admin(self.service.t)
        rows = await self.read_value("files." + actor_key(), {})
        return [{k: v for k, v in row.items() if k != "data"} for row in rows.values()]

    async def file(self, file_id: str, *, owner: str = "") -> tuple[FileReference, bytes]:
        rows = await self.read_value("files." + (owner or actor_key()), {})
        row = rows.get(file_id)
        if row is None:
            raise self.error("experience.file_unavailable")
        return FileReference.model_validate(row), base64.b64decode(row["data"])

    async def file_download(self, values: dict[str, Any]) -> dict[str, Any]:
        require_admin(self.service.t)
        reference, data = await self.file(str(values.get("file_id", "")))
        return {**reference.model_dump(), "data": base64.b64encode(data).decode("ascii")}

    async def attachments(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        query = MailQuery.model_validate(
            {key: values[key] for key in ("account_id", "after", "before") if key in values}
        )
        query.state = "all"
        records: list[Any] = []
        while True:
            page = await self.service.query_mails(query)
            records.extend(page.records)
            if page.next_offset is None:
                break
            query.offset = page.next_offset
        rows: list[dict[str, Any]] = []
        for record in records:
            for index, attachment in enumerate(record.mail.attachments):
                if (
                    str(values.get("filename", "")).casefold() not in attachment.filename.casefold()
                    or str(values.get("content_type", "")).casefold()
                    not in attachment.content_type.casefold()
                ):
                    continue
                cached = await self.service.storage.get_preference(
                    f"attachment.cache.{record.record_id}.{index}"
                )
                rows.append(
                    {
                        "record_id": record.record_id,
                        "index": index,
                        "filename": attachment.filename,
                        "content_type": attachment.content_type,
                        "size": attachment.size,
                        "account_id": record.mail.account_id,
                        "date": record.mail.date.isoformat(),
                        "subject": record.mail.subject,
                        "cached": attachment.data is not None or bool(cached),
                    }
                )
        return rows

    async def attachment_download(self, values: dict[str, Any]) -> dict[str, Any]:
        require_admin(self.service.t)
        record_id, index = str(values.get("record_id", "")), int(values.get("index", 0))
        record = await self.service.get_mail(record_id)
        if record is None or not 0 <= index < len(record.mail.attachments):
            raise self.error("ux.attachment_missing")
        attachment = record.mail.attachments[index]
        data = await self.service.fetch_attachment(record_id, index)
        reference = await self.upload(
            {
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "data": base64.b64encode(data).decode("ascii"),
            }
        )
        return {**reference, "data": base64.b64encode(data).decode("ascii"), "record_id": record_id}

    async def compose(self, values: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        require_admin(self.service.t)
        kind = str(values.get("kind", "new"))
        if kind not in {"new", "forward", "reply_all"}:
            raise self.error("common.unsupported")
        record = None
        if kind != "new":
            record = await self.service.get_mail(str(values.get("mail_id", "")))
            if record is None:
                raise self.error("reply.mail_not_found")
        account_id = record.mail.account_id if record else str(values.get("account_id", ""))
        if not any(a.account_id == account_id and a.enabled for a in self.service.config.accounts):
            raise self.error("experience.account_unavailable")
        draft = ReplyDraft(
            draft_id=uuid4().hex[:16],
            mail_id=record.record_id if record else "",
            account_id=account_id,
            to=MailAddress(address=""),
            kind=cast(Literal["new", "forward", "reply_all"], kind),
            subject=str(values.get("subject", "")),
            body=str(values.get("body", "")),
            owner=actor_key(),
            message_id=f"<{uuid4().hex}@mailflow.local>",
        )
        if record:
            draft.subject = ("Fwd: " if kind == "forward" else "Re: ") + record.mail.subject
            if kind == "forward":
                draft.body = mail_body_text(record.mail)
            else:
                own = next(
                    a.email.casefold()
                    for a in self.service.config.accounts
                    if a.account_id == account_id
                )
                unique: dict[str, MailAddress] = {}
                for address in [
                    record.mail.reply_to or record.mail.sender,
                    *record.mail.recipients,
                ]:
                    if address.address.casefold() != own:
                        unique.setdefault(address.address.casefold(), address)
                draft.recipients = list(unique.values())
                copied: dict[str, MailAddress] = {}
                for address in record.mail.cc:
                    key = address.address.casefold()
                    if key not in {*unique, own}:
                        copied.setdefault(key, address)
                draft.cc = list(copied.values())
        if "subject" in values:
            draft.subject = str(values["subject"])
        if "body" in values:
            draft.body = str(values["body"])
        if "to" in values:
            draft.recipients = addresses(values["to"])
        draft.to = next(iter(draft.recipients), MailAddress(address=""))
        draft.cc = addresses(values.get("cc", draft.cc))
        draft.bcc = addresses(values.get("bcc", []))
        self.check_private_recipients(draft.bcc)
        draft.attachments = [
            (await self.file(str(file_id)))[0]
            for file_id in dict.fromkeys(values.get("file_ids", []))
        ]
        if not persist:
            return draft.model_dump(mode="json")
        await self.service.storage.save_draft(draft)
        await self.service.events.emit(
            "reply.created", draft_id=draft.draft_id, mail_id=draft.mail_id
        )
        return draft.model_dump(mode="json")

    def check_private_recipients(self, bcc: list[MailAddress]) -> None:
        context = current_context()
        if (
            bcc
            and context.provider not in {"local", "server"}
            and context.chat_id != context.sender
        ):
            raise self.error("experience.private_send")

    async def edit_message(self, values: dict[str, Any]) -> dict[str, Any]:
        draft = await self.service.edit_draft(
            str(values["draft_id"]),
            str(values.get("subject", "")),
            str(values.get("body", "")),
            recipients=addresses(values["to"]) if "to" in values else None,
            cc=addresses(values["cc"]) if "cc" in values else None,
            bcc=addresses(values["bcc"]) if "bcc" in values else None,
            file_ids=[str(i) for i in values["file_ids"]] if "file_ids" in values else None,
        )
        return draft.model_dump(mode="json")

    async def actions(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        items = await self.service.list_actions_all(preview_days=int(values.get("preview_days", 0)))
        return [
            item.model_dump(mode="json")
            for item in items
            if (not values.get("category") or item.category == values["category"])
            and (not values.get("tag") or values["tag"] in item.tags)
        ]

    async def plan_control(self, values: dict[str, Any]) -> bool:
        require_admin(self.service.t)
        operation = str(values.get("operation", ""))
        plan_id = str(values.get("plan_id", ""))
        recurring = self.service.runtime.action_store.recurring
        if operation in {"pause", "resume"}:
            async with self.service.runtime.action_store.lock:
                result = await recurring.control(plan_id, paused=operation == "pause")
        elif operation == "skip":
            item_id = str(values.get("item_id", ""))
            item = next(
                (
                    a
                    for a in await self.service.list_actions_all(preview_days=30)
                    if a.item_id == item_id and a.plan_id == plan_id
                ),
                None,
            )
            if item is None:
                raise self.error("action.not_found", item_id=item_id)
            result = await recurring.control(plan_id, skip=item_id)
            if result and not item.preview:
                await self.service.delete_action(item_id)
        else:
            raise self.error("common.unsupported")
        if not result:
            raise self.error("action.not_found", item_id=plan_id)
        await self.service.events.emit("action.changed", plan_id=plan_id)
        return result

    async def outgoing(self, draft: ReplyDraft) -> list[Attachment]:
        result: list[Attachment] = []
        for reference in draft.attachments:
            stored, data = await self.file(reference.file_id, owner=draft.owner)
            result.append(
                Attachment(
                    filename=stored.filename,
                    content_type=stored.content_type,
                    size=len(data),
                    data=data,
                )
            )
        return result

    def resolve_query(self, query: MailQuery) -> MailQuery:
        zone = ZoneInfo(self.service.config.general.timezone)
        query = query.model_copy(
            update={
                key: value.replace(tzinfo=zone)
                for key in ("after", "before")
                if (value := getattr(query, key)) is not None and value.tzinfo is None
            }
        )
        return query.resolved(datetime.now(zone))
