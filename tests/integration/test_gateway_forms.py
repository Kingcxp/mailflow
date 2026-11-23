"""Gateway guide behavior with fake deployment services; no chat platform processes run."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from mailflow.i18n import I18n
from mailflow_tui.notifications import NotificationsPane
from rich.cells import cell_len
from textual.widgets import Button, Static

from mailflow_test_support.tui import ThemedApp as App
from mailflow_test_support.ui import until


class _Service:
    def __init__(self) -> None:
        self.config = SimpleNamespace(notifiers=[])
        self.added: list[tuple[str, dict[str, Any]]] = []

    async def add_config_entry(self, group: str, values: dict[str, Any]) -> None:
        self.added.append((group, values))

    def t(self, key: str, **_: Any) -> str:
        return key


class _Status:
    def update(self, _: str) -> None:
        pass


@pytest.mark.parametrize("reinstall", [False, True])
async def test_existing_gateway_menu_reuses_or_reinstalls_the_same_instance(
    reinstall: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailflow.config import MailFlowConfig, NotifierConfig
    from mailflow.contracts import GatewayInstance
    from mailflow.domain import Urgency
    from mailflow.registry import PluginRegistrar
    from mailflow_tui.confirm import ConfirmModal
    from mailflow_tui.gateway_guide import GatewayGuideModal
    from mailflow_tui.pickers import PopupMenu
    from textual.app import ComposeResult

    from mailflow_test_support.commands import commands_service

    entry = NotifierConfig(
        notifier_id="existing-qq",
        provider="onebot",
        enabled=False,
        minimum_urgency=Urgency.IMPORTANT,
        options={
            "gateway": "napcat",
            "http_url": "http://fake",
            "access_token": "saved-token",
            "admins": ["owner"],
            "targets": ["group:22"],
        },
    )
    service = commands_service(
        MailFlowConfig(notifiers=[entry]), config_path=str(tmp_path / "mailflow.toml")
    )
    monkeypatch.setattr(service, "reload_runtime", AsyncMock())
    started: list[str] = []
    installed: list[str] = []

    class ExistingGateway:
        backend_id = "napcat"

        async def detect(self) -> str:
            assert reinstall, "Re-login does not need an installation check"
            return "gateway.napcat_archive"

        async def install(self, instance_id: str, options: dict[str, Any]) -> None:
            assert reinstall, "Re-login must not install or download"
            assert options["_reinstall"] is True
            installed.append(instance_id)

        async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
            started.append(instance_id)
            return await self.status(instance_id)

        async def status(self, instance_id: str) -> GatewayInstance:
            return GatewayInstance(
                provider="napcat", instance_id=instance_id, status="running", endpoint="http://fake"
            )

        async def qr(self, instance_id: str) -> str:
            return "__MAILFLOW_LOGGED_IN__"

        async def stop(self, instance_id: str) -> None:
            pass

    PluginRegistrar(service.registry, service.config, "test").add_gateway_provisioner(
        "napcat", ExistingGateway
    )
    pane = NotificationsPane(service)

    class Host(App[None]):
        CSS_PATH = (
            Path(__file__).resolve().parents[2] / "packages/mailflow-tui/src/mailflow_tui/app.tcss"
        )

        def compose(self) -> ComposeResult:
            yield pane

    try:
        async with Host().run_test(size=(120, 40)) as pilot:
            action = "notif-reinstall" if reinstall else "notif-relogin"

            async def choose_action() -> None:
                pane.query_one("#notif-more", Button).press()
                await until(pilot, lambda: bool(pilot.app.screen.query(PopupMenu)))
                menu = pilot.app.screen.query_one(PopupMenu)
                menu.highlighted = menu.get_option_index(action)
                await pilot.press("enter")

            await until(pilot, lambda: not pane.query_one("#notif-more", Button).disabled)
            await choose_action()
            if reinstall:
                await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmModal))
                pilot.app.screen.query_one("#confirm-cancel", Button).press()
                await until(pilot, lambda: not isinstance(pilot.app.screen, ConfirmModal))
                assert started == [] and installed == []
                await choose_action()
                await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmModal))
                pilot.app.screen.query_one("#confirm-run", Button).press()
            await until(pilot, lambda: isinstance(pilot.app.screen, GatewayGuideModal))
            guide = cast(GatewayGuideModal, pilot.app.screen)
            await until(pilot, lambda: not guide.query_one("#guide-done", Button).disabled)
            assert started == ["existing-qq"]
            assert installed == (["existing-qq"] if reinstall else [])
            instance = service.gateways.instance("napcat", "existing-qq")
            assert instance is not None and "_reinstall" not in instance.extra["options"]
            assert "_confirm_docker_setup" not in instance.extra["options"]
            for width, height in ((120, 40), (80, 24), (60, 24)):
                await pilot.resize_terminal(width, height)
                buttons = list(guide.query_one("#guide-actions").query(Button))
                await until(
                    pilot,
                    lambda buttons=buttons: all(
                        guide.region.contains_region(button.region)
                        and button.region.width >= cell_len(str(button.label)) + 2
                        for button in buttons
                    ),
                )
                assert all(button.region.height == 1 for button in buttons)
                if width != 80:
                    assert len({button.region.y for button in buttons}) == (
                        1 if width == 120 else 2
                    )
                assert all(guide.region.contains_region(button.region) for button in buttons), (
                    width,
                    guide.region,
                    [(button.id, button.region) for button in buttons],
                )
                assert all(
                    button.region.width >= cell_len(str(button.label)) + 2 for button in buttons
                ), (width, [(str(button.label), button.region) for button in buttons])
            await pilot.resize_terminal(120, 40)
            guide.query_one("#guide-done", Button).press()
            await until(
                pilot,
                lambda: (
                    service.t("tui.notifications_configured_ok", instance="existing-qq")
                    in str(pane.query_one("#notifications-status", Static).render())
                ),
            )
            assert [notifier.model_dump() for notifier in service.config.notifiers] == [
                entry.model_dump()
            ]
    finally:
        await service.gateways.stop()


async def test_docker_setup_confirmation_password_and_qr_can_be_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mailflow_tui import gateway_guide
    from mailflow_tui.confirm import ConfirmModal
    from mailflow_tui.widgets import Input

    def fake_qr(_: str) -> str:
        return "\n".join(["██  ██"] * 20)

    monkeypatch.setattr(gateway_guide, "_ascii_qr", fake_qr)

    async def check(decision: str) -> None:
        approve = decision != "decline"

        class GuideService:
            def __init__(self) -> None:
                self.t = I18n("zh-CN").t
                self.gateways = self
                self.started = False
                self.shutdowns = 0

            async def gateway_detect(self, provider: str) -> str:
                return "gateway.napcat_archive"

            async def gateway_provision(
                self, provider: str, instance_id: str, options: dict[str, Any]
            ) -> Any:
                if not await options["_confirm_docker_setup"]():
                    raise asyncio.CancelledError
                password = await asyncio.to_thread(options["_ask_sudo_password"])
                assert password == "private-password" and password not in str(options)
                self.started = True
                return SimpleNamespace(endpoint="http://fake-endpoint")

            def progress(self, provider: str, instance_id: str) -> None:
                return None

            async def gateway_qr(self, provider: str, instance_id: str) -> str:
                return "fake-qr-data"

            async def gateway_shutdown(self, provider: str, instance_id: str) -> None:
                self.shutdowns += 1

        service = GuideService()
        guide = gateway_guide.GatewayGuideModal(cast(Any, service), "napcat", "qq", {})
        results: list[dict[str, Any] | None] = []

        class GuideApp(App[None]):
            CSS_PATH = (
                Path(__file__).resolve().parents[2]
                / "packages/mailflow-tui/src/mailflow_tui/app.tcss"
            )

            def on_mount(self) -> None:
                self.push_screen(guide, results.append)

        app = GuideApp()
        async with app.run_test(size=(100, 36)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
            await pilot.pause()
            assert await pilot.click("#confirm-run" if approve else "#confirm-cancel")
            if approve:
                await until(pilot, lambda: guide.query_one("#sudo-prompt-input", Input).has_focus)
                field = guide.query_one("#sudo-prompt-input", Input)
                assert field.password
                await until(
                    pilot,
                    lambda: guide.region.contains_region(
                        guide.query_one("#guide-cancel", Button).region
                    ),
                )
                if decision == "cancel-password":
                    assert await pilot.click("#guide-cancel")
                    await until(pilot, lambda: results == [None])
                    assert not service.started and service.shutdowns >= 1
                    return
                field.value = "private-password"
                await pilot.click("#sudo-prompt-ok")
                await until(pilot, lambda: not guide.query_one("#guide-view-qr", Button).disabled)
                assert field.value == "" and service.started
                await pilot.resize_terminal(60, 24)
                await until(
                    pilot,
                    lambda: all(
                        guide.region.contains_region(button.region)
                        for button in guide.query_one("#guide-actions").query(Button)
                    ),
                )
                await pilot.click("#guide-view-qr")
                await until(pilot, lambda: isinstance(app.screen, gateway_guide.QRScreen))
                await pilot.click("#qr-close")
                await until(pilot, lambda: app.screen is guide)
                await pilot.pause()
                assert await pilot.click("#guide-cancel"), [
                    (button.id, button.region)
                    for button in guide.query_one("#guide-actions").query(Button)
                ]
            await until(pilot, lambda: results == [None])
            assert service.started == approve and service.shutdowns >= 1

    for decision in ("decline", "cancel-password", "approve"):
        await check(decision)


@pytest.mark.parametrize("failure", ["qr-timeout", "unreachable-endpoint"])
@pytest.mark.parametrize("language", ["en", "zh-CN"])
async def test_failed_guide_cannot_save_and_can_be_cancelled(
    failure: str, language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailflow_tui import gateway_guide

    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            current = self.now
            if failure == "qr-timeout":
                self.now += 301.0
            return current

    clock = Clock()
    monkeypatch.setattr(gateway_guide, "time", clock)
    client_type = httpx.AsyncClient
    requests: list[str] = []

    def unreachable(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        clock.now += 31.0
        raise httpx.ConnectError("fake endpoint is offline", request=request)

    def http_client(**kwargs: Any) -> httpx.AsyncClient:
        return client_type(transport=httpx.MockTransport(unreachable), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", http_client)

    class GuideService:
        def __init__(self) -> None:
            self.shutdowns: list[tuple[str, str]] = []
            self.gateways = self
            self.t = I18n(language).t

        async def gateway_detect(self, provider: str) -> str:
            return "ready"

        async def gateway_provision(self, *args: Any) -> Any:
            return SimpleNamespace(endpoint="http://fake-endpoint")

        def progress(self, provider: str, instance_id: str) -> None:
            return None

        async def gateway_qr(self, provider: str, instance_id: str) -> str:
            return "__MAILFLOW_LOGGED_IN__"

        async def gateway_check(self, provider: str, instance_id: str) -> Any:
            clock.now += 31.0
            return SimpleNamespace(status="starting", extra={"logged_in": False}, error="")

        async def gateway_shutdown(self, provider: str, instance_id: str) -> None:
            self.shutdowns.append((provider, instance_id))

    service = GuideService()
    guide = gateway_guide.GatewayGuideModal(cast(Any, service), "wechatpadpro", "one", {})
    results: list[dict[str, Any] | None] = []

    class GuideApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(guide, results.append)

    app = GuideApp()
    async with app.run_test(size=(140, 50)) as pilot:
        await until(pilot, lambda: bool(service.shutdowns))
        key = (
            "tui.bots_guide_qr_timeout"
            if failure == "qr-timeout"
            else "tui.bots_guide_endpoint_pending"
        )
        message = service.t(key, endpoint="http://fake-endpoint")
        assert message in str(guide.query_one("#guide-status", Static).render())
        assert guide.query_one("#guide-done", Button).disabled
        await pilot.click("#guide-done")
        assert app.screen is guide and results == []
        assert service.shutdowns == [("wechatpadpro", "one")]
        assert requests == []  # Readiness uses the platform session, not the landing page.

        await pilot.press("escape")
        await until(pilot, lambda: app.screen is not guide)
        assert results == [None]


@pytest.mark.asyncio
async def test_guided_wechatpadpro_notifier_uses_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed deployment must create a notifier the adapter can reach."""
    service = _Service()
    pane = NotificationsPane(cast(Any, service))
    status = _Status()

    def no_refresh() -> None:
        pass

    def status_node(*_: Any) -> _Status:
        return status

    monkeypatch.setattr(pane, "refresh_data", no_refresh)
    monkeypatch.setattr(pane, "query_one", status_node)

    await pane._save_guided_notifier(  # pyright: ignore[reportPrivateUsage]
        "wechatpadpro",
        "wechat-1",
        {"endpoint": "http://127.0.0.1:8101"},
        {"options": {"admins": ["wxid-owner"]}},
    )

    assert service.added == [
        (
            "notifiers",
            {
                "notifier_id": "wechat-1",
                "provider": "wechatpadpro",
                "options": {
                    "admins": ["wxid-owner"],
                    "base_url": "http://127.0.0.1:8101",
                    "gateway": "wechatpadpro",
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_guide_escape_cancels_deployment_and_shuts_down() -> None:
    """Escape has the same cleanup semantics as the visible Cancel button."""
    from mailflow_tui.gateway_guide import GatewayGuideModal

    class GuideService:
        def __init__(self) -> None:
            self.gateways = self
            self.provision_started = asyncio.Event()
            self.provision_cancelled = asyncio.Event()
            self.shutdowns: list[tuple[str, str]] = []

        def progress(self, provider: str, instance_id: str) -> None:
            return None

        def t(self, key: str, **_: Any) -> str:
            return key

        async def gateway_detect(self, _: str) -> str:
            return "ready"

        async def gateway_provision(self, *_: Any) -> None:
            self.provision_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.provision_cancelled.set()

        async def gateway_shutdown(self, provider: str, instance_id: str) -> None:
            self.shutdowns.append((provider, instance_id))

    class GuideApp(App[None]):
        def __init__(self, guide: GatewayGuideModal) -> None:
            super().__init__()
            self._guide = guide

        def on_mount(self) -> None:
            self.push_screen(self._guide)

    service = GuideService()
    guide = GatewayGuideModal(cast(Any, service), "wechatpadpro", "wechat-1", {})
    app = GuideApp(guide)
    async with app.run_test(size=(140, 50)) as pilot:
        for _ in range(40):
            if service.provision_started.is_set():
                break
            await pilot.pause(0.05)
        assert service.provision_started.is_set()

        await pilot.press("escape")
        for _ in range(40):
            if service.shutdowns and service.provision_cancelled.is_set():
                break
            await pilot.pause(0.05)

        assert service.shutdowns == [("wechatpadpro", "wechat-1")]
        assert service.provision_cancelled.is_set()
        assert app.screen is not guide


@pytest.mark.parametrize(
    "provider,payload",
    [
        ("onebot", {"status": "failed", "retcode": 100}),
        ("telegram", {"ok": False}),
        ("openwechat", {"logged_in": False}),
        ("whatsapp", {"logged_in": False}),
    ],
)
async def test_http_success_without_platform_login_is_not_online(
    provider: str, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailflow_tui.notifications import NotifierProbe

    client_type = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return client_type(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
            **kwargs,
        )

    monkeypatch.setattr(httpx, "AsyncClient", client)
    i18n = I18n("zh-CN")
    result = await NotifierProbe.probe(
        provider,
        {
            "http_url": "http://fake",
            "gateway_url": "http://fake",
            "api_base": "http://fake",
            "bot_token": "fake",
        },
        i18n.t,
    )
    assert result == i18n.t("tui.bots_not_logged_in")
