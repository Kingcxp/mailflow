"""Mail filters shared by storage, remote clients and the interactive views."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from mailflow.domain import MailRecord, to_utc
from mailflow.ocr import searchable_record_text


class MailQuery(BaseModel):
    text: str = ""
    account_id: str = ""
    sender: str = ""
    after: datetime | None = None
    before: datetime | None = None
    state: str = "inbox"
    urgency: str = ""
    offset: int = 0
    limit: int = 200

    def matches(self, record: MailRecord) -> bool:
        mail = record.mail
        if self.account_id and mail.account_id != self.account_id:
            return False
        if self.sender.casefold() not in mail.sender.display.casefold():
            return False
        if self.after and to_utc(mail.received_at) < to_utc(self.after):
            return False
        if self.before and to_utc(mail.received_at) >= to_utc(self.before):
            return False
        if self.urgency and record.effective_urgency.value != self.urgency:
            return False
        states = {
            "inbox": not record.archived,
            "archived": record.archived,
            "unread": not record.is_read,
            "starred": record.starred,
            "pending": record.analysis_state in {"pending", "analysing"},
            "failed": record.analysis_state == "failed",
        }
        return states.get(self.state, True) and (
            not self.text or self.text.casefold() in searchable_record_text(record).casefold()
        )


class MailPage(BaseModel):
    records: list[MailRecord] = Field(default_factory=lambda: [])
    total: int = 0
    next_offset: int | None = None
