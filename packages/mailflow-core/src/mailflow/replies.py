"""Portable reply failures shared by local and remote hosts."""

from __future__ import annotations

from typing import Any


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
