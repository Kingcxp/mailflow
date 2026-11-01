"""Shared plugin checks derived from the core's public protocols.

Loading checks signatures, not credentials or connectivity. The marketplace
validator can use the same checks and validate results from offline samples.
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable
from functools import wraps
from typing import Any, Union, cast, get_args, get_origin, get_type_hints

from mailflow.bot_export import BotExportResult
from mailflow.contracts import (
    GatewayProvisioner,
    HistoryCapableSource,
    LLMBackend,
    LLMEnhancer,
    MailProcessor,
    MailSource,
    Notifier,
    StorageBackend,
    TextNotifier,
)
from mailflow.domain import ComponentKind

_PROTOCOLS: dict[ComponentKind, type[Any]] = {
    ComponentKind.MAIL_SOURCE: MailSource,
    ComponentKind.LLM_BACKEND: LLMBackend,
    ComponentKind.MAIL_PROCESSOR: MailProcessor,
    ComponentKind.NOTIFIER: Notifier,
    ComponentKind.STORAGE: StorageBackend,
    ComponentKind.LLM_ENHANCER: LLMEnhancer,
    ComponentKind.GATEWAY_PROVISIONER: GatewayProvisioner,
}


def _check_method(component: Any, name: str, contract: Callable[..., Any]) -> None:
    method = getattr(component, name, None)
    if not callable(method):
        raise TypeError(f"missing method {name}")
    if inspect.iscoroutinefunction(contract) != inspect.iscoroutinefunction(method):
        mode = "async" if inspect.iscoroutinefunction(contract) else "synchronous"
        raise TypeError(f"{name} must be {mode}")
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for param in inspect.signature(contract).parameters.values():
        if param.name == "self":
            continue
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            args.append(None)
        elif param.kind == param.KEYWORD_ONLY:
            kwargs[param.name] = None
    if inspect.isclass(component):
        args.insert(0, None)
    try:
        inspect.signature(method).bind(*args, **kwargs)
    except TypeError as exc:
        raise TypeError(f"{name}: {exc}") from exc


def validate_component(kind: ComponentKind, component: Any) -> None:
    """Check the methods the host actually calls, without running them."""
    if kind is ComponentKind.BOT_EXPORTER:
        if not isinstance(component, BotExportResult):
            raise TypeError("exporter must return BotExportResult")
        return
    protocol = _PROTOCOLS[kind]
    if not inspect.isclass(component):
        for name in getattr(protocol, "__annotations__", {}):
            if not isinstance(getattr(component, name, None), str):
                raise TypeError(f"missing string attribute {name}")
    for name, contract in vars(protocol).items():
        if name.startswith("_") or not callable(contract):
            continue
        # Enhancer hooks are optional; validate the hooks that are supplied.
        if kind is ComponentKind.LLM_ENHANCER and not hasattr(component, name):
            continue
        _check_method(component, name, contract)
    if kind is ComponentKind.MAIL_SOURCE and hasattr(component, "fetch_history"):
        _check_method(component, "fetch_history", HistoryCapableSource.fetch_history)
    if kind is ComponentKind.NOTIFIER and hasattr(component, "push_text"):
        _check_method(component, "push_text", TextNotifier.push_text)


def checked_factory(kind: ComponentKind, factory: Any, plugin_id: str, component_id: str) -> Any:
    """Keep failures attached to their owning plugin and component."""
    label = f"plugin {plugin_id!r}, {kind.value} {component_id!r}"
    if not callable(factory):
        raise TypeError(f"{label}: factory must be callable")
    if inspect.iscoroutinefunction(factory):
        raise TypeError(f"{label}: factory must be synchronous")
    if kind is ComponentKind.BOT_EXPORTER:
        try:
            inspect.signature(factory).bind(None)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{label}: {exc}") from exc
    if inspect.isclass(factory) and kind is not ComponentKind.BOT_EXPORTER:
        try:
            validate_component(kind, factory)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{label}: {exc}") from exc

    @wraps(factory)
    def create(*args: Any, **kwargs: Any) -> Any:
        try:
            component = factory(*args, **kwargs)
            validate_component(kind, component)
        except Exception as exc:
            raise TypeError(f"{label}: {exc}") from exc
        return component

    return create


def _matches(value: Any, annotation: Any) -> bool:
    if annotation is Any:
        return True
    if annotation in (None, type(None)):
        return value is None
    origin = get_origin(annotation)
    if origin in (types.UnionType, Union):
        return any(_matches(value, item) for item in get_args(annotation))
    if origin is list:
        return isinstance(value, list) and all(
            _matches(item, get_args(annotation)[0]) for item in cast(list[Any], value)
        )
    if origin is dict:
        return isinstance(value, dict) and all(
            _matches(key, get_args(annotation)[0]) and _matches(item, get_args(annotation)[1])
            for key, item in cast(dict[Any, Any], value).items()
        )
    return isinstance(value, origin or annotation)


def validate_result(kind: ComponentKind, method: str, result: Any) -> None:
    """Check a sample's actual return value against the public protocol."""
    if kind is ComponentKind.BOT_EXPORTER:
        validate_component(kind, result)
        return
    protocol = _PROTOCOLS[kind]
    if kind is ComponentKind.MAIL_SOURCE and method == "fetch_history":
        protocol = HistoryCapableSource
    if kind is ComponentKind.NOTIFIER and method == "push_text":
        protocol = TextNotifier
    contract = getattr(protocol, method)
    annotation = get_type_hints(contract)["return"]
    if not _matches(result, annotation):
        raise TypeError(f"{method} returned {type(result).__name__}, expected {annotation}")
