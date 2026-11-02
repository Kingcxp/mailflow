"""Remote-mode support: session persistence, login screen and a service
adapter that lets :class:`mailflow_tui.app.MailFlowApp` drive a remote
``mailflow serve`` host over REST+WS."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import queue as queue_module
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.parse import urlsplit

import httpx
from httpx import AsyncClient
from mailflow.action_store import action_has_ended
from mailflow.config import MailFlowConfig
from mailflow.domain import ActionItem, MailRecord, ReplyDraft, RuntimeSnapshot, TrashRecord
from mailflow.i18n import I18n
from mailflow.permissions import AccessDenied
from mailflow.replies import ReplyError
from mailflow.settings import EditorKind, OptionSpec, SettingsSection
from mailflow_server.client import RemoteClient, RemoteUnsupported
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    Static,
    Switch,
)
from textual.worker import WorkerCancelled

from mailflow_tui.labels import error_detail, redact_operation_logs

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
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=_SESSION_FILE.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
        temporary.chmod(0o600)
        temporary.replace(_SESSION_FILE)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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

    DEFAULT_CSS = """
    LoginScreen {
        align: center middle;
    }
    #login-dialog {
        width: 56;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #login-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #login-hint {
        color: $text-muted;
        margin-top: 0;
    }
    #login-status {
        height: 1;
        margin-top: 1;
    }
    .field-label {
        color: $text-muted;
    }
    #login-url, #login-user, #login-pass {
        margin-bottom: 1;
    }
    """

    def __init__(self, session: dict[str, Any]) -> None:
        super().__init__()
        self._session = session
        self._i18n = I18n(language=str(session.get("language") or "en"))
        self._connection: Any = None
        self._connecting = False
        self._bindings.bind("escape", "cancel_connection", self._t("tui.btn_cancel"))

    def _t(self, key: str, **params: Any) -> str:
        return self._i18n.t(key, **params)

    def compose(self) -> Any:
        # centered bordered panel like every other standalone dialog
        with Vertical(id="login-dialog"):
            yield Label(self._t("tui.remote_login_title"), id="login-title")
            yield Label(self._t("tui.remote_server_url"), classes="field-label")
            yield Input(
                value=str(self._session.get("url", "")),
                placeholder=self._t("tui.remote_url_placeholder"),
                id="login-url",
            )
            yield Label(self._t("tui.remote_username"), classes="field-label")
            yield Input(value=str(self._session.get("username", "")), id="login-user")
            yield Label(self._t("tui.remote_password"), classes="field-label")
            yield Input(password=True, id="login-pass")
            yield Switch(value=bool(self._session.get("autologin")), id="login-save-password")
            yield Static(self._t("tui.remote_save_password"), id="login-hint")
            with Horizontal(classes="dialog-actions"):
                yield Button(self._t("tui.remote_connect"), id="login-connect", variant="success")
                yield Button(self._t("tui.btn_quit"), id="login-quit", variant="error")
            yield Static("", id="login-status")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "login-connect" and self._connecting:
            await self.action_cancel_connection()
            return
        if event.button.id == "login-quit":
            await self.action_cancel_connection()
            self.dismiss(None)
            return
        if event.button.id != "login-connect":
            return
        self._connecting = True
        self._connection = self.run_worker(
            self._connect(), group="remote-login", exit_on_error=False
        )

    async def action_cancel_connection(self) -> None:
        if self._connecting and self._connection is not None:
            self._connection.cancel()
            with contextlib.suppress(WorkerCancelled):
                await self._connection.wait()
            self._connecting = False
            self.query_one("#login-connect", Button).label = self._t("tui.remote_connect")
            self.query_one("#login-status", Static).update(self._t("tui.form_cancelled"))
            for node in self.query("Input, Switch"):
                node.disabled = False

    async def _connect(self) -> None:
        self._connecting = True
        button = self.query_one("#login-connect", Button)
        button.label = self._t("tui.btn_cancel")
        for node in self.query("Input, Switch"):
            node.disabled = True
        try:
            password = self.query_one("#login-pass", Input).value
            with redact_operation_logs(self, extra_secrets=(password,)):
                await self._connect_once()
        except asyncio.CancelledError:
            self.query_one("#login-status", Static).update(self._t("tui.form_cancelled"))
            raise
        except Exception as exc:
            password = self.query_one("#login-pass", Input).value
            message = self._t(
                "tui.form_failed", error=error_detail(self, exc, extra_secrets=(password,))
            )
            self.query_one("#login-status", Static).update(escape(message))
        finally:
            self._connecting = False
            button.label = self._t("tui.remote_connect")
            for node in self.query("Input, Switch"):
                node.disabled = False

    def t(self, key: str, **params: Any) -> str:
        return self._t(key, **params)

    async def _connect_once(self) -> None:
        url = self.query_one("#login-url", Input).value.strip().rstrip("/")
        user = self.query_one("#login-user", Input).value.strip()
        password = self.query_one("#login-pass", Input).value
        save = bool(self.query_one("#login-save-password", Switch).value)
        status = self.query_one("#login-status", Static)
        if not url or not user or not password:
            status.update(f"[red]{self._t('tui.remote_fields_required')}[/red]")
            return
        address = urlsplit(url)
        if (
            address.scheme not in ("http", "https")
            or not address.hostname
            or address.username is not None
        ):
            status.update(self._t("tui.remote_invalid_url"))
            return
        status.update(f"[dim]{self._t('tui.remote_connecting')}[/dim]")
        try:
            async with AsyncClient(base_url=url, timeout=10.0) as probe:
                response = await probe.get("/snapshot", headers=basic_header(user, password))
        except (httpx.HTTPError, ValueError) as exc:
            status.update(
                f"[red]{escape(self._t('tui.remote_unreachable', url=url, error=type(exc).__name__))}[/red]"
            )
            return
        if response.status_code == 401:
            status.update(f"[red]{self._t('tui.remote_credentials_invalid')}[/red]")
            clear_password()
            return
        if response.status_code >= 400:
            status.update(
                f"[red]{self._t('tui.remote_http_error', status=response.status_code)}[/red]"
            )
            return
        session: dict[str, Any] = {"url": url, "username": user, "autologin": bool(save)}
        try:
            payload: Any = response.json()
        except ValueError:
            status.update(self._t("tui.remote_invalid_response"))
            return
        if not isinstance(payload, dict) or "version" not in payload:
            status.update(self._t("tui.remote_invalid_response"))
            return
        try:
            RuntimeSnapshot.model_validate(payload)
        except ValueError:
            status.update(self._t("tui.remote_invalid_response"))
            return
        if isinstance(payload, dict):
            language = cast("dict[str, Any]", payload).get("language")
            if language:
                session["language"] = str(language)
        if save:
            session["saved_password"] = password
        save_session(session)
        status.update(self._t("tui.remote_connected"))
        self.dismiss({"url": url, "username": user, "password": password})


class RemoteServiceAdapter:
    """Duck-typed stand-in for :class:`MailFlowService` backed by REST.

    Domain models and settings use the same types as the local service;
    operations that require a local service raise :class:`RemoteUnsupported`,
    which panes surface in their status lines.
    """

    remote = True

    def __init__(self, client: RemoteClient, snapshot: dict[str, Any], i18n: I18n) -> None:
        self.client = client
        self.i18n = i18n
        self._snapshot = RuntimeSnapshot.model_validate(snapshot)
        self.commands = None
        self.log_queue: queue_module.Queue[Any] = queue_module.Queue()
        self.config = MailFlowConfig()
        self.config.general.timezone = self._snapshot.timezone
        self._sections: list[SettingsSection] = []
        self.events = _EventProxy(client)

        self._apply_language(str(snapshot.get("language") or ""))
        self.client.on("language.changed", self._on_language_changed)

    def _apply_language(self, language: str) -> None:
        """Keep the client-side pack aligned with the remote service."""
        if language and language in self.i18n.available_codes():
            self.i18n.set_language(language)
            self._snapshot.language = language
            self.config.general.language = language

    def _on_language_changed(self, event: str, **payload: Any) -> None:
        self._apply_language(str(payload.get("language") or ""))

    def t(self, key: str, **params: Any) -> str:
        result: str = self.i18n.t(key, **params)
        return result

    def on(self, event: str, handler: Any) -> Any:
        """Subscribe to a live event; delegates to the websocket relay."""
        return self.events.on(event, handler)

    # -- reads -----------------------------------------------------------------
    def snapshot(self) -> RuntimeSnapshot:
        return self._snapshot

    async def refresh_snapshot(self) -> RuntimeSnapshot:
        self._snapshot = RuntimeSnapshot.model_validate(await self.client.snapshot())
        self.config.general.timezone = self._snapshot.timezone
        self._apply_language(self._snapshot.language)
        return self._snapshot

    def snapshot_sync(self) -> dict[str, Any]:
        return self._snapshot.model_dump(mode="json")

    async def list_mails(self, limit: int | None = None) -> list[MailRecord]:
        return [MailRecord.model_validate(item) for item in await self.client.list_mails(limit)]

    async def get_mail(self, record_id: str) -> MailRecord | None:
        data = await self.client.get_mail(record_id)
        return MailRecord.model_validate(data) if data is not None else None

    async def count_mails(self) -> int:
        return len(await self.client.list_mails())

    async def list_actions(self) -> list[ActionItem]:
        return [ActionItem.model_validate(item) for item in await self.client.list_actions()]

    async def list_trash(self) -> list[TrashRecord]:
        return [TrashRecord.model_validate(item) for item in await self.client.list_trash()]

    async def list_failed_mails(self) -> list[MailRecord]:
        return [MailRecord.model_validate(item) for item in await self.client.list_failed_mails()]

    async def list_expired_mails(self) -> list[MailRecord]:
        return [MailRecord.model_validate(item) for item in await self.client.list_expired_mails()]

    def display_text(self, text: str | None) -> str:
        return self.i18n.display_text(text)

    def is_action_expired(self, item: ActionItem) -> bool:
        return action_has_ended(item, datetime.now(UTC))

    # -- writes -----------------------------------------------------------------
    async def set_mail_urgency(self, record_id: str, urgency: Any) -> MailRecord | None:
        data = await self.client.set_mail_urgency(record_id, urgency)
        return MailRecord.model_validate(data) if data is not None else None

    async def delete_mail(self, record_id: str) -> bool:
        return await self.client.delete_mail(record_id)

    async def delete_mails(self, record_ids: list[str]) -> int:
        return await self.client.delete_mails(record_ids)

    async def purge_expired_mails(self, record_ids: list[str] | None = None) -> int:
        return await self.client.purge_expired_mails(record_ids)

    async def restore_mail(self, record_id: str) -> MailRecord | None:
        data = await self.client.restore_mail(record_id)
        return MailRecord.model_validate(data) if data is not None else None

    async def add_action(self, summary: str, due_at: datetime, **values: Any) -> ActionItem:
        return ActionItem.model_validate(
            await self.client.add_action(
                {
                    "summary": summary,
                    "due_at": due_at,
                    **values,
                }
            )
        )

    async def edit_action(self, item_id: str, **values: Any) -> ActionItem:
        return ActionItem.model_validate(await self.client.edit_action(item_id, values))

    async def delete_action(self, item_id: str) -> bool:
        return await self.client.delete_action(item_id)

    async def _reply_call(self, operation: Any) -> Any:
        try:
            return await operation
        except ReplyError as exc:
            raise ReplyError(exc.code, self.t(exc.code, **exc.params), **exc.params) from None
        except AccessDenied:
            raise AccessDenied(self.t("permission.denied")) from None
        except RemoteUnsupported:
            raise
        except PermissionError:
            raise PermissionError(self.t("reply.confirm_invalid")) from None
        except (httpx.HTTPError, RuntimeError):
            raise ReplyError("reply.connection_failed", self.t("reply.connection_failed")) from None

    async def create_reply(self, mail_id: str) -> ReplyDraft:
        return ReplyDraft.model_validate(await self._reply_call(self.client.create_reply(mail_id)))

    async def get_draft(self, draft_id: str) -> ReplyDraft | None:
        data = await self._reply_call(self.client.get_draft(draft_id))
        return ReplyDraft.model_validate(data) if data is not None else None

    async def edit_draft(self, draft_id: str, subject: str, body: str) -> ReplyDraft:
        return ReplyDraft.model_validate(
            await self._reply_call(self.client.edit_draft(draft_id, subject, body))
        )

    async def prepare_reply(self, draft_id: str) -> ReplyDraft:
        return ReplyDraft.model_validate(
            await self._reply_call(self.client.prepare_reply(draft_id))
        )

    async def confirm_reply(self, draft_id: str, token: str) -> ReplyDraft:
        return ReplyDraft.model_validate(
            await self._reply_call(self.client.confirm_reply(draft_id, token))
        )

    async def cancel_reply(self, draft_id: str) -> ReplyDraft:
        return ReplyDraft.model_validate(await self._reply_call(self.client.cancel_reply(draft_id)))

    def settings_sections(self) -> list[SettingsSection]:
        return self._sections

    async def refresh_settings(self) -> None:
        self._sections = [_section(item) for item in await self.client.settings_sections()]

    async def set_setting(self, key: str, value: Any) -> OptionSpec | None:
        data = await self.client.set_setting(key, value)
        await self.refresh_snapshot()
        return _option(data) if data is not None else None

    async def reset_setting(self, key: str) -> OptionSpec | None:
        data = await self.client.reset_setting(key)
        await self.refresh_snapshot()
        return _option(data) if data is not None else None

    async def plugin_enable(self, plugin_id: str) -> str:
        created = await self.client.plugin_enable(plugin_id)
        await self.refresh_snapshot()
        return created

    async def plugin_disable(self, plugin_id: str) -> None:
        await self.client.plugin_disable(plugin_id)
        await self.refresh_snapshot()

    async def plugin_uninstall(self, plugin_id: str) -> str:
        output = await self.client.plugin_uninstall(plugin_id)
        await self.refresh_snapshot()
        return output

    async def plugin_install(self, plugin_id: str) -> str:
        output = await self.client.plugin_install(plugin_id)
        await self.refresh_snapshot()
        return output

    def plugin_status(self, plugin_id: str) -> str:
        plugin = self._snapshot.plugin(plugin_id)
        return plugin.status if plugin is not None else "not_loaded"

    def _unsupported(self, operation: str) -> RemoteUnsupported:
        return RemoteUnsupported(self.t("remote.unsupported", operation=operation))

    async def fetch_history(self, account_id: str, *, limit: int = 50, offset: int = 0) -> Any:
        raise self._unsupported("fetch_history")

    async def process_mail(self, mail: Any, *, force: bool = False) -> Any:
        raise self._unsupported("process_mail")

    async def plugin_install_local(self, root: Path) -> str:
        raise self._unsupported("plugin_install_local")

    async def gateway_provision(
        self, provider: str, instance_id: str, options: dict[str, Any], *, autostart: bool = True
    ) -> Any:
        raise self._unsupported("gateway_provision")

    async def gateway_shutdown(self, provider: str, instance_id: str) -> None:
        raise self._unsupported("gateway_shutdown")

    async def gateway_instances(self) -> list[Any]:
        raise self._unsupported("gateway_instances")

    async def gateway_detect(self, provider: str) -> str:
        raise self._unsupported("gateway_detect")

    async def gateway_qr(self, provider: str, instance_id: str) -> str:
        raise self._unsupported("gateway_qr")

    def gateway_providers(self) -> list[str]:
        raise self._unsupported("gateway_providers")

    async def chat_about_mail(self, record_id: str, messages: list[dict[str, str]]) -> Any:
        raise self._unsupported("chat_about_mail")

    async def apply_mail_correction(
        self, record_id: str, corrections: dict[str, Any], *, note: str = ""
    ) -> Any:
        raise self._unsupported("apply_mail_correction")

    async def smart_action(self, instruction: str, *, progress: Any = None) -> Any:
        raise self._unsupported("smart_action")

    async def list_seminar_candidates(self, **options: Any) -> list[Any]:
        raise self._unsupported("list_seminar_candidates")

    async def import_seminar(self, candidate_id: str, **options: Any) -> Any:
        raise self._unsupported("import_seminar")

    async def reject_seminar(self, candidate_id: str) -> bool:
        raise self._unsupported("reject_seminar")

    async def user_profile(self) -> str:
        raise self._unsupported("user_profile")

    async def set_user_profile(self, text: str) -> str:
        raise self._unsupported("set_user_profile")

    def open_url(self, url: str) -> str:
        raise self._unsupported("open_url")


class _EventProxy:
    """Maps service.on(event, handler) onto the websocket relay."""

    def __init__(self, client: RemoteClient) -> None:
        self._client = client

    def subscribe(self, event: str, handler: Any) -> Any:
        return self._client.on(event, handler)

    on = subscribe


def _option(data: dict[str, Any]) -> OptionSpec:
    return OptionSpec(
        **{
            **data,
            "editor": EditorKind(data["editor"]),
            "choices": tuple(data.get("choices") or ()),
            "item_fields": tuple(_option(item) for item in data.get("item_fields") or ()),
        }
    )


def _section(data: dict[str, Any]) -> SettingsSection:
    return SettingsSection(
        **{
            **data,
            "options": [_option(item) for item in data.get("options", [])],
        }
    )


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
