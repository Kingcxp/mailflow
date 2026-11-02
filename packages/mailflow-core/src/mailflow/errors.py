"""Localized failures shared by commands, HTTP clients and the TUI."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, cast

E = TypeVar("E", bound=Exception)


def translated_error(kind: type[E], translate: Callable[..., str], code: str, **params: Any) -> E:
    """Keep the existing exception type while carrying a portable message key."""
    error = kind(translate(code, **params))
    error.__dict__.update(code=code, params=params)
    return error


def error_info(error: BaseException, translate: Callable[..., str]) -> dict[str, Any]:
    """Expose useful failure categories without returning backend exception text."""
    code = getattr(error, "code", "")
    params = getattr(error, "params", {})
    if isinstance(code, str) and code and isinstance(params, dict):
        values = dict(cast("dict[str, Any]", params))
        if code in {"tui.settings_reload_failed", "errors.plugin_operation"}:
            values["error"] = translate("errors.operation")
        elif code == "plugin.load_failed":
            reason_code = values.get("reason_code", "errors.plugin_contract")
            values["reason"] = translate(reason_code, **values.get("reason_params", {}))
        message = translate(code, **values)
        if message != code:
            return {"code": code, "params": values, "message": message}

    code = error_code(error)
    return {"code": code, "params": {}, "message": translate(code)}


def error_code(error: BaseException) -> str:
    """Classify an unlabelled provider failure without importing provider libraries."""
    if isinstance(error, NotImplementedError):
        code = "common.unsupported"
    elif isinstance(error, TimeoutError) or "Timeout" in type(error).__name__:
        code = "errors.timeout"
    elif isinstance(error, PermissionError):
        code = "errors.access"
    elif (
        isinstance(error, ConnectionError)
        or getattr(error, "status", 0) in (502, 503, 504)
        or type(error).__module__.startswith(("httpx", "httpcore", "websockets", "urllib"))
    ):
        code = "errors.connection"
    elif isinstance(error, KeyError):
        code = "errors.not_found"
    elif isinstance(error, ValueError):
        code = "errors.invalid_value"
    elif isinstance(error, OSError):
        code = "errors.storage"
    else:
        code = "errors.operation"
    return code


def error_text(error: BaseException, translate: Callable[..., str]) -> str:
    return str(error_info(error, translate)["message"])
