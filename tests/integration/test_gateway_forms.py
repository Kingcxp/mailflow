"""Gateway guide behavior with fake deployment services; no chat platform processes run."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from mailflow.i18n import I18n
from mailflow_tui.notifications import NotificationsPane
from textual.app import App
from textual.widgets import Button, Static

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
        assert requests == ([] if failure == "qr-timeout" else ["http://fake-endpoint"])

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
    from textual.app import App

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
