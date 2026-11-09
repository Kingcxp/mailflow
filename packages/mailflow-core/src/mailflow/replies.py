"""Portable reply failures shared by local and remote hosts."""

from __future__ import annotations

import re
from typing import Any

from mailflow.domain import MailMessage


def reply_thread(mail: MailMessage) -> tuple[str, list[str]]:
    """Use original message identifiers, never the account-scoped storage id."""
    parent = mail.headers.get("Message-ID") or (mail.message_id if "@" in mail.message_id else "")
    parent = parent.strip().strip("<>")
    if not parent or any(char.isspace() for char in parent):
        return "", []
    parent = f"<{parent}>"
    previous = (
        mail.headers.get("References") or mail.headers.get("In-Reply-To") or mail.in_reply_to or ""
    )
    references = re.findall(r"<[^<>\s]+>", previous)
    return parent, list(dict.fromkeys([*references, parent]))


class ReplyError(ValueError):
    def __init__(self, code: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.code = code
        self.params = params


class ReplySourceError(ValueError):
    """A source's local preflight found a missing or invalid send setting."""

    def __init__(self, option: str) -> None:
        super().__init__(option)
        self.option = option
