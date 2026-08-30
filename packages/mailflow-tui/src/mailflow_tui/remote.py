"""Remote-mode support: session persistence, login screen and a service
adapter that lets :class:`mailflow_tui.app.MailFlowApp` drive a remote
``mailflow serve`` host over REST+WS."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import queue as queue_module
from pathlib import Path
from typing import Any, ClassVar, cast

from httpx import AsyncClient
from mailflow_server.client import RemoteClient, RemoteUnsupported
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    Static,
    Switch,
)

_SESSION_FILE = Path.home() / ".mailflow" / "tui-session.json"


def load_session() -> dict[str, Any]:
    try:
        loaded: Any = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    session: dict[str, Any] = {}
    if isinstance(loaded, dict):
        mapping = cast("dict[str, Any]", loaded)
        for raw_key, raw_value in mapping.items():
            session[str(raw_key)] = raw_value
    return session


def save_session(data: dict[str, Any]) -> None:
    _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        _SESSION_FILE.chmod(0o600)


def clear_password() -> None:
    data = load_session()
    data.pop("saved_password", None)
    data["autologin"] = False
    save_session(data)


def basic_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class LoginScreen(ModalScreen[dict[str, str] | None]):
    """URL / username / password entry; remembers address and username,
    optionally saves the password and auto-logins on the next start."""

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, session: dict[str, Any]) -> None:
        super().__init__()
        self._session = session

    def compose(self) -> Any:
        yield Label("MailFlow remote", id="login-title")
        with Vertical(id="login-dialog"):
            yield Label("Server URL", classes="field-label")
            yield Input(
                value=str(self._session.get("url", "")),
                placeholder="http://host:8800",
                id="login-url",
            )
            yield Label("Username", classes="field-label")
            yield Input(value=str(self._session.get("username", "")), id="login-user")
            yield Label("Password", classes="field-label")
            yield Input(password=True, id="login-pass")
            yield Switch(value=bool(self._session.get("autologin")), id="login-save-password")
            yield Static("save password + auto-login", id="login-hint")
            with Horizontal(classes="dialog-actions"):
                yield Button("Connect", id="login-connect", variant="success")
                yield Button("Quit", id="login-quit", variant="error")
        yield Static("", id="login-status")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "login-quit":
            self.dismiss(None)
            return
        if event.button.id != "login-connect":
            return
        url = self.query_one("#login-url", Input).value.strip().rstrip("/")
        user = self.query_one("#login-user", Input).value.strip()
        password = self.query_one("#login-pass", Input).value
        save = bool(self.query_one("#login-save-password", Switch).value)
        status = self.query_one("#login-status", Static)
        if not url or not user or not password:
            status.update("[red]all fields are required[/red]")
            return
        async with AsyncClient(base_url=url, timeout=10.0) as probe:
            try:
                response = await probe.get("/snapshot", headers=basic_header(user, password))
            except Exception as exc:
                status.update(f"[red]{type(exc).__name__}: cannot reach {url}[/red]")
                return
        if response.status_code == 401:
            status.update("[red]wrong credentials[/red]")
            clear_password()
            return
        if response.status_code >= 400:
            status.update(f"[red]HTTP {response.status_code}[/red]")
            return
        session: dict[str, Any] = {"url": url, "username": user, "autologin": bool(save)}
        if save:
            session["saved_password"] = password
        save_session(session)
        self.dismiss({"url": url, "username": user, "password": password})


class RemoteServiceAdapter:
    """Duck-typed stand-in for :class:`MailFlowService` backed by REST.

    Panes keep working against JSON payloads via small view wrappers;
    operations that require a local service raise :class:`RemoteUnsupported`,
    which panes surface in their status lines.
    """

    remote = True

    def __init__(self, client: RemoteClient, snapshot: dict[str, Any], i18n: Any) -> None:
        self.client = client
        self.i18n = i18n
        self._snapshot = snapshot
        self.commands = None
        self.log_queue: queue_module.Queue[Any] = queue_module.Queue()
        self.config = _StaticConfig(snapshot)
        self.events = _EventProxy(client)

    def t(self, key: str, **params: Any) -> str:
        result: str = self.i18n.t(key, **params)
        return result

    def on(self, event: str, handler: Any) -> Any:
        """Subscribe to a live event; delegates to the websocket relay."""
        return self.events.on(event, handler)

    # -- reads -----------------------------------------------------------------
    async def snapshot(self) -> dict[str, Any]:
        self._snapshot = await self.client.snapshot()
        return self._snapshot

    def snapshot_sync(self) -> dict[str, Any]:
        return self._snapshot

    async def list_mails(self, limit: int | None = None) -> list[_RecordView]:
        return [_RecordView(item) for item in await self.client.list_mails(limit)]

    async def get_mail(self, record_id: str) -> _RecordView | None:
        data = await self.client.get_mail(record_id)
        return _RecordView(data) if data else None

    async def count_mails(self) -> int:
        return len(await self.client.list_mails())

    async def list_actions(self) -> list[dict[str, Any]]:
        return await self.client.list_actions()

    async def list_trash(self) -> list[dict[str, Any]]:
        return await self.client.list_trash()

    # -- writes -----------------------------------------------------------------
    async def set_mail_urgency(self, record_id: str, urgency: Any) -> _RecordView | None:
        data = await self.client.set_mail_urgency(record_id, urgency)
        return _RecordView(data) if data else None

    async def delete_mail(self, record_id: str) -> bool:
        return await self.client.delete_mail(record_id)

    async def restore_mail(self, record_id: str) -> dict[str, Any] | None:
        return await self.client.restore_mail(record_id)

    async def settings_sections(self) -> list[_SectionView]:
        return [_SectionView(section) for section in await self.client.settings_sections()]

    async def set_setting(self, key: str, value: Any) -> None:
        await self.client.set_setting(key, value)

    async def plugin_enable(self, plugin_id: str) -> str:
        return await self.client.plugin_enable(plugin_id)

    async def plugin_disable(self, plugin_id: str) -> None:
        await self.client.plugin_disable(plugin_id)

    def plugin_status(self, plugin_id: str) -> str:
        for plugin in self._snapshot.get("plugins", []):
            if plugin.get("plugin_id") == plugin_id:
                return "enabled"
        return "not_loaded"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def _unsupported(*args: Any, **kwargs: Any) -> Any:
            raise RemoteUnsupported(f"{name} requires a locally attached service (omit --remote)")

        return _unsupported


class _EventProxy:
    """Maps service.on(event, handler) onto the websocket relay."""

    def __init__(self, client: RemoteClient) -> None:
        self._client = client

    def subscribe(self, event: str, handler: Any) -> Any:
        return self._client.on(event, handler)

    on = subscribe


class _StaticConfig:
    """Minimal config view: timezone for display plus passthrough defaults."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        from mailflow.config import MailFlowConfig

        self._base = MailFlowConfig()
        self.general = _GeneralProxy(str(snapshot.get("timezone") or "UTC"))
        self.plugins = self._base.plugins
        self.server = self._base.server

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


class _GeneralProxy:
    def __init__(self, timezone: str) -> None:
        from mailflow.config import GeneralConfig

        self.timezone = timezone
        self._defaults = GeneralConfig()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._defaults, name)


class _SectionView:
    """Attribute adapter over one settings-section payload."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.section_id = str(data.get("section_id", ""))
        self.title = str(data.get("title", ""))
        self.plugin_id = str(data.get("plugin_id", ""))
        self.options = [_OptionView(option) for option in data.get("options", [])]


def _as_str_lines(value: Any) -> str:
    """Render a JSON list/dict as editor text; typed via Any intermediates so
    both mypy and pyright treat element types as known-any."""
    if isinstance(value, list):
        items: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
        return "\n".join(str(item) for item in items)
    if isinstance(value, dict):
        mapping: dict[Any, Any] = value  # pyright: ignore[reportUnknownVariableType]
        return "\n".join(f"{k} = {v}" for k, v in mapping.items())
    return ""


class _OptionView:
    _SECRET_MARKERS = ("api_key", "token", "password", "secret")

    def __init__(self, data: dict[str, Any]) -> None:
        self.key = str(data.get("key", ""))
        self.section = str(data.get("section", ""))
        self.label = str(data.get("label", ""))
        self.editor = str(data.get("editor", "text"))
        self.description = str(data.get("description", ""))
        self.default = data.get("default")
        self.value = data.get("value")
        self.required = bool(data.get("required"))
        self.choices = tuple(str(c) for c in data.get("choices") or ())

    @property
    def secret(self) -> bool:
        lowered = self.key.lower()
        return any(marker in lowered for marker in self._SECRET_MARKERS)

    def is_default(self) -> bool:
        return self.value == self.default

    def display_value(self) -> str:
        value: Any = self.value
        return _as_str_lines(value)


class _RecordView:
    """Attribute wrapper over one /mails JSON payload."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.record_id = str(data.get("record_id", ""))
        self.mail = _Namespace(dict(data.get("mail") or {}))
        analysis = data.get("analysis")
        self.analysis = _Namespace(dict(analysis)) if analysis else None
        self.auto_urgency = data.get("auto_urgency")
        self.manual_urgency = data.get("manual_urgency")
        self.processor_notes = list(data.get("processor_notes") or [])

    @property
    def effective_urgency(self) -> Any:
        from mailflow.domain import Urgency

        value = (
            self.manual_urgency
            if self.manual_urgency is not None
            else (self.auto_urgency or "info")
        )
        return Urgency(str(value))

    @property
    def summary(self) -> str:
        if self.analysis is not None:
            return str(self.analysis.__dict__.get("summary", ""))
        return str(self.mail.subject)

    @property
    def action_items(self) -> list[dict[str, Any]]:
        if self.analysis is None:
            return []
        items: list[dict[str, Any]] = list(self.analysis.action_items or [])
        return items


class _Namespace:
    """Attribute access over a JSON payload; unknown keys are a caller bug."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.__dict__.update(data)

    def __getattr__(self, name: str) -> Any:
        return self.__dict__.get(name)


async def pump_async_to_thread(source: asyncio.Queue[Any], target: queue_module.Queue[Any]) -> None:
    """Bridge the asyncio WS log feed into the TUI's thread-style queue."""
    while True:
        line = await source.get()
        target.put(line)


__all__ = [
    "LoginScreen",
    "RemoteServiceAdapter",
    "clear_password",
    "load_session",
    "pump_async_to_thread",
    "save_session",
]
