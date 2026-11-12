"""Identity and access rules shared by commands, services and transports."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, ParamSpec, TypeVar


@dataclass(frozen=True)
class PermissionContext:
    sender: str = ""
    provider: str = ""
    instance_id: str = ""
    chat_id: str = ""
    is_admin: bool = False


# Only in-process callers inherit local trust. Every transport supplies an
# explicit identity; a missing platform identity never becomes a local admin.
LOCAL_CONTEXT = PermissionContext(sender="local", provider="local", is_admin=True)
_context: ContextVar[PermissionContext] = ContextVar("mailflow_permissions", default=LOCAL_CONTEXT)


def current_context() -> PermissionContext:
    return _context.get()


def normalize_provider(provider: str) -> str:
    name = provider.strip().lower()
    return {
        "napcat": "onebot",
        "onebot v11": "onebot",
        "onebot v12": "onebot",
        "aiocqhttp": "onebot",
    }.get(name, name)


@contextmanager
def permission_scope(context: PermissionContext) -> Generator[None]:
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


class AccessDenied(PermissionError):
    code = "permission.denied"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.params: dict[str, Any] = {}


def require_admin(translate: Callable[..., str], context: PermissionContext | None = None) -> None:
    if not (context or current_context()).is_admin:
        raise AccessDenied(translate("permission.denied"))


def authorize_command(command: str, args: list[str], translate: Callable[..., str]) -> None:
    """Read commands are public to authenticated chat/API users; writes are admin-only."""
    sub = args[0] if args else ""
    reads = {
        "help": None,
        "assistant": None,
        "files": {"", "list"},
        "example": None,
        "status": None,
        "runtime": None,
        "mail": {"", "list", "show", "search", "thread", "filters"},
        "action": {"", "list", "show", "plans"},
        "reply": {"show"},
        "trash": {"", "list"},
        "account": {"", "list"},
        "adapter": {"", "list"},
        "llm": {"", "list", "bindings"},
        "lang": {"", "get", "list"},
        "hourly": {""},
        "plugin": {"", "list", "show", "search", "market"},
        "update": {"status"},
    }
    if command in reads and (reads[command] is None or sub in (reads[command] or set())):
        return
    if command == "plugin" and sub == "repo" and (len(args) == 1 or args[1] == "list"):
        return
    require_admin(translate)


P = ParamSpec("P")
R = TypeVar("R")


def admin_operation(
    method: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Keep direct service calls and command handlers under the same policy."""

    @wraps(method)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        service: Any = args[0]
        require_admin(service.t)
        return await method(*args, **kwargs)

    return guarded
