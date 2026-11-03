"""Settings operations preserve input, isolate providers and release resources."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Never, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from mailflow.config import LLMConfig, MailAccountConfig, NotifierConfig
from mailflow.contracts import GatewayInstance, LLMCompletion
from mailflow.domain import ComponentKind
from mailflow.forms import FormField
from mailflow.gateway import InstallProgress
from mailflow.registry import PluginRegistrar
from mailflow_tui.gateway_guide import GatewayGuideModal
from mailflow_tui.notifications import NotificationsPane
from mailflow_tui.remote import LoginScreen
from mailflow_tui.settings import EntryFormScreen, ListEditScreen, LLMPane, SettingsPane
from textual.app import App, ComposeResult
from textual.widgets import Button, DataTable, Input, Select, Static, TextArea
from textual.worker import WorkerCancelled

from mailflow_test_support.commands import commands_service
from mailflow_test_support.ui import mock_http_client


def unused_provider(_config: Any) -> Never:
    raise AssertionError("settings forms must not instantiate providers")


async def settle(pilot: Any) -> None:
    await pilot.pause()
    with contextlib.suppress(WorkerCancelled):
        await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def rendered(screen: Any, selector: str) -> str:
    return str(screen.query_one(selector, Static).render())


@pytest.mark.parametrize("language", ["en", "zh-CN"])
@pytest.mark.parametrize("group", ["accounts", "llms", "notifiers"])
async def test_provider_drafts_do_not_cross_and_save_failure_keeps_input(
    group: str,
    language: str,
) -> None:
    service = commands_service()
    service.i18n.set_language(language)
    registrar = PluginRegistrar(service.registry, service.config, "fake")
    kind, register, id_field = {
        "accounts": (ComponentKind.MAIL_SOURCE, registrar.add_source, "account_id"),
        "llms": (ComponentKind.LLM_BACKEND, registrar.add_llm, "llm_id"),
        "notifiers": (ComponentKind.NOTIFIER, registrar.add_notifier, "notifier_id"),
    }[group]
    for provider in ("old", "fresh"):
        register(provider, unused_provider)
    registrar.add_form_fields(kind, "old", (FormField("legacy"),))
    registrar.add_form_fields(
        kind,
        "fresh",
        (
            FormField("endpoint"),
            FormField("access_token", kind="textarea", secret=True),
            FormField("retry_delay", kind="number", default=2.5),
            FormField("instructions", kind="textarea"),
        ),
    )
    values: dict[str, Any] = {
        id_field: "entry",
        "provider": "old",
        "options": {"legacy": "old", "password": "old-secret"},
    }
    if group == "llms":
        values.update(
            model="model", api_key="old-key", base_url="https://old", extra_body={"old": True}
        )
    attempts: list[dict[str, Any]] = []

    async def save(payload: dict[str, Any]) -> None:
        attempts.append(payload)
        if len(attempts) == 1:
            raise OSError("cannot save draft-secret")

    form = EntryFormScreen(service, group, values=values, on_save=save)
    results: list[Any] = []
    app = App[None]()
    async with app.run_test() as pilot:
        app.push_screen(form, results.append)
        await settle(pilot)
        form.query_one("#extra-legacy", Input).value = "unsaved old"
        form.query_one("#field-provider", Select).value = "fresh"
        await settle(pilot)
        form.query_one("#extra-endpoint", Input).value = "https://fresh"
        form.query_one("#extra-access-token", Input).value = "draft-secret"
        assert form.query_one("#extra-access-token", Input).password
        form.query_one("#extra-instructions", TextArea).text = "first line\nsecond line"
        form.query_one("#field-provider", Select).value = "old"
        await settle(pilot)
        assert form.query_one("#extra-legacy", Input).value == "unsaved old"
        form.query_one("#field-provider", Select).value = "fresh"
        await settle(pilot)
        assert form.query_one("#extra-endpoint", Input).value == "https://fresh"
        form.query_one("#entry-form-save", Button).press()
        await settle(pilot)
        assert app.screen is form and not results
        assert form.query_one("#extra-access-token", Input).value == "draft-secret"
        assert "draft-secret" not in rendered(form, "#entry-form-status")
        assert attempts[0]["options"] == {
            "endpoint": "https://fresh",
            "access_token": "draft-secret",
            "retry_delay": 2.5,
            "instructions": "first line\nsecond line",
            **(
                {
                    "mail_notifications": True,
                    "task_reminders": True,
                    "daily_digest": True,
                    "hourly_summary": False,
                }
                if group == "notifiers"
                else {}
            ),
        }
        assert attempts[0].get("api_key", "") == ""
        assert not attempts[0].get("extra_body")
        form.query_one("#entry-form-save", Button).press()
        await settle(pilot)
        assert results == [attempts[1]]


async def test_model_page_save_failure_is_retryable_without_retyping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from mailflow import service as service_module

    service = commands_service(config_path=str(tmp_path / "config.toml"))
    registrar = PluginRegistrar(service.registry, service.config, "fake")
    registrar.add_llm("openai-completions", unused_provider)
    monkeypatch.setattr(service, "reload_runtime", AsyncMock())
    write = service_module.write_config
    attempts = 0

    def fail_once(*args: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("disk unavailable")
        write(*args)

    monkeypatch.setattr(service_module, "write_config", fail_once)
    pane = LLMPane(service)

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield pane

    async with Host().run_test() as pilot:
        pane.query_one("#llm-add", Button).press()
        await settle(pilot)
        form = pilot.app.screen
        form.query_one("#field-llm-id", Input).value = "local"
        form.query_one("#field-model", Input).value = "local-model"
        form.query_one("#entry-form-save", Button).press()
        await settle(pilot)
        assert pilot.app.screen is form
        assert not service.config.llms
        assert form.query_one("#field-model", Input).value == "local-model"
        form.query_one("#entry-form-save", Button).press()
        await settle(pilot)
        assert pilot.app.screen is not form
        assert [llm.llm_id for llm in service.config.llms] == ["local"]
        assert service.config.llms[0].api_key == ""
        assert "local-model" in (tmp_path / "config.toml").read_text(encoding="utf-8")
        # A reload failure is reported as already saved, so retrying cannot add a duplicate.
        monkeypatch.setattr(
            service, "reload_runtime", AsyncMock(side_effect=RuntimeError("apply reload-secret"))
        )
        pane.query_one("#llm-add", Button).press()
        await settle(pilot)
        second = pilot.app.screen
        second.query_one("#field-llm-id", Input).value = "second"
        second.query_one("#field-model", Input).value = "second-model"
        second.query_one("#extra-api-key", Input).value = "reload-secret"
        second.query_one("#entry-form-save", Button).press()
        await settle(pilot)
        assert pilot.app.screen is not second
        assert [llm.llm_id for llm in service.config.llms] == ["local", "second"]
        assert "reload-secret" not in rendered(pane, "#llms-status")
        assert "reload-secret" not in caplog.text
        assert service.t(
            "tui.settings_reload_failed", error=service.t("errors.operation")
        ) in rendered(pane, "#llms-status")


async def test_probe_cancellation_closes_backend_and_errors_redact_new_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = commands_service()
    entered, closed = asyncio.Event(), asyncio.Event()
    outcome = "wait"

    class Backend:
        backend_id = "fake"

        async def chat(self, *_args: Any, **_kwargs: Any) -> LLMCompletion:
            entered.set()
            if outcome == "wait":
                await asyncio.Future()
            if outcome == "error":
                logging.getLogger("fake.settings.probe").warning("bad new-secret")
                raise RuntimeError("bad new-secret")
            return LLMCompletion(text="private model output", model="fake")

        async def aclose(self) -> None:
            closed.set()

    registrar = PluginRegistrar(service.registry, service.config, "fake")
    registrar.add_llm("openai-completions", lambda _config: Backend())
    form = EntryFormScreen(service, "llms")
    app = App[None]()
    async with app.run_test() as pilot:
        app.push_screen(form)
        await settle(pilot)
        form.query_one("#field-llm-id", Input).value = "main"
        form.query_one("#field-model", Input).value = "fake-model"
        form.query_one("#extra-api-key", Input).value = "new-secret"
        form.query_one("#entry-form-test", Button).press()
        await pilot.pause()
        assert entered.is_set(), rendered(form, "#entry-form-status")
        await asyncio.wait_for(entered.wait(), 2)
        await pilot.pause()
        assert form.query_one("#entry-form-save", Button).disabled
        form.query_one("#entry-form-test", Button).press()
        await settle(pilot)
        assert closed.is_set()
        assert rendered(form, "#entry-form-status") == service.t("tui.form_cancelled")
        outcome = "error"
        form.query_one("#entry-form-test", Button).press()
        await settle(pilot)
        assert "new-secret" not in rendered(form, "#entry-form-status")
        assert "new-secret" not in caplog.text and "***" in caplog.text
        outcome = "success"
        form.query_one("#entry-form-test", Button).press()
        await settle(pilot)
        assert "fake" in rendered(form, "#entry-form-status")
        assert "private model output" not in rendered(form, "#entry-form-status")


async def test_mailbox_probe_uses_account_options_and_cancel_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imaplib
    import smtplib

    class Sender:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def login(self, *args: Any) -> None:
            pass

        def quit(self) -> None:
            pass

        def close(self) -> None:
            pass

        def starttls(self) -> None:
            pass

    monkeypatch.setattr(smtplib, "SMTP_SSL", Sender)
    monkeypatch.setattr(smtplib, "SMTP", Sender)

    service = commands_service()
    clients: list[Any] = []
    entered = threading.Event()
    outcome = "wait"

    class Client:
        def __init__(self, host: str, port: int, **_kwargs: Any) -> None:
            self.address = (host, port)
            self.released, self.closed = threading.Event(), threading.Event()
            clients.append(self)

        def login(self, username: str, password: str) -> None:
            self.credentials = (username, password)
            entered.set()
            if outcome == "wait":
                assert self.released.wait(3)
            elif outcome == "error":
                raise OSError(f"failed with {password}")

        def shutdown(self) -> None:
            self.released.set()

        def logout(self) -> None:
            self.closed.set()

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Client)
    form = EntryFormScreen(
        service,
        "accounts",
        values={
            "account_id": "mail",
            "provider": "imap",
            "email": "display@example.test",
            "options": {
                "imap_host": "imap.custom.test",
                "imap_port": 999,
                "smtp_host": "smtp.custom.test",
                "username": "login-user",
                "password": "mail-secret",
            },
        },
    )
    app = App[None]()
    results: list[Any] = []
    async with app.run_test() as pilot:
        app.push_screen(form, results.append)
        await settle(pilot)
        form.query_one("#entry-form-test", Button).press()
        assert await asyncio.to_thread(entered.wait, 3)
        assert clients[0].address == ("imap.custom.test", 999)
        assert clients[0].credentials == ("login-user", "mail-secret")
        form.query_one("#entry-form-test", Button).press()
        await settle(pilot)
        assert await asyncio.to_thread(clients[0].closed.wait, 3)
        assert clients[0].released.is_set()
        assert form.query_one("#extra-password", Input).value == "mail-secret"
        outcome = "error"
        form.query_one("#entry-form-test", Button).press()
        await settle(pilot)
        assert "mail-secret" not in rendered(form, "#entry-form-status")
        outcome = "success"
        form.query_one("#extra-username", Input).value = ""
        form.query_one("#entry-form-test", Button).press()
        await settle(pilot)
        assert clients[-1].credentials == ("display@example.test", "mail-secret")
        form.query_one("#extra-preset", Input).value = "gmail"
        form.query_one("#entry-form-save", Button).press()
        await settle(pilot)
        assert results[0]["options"]["preset"] == "gmail"
        assert results[0]["options"]["imap_host"] == "imap.gmail.com"
        assert "smtp_host" not in results[0]["options"]


async def test_notifier_probe_uses_edited_token_and_does_not_gate_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = commands_service()
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401)

    mock_http_client(monkeypatch, httpx.MockTransport(respond))
    registrar = PluginRegistrar(service.registry, service.config, "fake")
    registrar.add_notifier("onebot", unused_provider)
    registrar.add_form_fields(
        ComponentKind.NOTIFIER,
        "onebot",
        (
            FormField("http_url"),
            FormField("access_token", kind="password", secret=True),
        ),
    )
    form = EntryFormScreen(
        service,
        "notifiers",
        values={
            "notifier_id": "chat",
            "provider": "onebot",
            "options": {"http_url": "http://old", "access_token": "old"},
        },
    )
    app = App[None]()
    async with app.run_test() as pilot:
        app.push_screen(form)
        await settle(pilot)
        form.query_one("#extra-http-url", Input).value = "http://fresh"
        form.query_one("#extra-access-token", Input).value = "edited-token"
        form.query_one("#entry-form-test", Button).press()
        await settle(pilot)
        assert requests[0].headers["Authorization"] == "Bearer edited-token"
        assert requests[0].url.host == "fresh"
        assert service.t("tui.bots_http_status", status=401) in rendered(form, "#entry-form-status")
        assert not form.query_one("#entry-form-save", Button).disabled


async def test_notifier_page_rename_and_delete_recover_from_save_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = commands_service(config_path=str(tmp_path / "config.toml"))
    service.config.notifiers = [
        NotifierConfig(notifier_id="old", provider="fake", options={"access_token": "saved-token"})
    ]
    registrar = PluginRegistrar(service.registry, service.config, "fake")
    registrar.add_notifier("fake", unused_provider)
    registrar.add_form_fields(
        ComponentKind.NOTIFIER, "fake", (FormField("access_token", kind="password", secret=True),)
    )

    async def probe(_options: Any, _t: Any) -> str:
        return "failure: saved-token"

    registrar.add_probe(ComponentKind.NOTIFIER, "fake", probe)
    monkeypatch.setattr(service, "reload_runtime", AsyncMock())
    update = service.update_config_entry
    attempts = 0

    async def retry_update(*args: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("disk unavailable")
        await update(*args)

    monkeypatch.setattr(service, "update_config_entry", retry_update)
    pane = NotificationsPane(service)

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield pane

    async with Host().run_test() as pilot:
        await settle(pilot)
        table = cast(DataTable[Any], pane.query_one("#notifications-table", DataTable))
        assert "saved-token" not in str(table.get_row("old")[-1])
        pane.query_one("#notif-edit", Button).press()
        await settle(pilot)
        form = pilot.app.screen
        form.query_one("#field-notifier-id", Input).value = "renamed"
        form.query_one("#entry-form-save", Button).press()
        await settle(pilot)
        assert pilot.app.screen is form
        assert service.config.notifiers[0].notifier_id == "old"
        form.query_one("#entry-form-save", Button).press()
        await settle(pilot)
        assert [entry.notifier_id for entry in service.config.notifiers] == ["renamed"]
        remove = service.remove_config_entry
        monkeypatch.setattr(
            service, "remove_config_entry", AsyncMock(side_effect=OSError("read only"))
        )
        pane.query_one("#notif-delete", Button).press()
        await settle(pilot)
        assert service.config.notifiers
        assert service.t("errors.storage") in rendered(pane, "#notifications-status")
        assert "read only" not in rendered(pane, "#notifications-status")
        monkeypatch.setattr(service, "remove_config_entry", remove)
        pane.query_one("#notif-delete", Button).press()
        await settle(pilot)
        assert not service.config.notifiers and table.row_count == 0


async def test_remote_login_can_cancel_recover_and_retry_session_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mailflow_tui import remote

    service = commands_service()
    entered = asyncio.Event()
    mode = "wait"
    clients: list[httpx.AsyncClient] = []

    async def respond(_request: httpx.Request) -> httpx.Response:
        entered.set()
        if mode == "wait":
            await asyncio.Future()
        if mode == "offline":
            return httpx.Response(503)
        if mode == "invalid":
            return httpx.Response(200, json={"unexpected": True})
        return httpx.Response(200, json={"version": "test", "language": "en"})

    def make_client(**kwargs: Any) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(remote, "AsyncClient", make_client)
    monkeypatch.setattr(remote, "_SESSION_FILE", tmp_path / "session.json")
    saved: list[Any] = []
    form = LoginScreen({})
    app = App[None]()
    async with app.run_test() as pilot:
        app.push_screen(form, saved.append)
        await settle(pilot)
        for key, value in {"url": "http://fake", "user": "admin", "pass": "secret"}.items():
            form.query_one(f"#login-{key}", Input).value = value
        form.query_one("#login-connect", Button).press()
        await asyncio.wait_for(entered.wait(), 2)
        await pilot.pause()
        form.query_one("#login-connect", Button).press()
        await settle(pilot)
        assert clients[0].is_closed
        assert rendered(form, "#login-status") == service.t("tui.form_cancelled")
        for response_mode in ("offline", "invalid"):
            mode = response_mode
            form.query_one("#login-connect", Button).press()
            await settle(pilot)
            assert app.screen is form and not saved
            assert form.query_one("#login-pass", Input).value == "secret"
            assert "secret" not in rendered(form, "#login-status")
        mode = "success"
        original = remote.save_session
        monkeypatch.setattr(remote, "save_session", Mock(side_effect=OSError("read only")))
        form.query_one("#login-connect", Button).press()
        await settle(pilot)
        assert app.screen is form and not saved
        monkeypatch.setattr(remote, "save_session", original)
        form.query_one("#login-connect", Button).press()
        await settle(pilot)
        assert saved == [{"url": "http://fake", "username": "admin", "password": "secret"}]
        assert "secret" not in (tmp_path / "session.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("phase", ["install", "login", "endpoint"])
async def test_gateway_cancel_waits_for_operation_and_releases_resources(
    phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = commands_service()
    entered, released = asyncio.Event(), asyncio.Event()
    qr_ready = asyncio.Event()
    progress = InstallProgress(stage="installing", message="internal token", percent=40)
    monkeypatch.setattr(service, "gateways", SimpleNamespace(progress=Mock(return_value=progress)))
    service.gateway_detect = AsyncMock(return_value="")
    service.gateway_shutdown = AsyncMock()

    async def provision(*_args: Any, **_kwargs: Any) -> GatewayInstance:
        if phase == "install":
            entered.set()
            try:
                await asyncio.Future()
            finally:
                released.set()
        return GatewayInstance(provider="napcat", instance_id="chat", status="running")

    async def qr(*_args: Any) -> str:
        if phase == "endpoint":
            qr_ready.set()
            return "https://fake/login"
        entered.set()
        try:
            await asyncio.Future()
        finally:
            released.set()
        return ""

    async def shutdown(*_args: Any) -> None:
        assert released.is_set()

    service.gateway_provision = provision
    monkeypatch.setattr(service, "gateway_qr", qr)
    service.gateway_shutdown.side_effect = shutdown
    guide = GatewayGuideModal(service, "napcat", "chat", {"access_token": "internal token"})

    async def endpoint() -> bool:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            released.set()
        return True

    monkeypatch.setattr(guide, "_wait_for_endpoint", endpoint)
    app = App[None]()
    results: list[Any] = []
    async with app.run_test() as pilot:
        app.push_screen(guide, results.append)
        if phase == "endpoint":
            await asyncio.wait_for(qr_ready.wait(), 3)
            await pilot.pause()
            assert not guide.query_one("#guide-logged-in", Button).disabled
            guide.query_one("#guide-logged-in", Button).press()
        await asyncio.wait_for(entered.wait(), 3)
        await pilot.pause()
        guide.query_one("#guide-cancel", Button).press()
        await settle(pilot)
        assert results == [None]
        assert released.is_set()
        service.gateway_shutdown.assert_awaited_once_with("napcat", "chat")


async def test_gateway_failure_redacts_unsaved_credentials_and_cleans_up() -> None:
    service = commands_service()
    secret = "token[private]"
    service.gateway_detect = AsyncMock(return_value="")
    service.gateway_provision = AsyncMock(side_effect=OSError(f"failed with {secret}"))
    service.gateway_shutdown = AsyncMock()
    guide = GatewayGuideModal(service, "napcat", "chat", {"access_token": secret})
    app = App[None]()
    async with app.run_test() as pilot:
        app.push_screen(guide)
        await settle(pilot)
        assert secret not in rendered(guide, "#guide-status")
        assert service.t("errors.storage") in rendered(guide, "#guide-status")
        assert guide.query_one("#guide-done", Button).disabled
        service.gateway_shutdown.assert_awaited_with("napcat", "chat")


async def test_settings_reload_keeps_other_unsaved_fields_and_failed_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = commands_service(config_path=str(tmp_path / "config.toml"))
    service.config.accounts = [MailAccountConfig(account_id="mail", provider="fake")]
    service.config.llms = [LLMConfig(llm_id="model")]
    service.config.notifiers = [NotifierConfig(notifier_id="notify", provider="console")]
    pane = SettingsPane(service)
    monkeypatch.setattr(service, "reload_runtime", AsyncMock())

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield pane

    async with Host().run_test() as pilot:
        await settle(pilot)
        query = pane.query_one("#settings-search", Input)
        query.value = "general."
        await settle(pilot)
        draft = pane.query_one("#value-general-workers", Input)
        draft.value = "7"
        service.i18n.set_language("zh-CN")
        await pane.relabel()
        await settle(pilot)
        assert pane.query_one("#value-general-workers", Input).value == "7"
        pane.query_one("#value-general-queue-size", Input).value = "33"
        pane.query_one("#save-general-queue-size", Button).press()
        await settle(pilot)
        assert service.config.general.queue_size == 33
        assert pane.query_one("#value-general-workers", Input).value == "7"
        monkeypatch.setattr(service, "reset_setting", AsyncMock(side_effect=OSError("read only")))
        pane.query_one("#reset-general-workers", Button).press()
        await settle(pilot)
        assert pane.query_one("#value-general-workers", Input).value == "7"
        assert service.t("errors.storage") in rendered(pane, "#settings-status")
        assert "read only" not in rendered(pane, "#settings-status")
        assert not pane.query_one("#save-general-workers", Button).disabled


async def test_mapping_editor_keeps_edits_after_save_failure() -> None:
    service = commands_service()
    service.config.notifiers = [NotifierConfig(notifier_id="notify", provider="console")]
    spec = service.settings_option("notifiers[0].options")
    assert spec is not None
    save = AsyncMock(side_effect=[OSError("disk unavailable draft-secret"), None])
    form = ListEditScreen(service, spec, on_save=save)
    app = App[None]()
    results: list[Any] = []
    async with app.run_test() as pilot:
        app.push_screen(form, results.append)
        await settle(pilot)
        form.query_one("#list-edit-text", TextArea).text = "access_token = draft-secret"
        form.query_one("#list-edit-save", Button).press()
        await settle(pilot)
        assert app.screen is form and not results
        assert form.query_one("#list-edit-text", TextArea).text == "access_token = draft-secret"
        assert "draft-secret" not in rendered(form, "#list-edit-status")
        form.query_one("#list-edit-save", Button).press()
        await settle(pilot)
        assert results == ["access_token = draft-secret"]


@pytest.mark.parametrize("width", [85, 130])
async def test_password_fields_align_and_visibility_toggle_keeps_input(width: int) -> None:
    import mailflow_tui.app as ui

    service = commands_service()
    registrar = PluginRegistrar(service.registry, service.config, "fake")
    registrar.add_source("fake", unused_provider)
    registrar.add_form_fields(
        ComponentKind.MAIL_SOURCE,
        "fake",
        (FormField("login"), FormField("password", secret=True)),
    )

    class FormApp(App[None]):
        CSS_PATH = Path(ui.__file__).with_name("app.tcss")

    app = FormApp()
    form = EntryFormScreen(service, "accounts", values={"account_id": "test", "provider": "fake"})
    async with app.run_test(size=(width, 48)) as pilot:
        app.push_screen(form)
        await settle(pilot)
        login = form.query_one("#extra-login", Input)
        password = form.query_one("#extra-password", Input)
        eye = form.query_one("#extra-password-eye", Button)
        password.scroll_visible(animate=False)
        await pilot.pause()
        assert (password.region.x, password.region.width) == (login.region.x, login.region.width)
        assert password.region.x < eye.region.x < eye.region.right < password.region.right
        password.value = "keep-this-secret"
        await pilot.click(eye)
        assert not password.password and password.value == "keep-this-secret"
        await pilot.click(eye)
        assert password.password and password.value == "keep-this-secret"
