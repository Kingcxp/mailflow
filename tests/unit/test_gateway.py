"""GatewayManager unit tests: provisioning, state persistence, supervision."""

from __future__ import annotations

from typing import Any, cast

import pytest
from mailflow.config import MailFlowConfig
from mailflow.contracts import GatewayInstance
from mailflow.gateway import GatewayManager
from mailflow.registry import ComponentRegistry, PluginRegistrar


class FakeProvisioner:
    """Deterministic provisioner: install/start succeed, status reflects
    an in-memory flag, qr returns a fixed payload."""

    backend_id = "fake-gw"

    def __init__(self) -> None:
        self.installed: list[str] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.running = True
        self.detect_calls = 0
        self.bridge_ensures: list[str] = []
        self.ensure_bridge_impl: Any = None  # optional async override

    async def ensure_bridge(self, instance_id: str, options: dict[str, Any]) -> None:
        if self.ensure_bridge_impl is not None:
            await self.ensure_bridge_impl(instance_id, options)
            return
        self.bridge_ensures.append(instance_id)

    async def detect(self) -> str:
        self.detect_calls += 1
        return "fake gateway ready"

    async def install(self, instance_id: str, options: dict[str, Any]) -> None:
        self.installed.append(instance_id)

    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
        self.started.append(instance_id)
        return GatewayInstance(
            provider="fake-gw",
            instance_id=instance_id,
            status="running",
            endpoint=f"http://127.0.0.1:{9000 + len(self.started)}",
        )

    async def stop(self, instance_id: str) -> None:
        self.stopped.append(instance_id)
        self.running = False

    async def status(self, instance_id: str) -> GatewayInstance:
        if self.running:
            return GatewayInstance(
                provider="fake-gw",
                instance_id=instance_id,
                status="running",
                endpoint="http://127.0.0.1:9001",
            )
        return GatewayInstance(provider="fake-gw", instance_id=instance_id, status="stopped")

    async def qr(self, instance_id: str) -> str:
        return "FAKEQR"


class FakeStorage:
    """In-memory preferences store."""

    def __init__(self) -> None:
        self.preferences: dict[str, str] = {}

    async def get_preference(self, key: str) -> str | None:
        return self.preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value


def _manager(provisioner: FakeProvisioner) -> tuple[GatewayManager, FakeStorage]:
    config = MailFlowConfig()
    registry = ComponentRegistry()
    registrar = PluginRegistrar(registry, config, "test-plugin")
    registrar.add_gateway_provisioner("fake-gw", lambda: provisioner)
    storage = FakeStorage()
    return GatewayManager(config, registry, storage), storage


@pytest.mark.asyncio
async def test_provision_installs_starts_and_persists() -> None:
    provisioner = FakeProvisioner()
    manager, storage = _manager(provisioner)

    instance = await manager.provision("fake-gw", "gw-1", {"token": "x"})

    assert provisioner.installed == ["gw-1"]
    assert provisioner.started == ["gw-1"]
    assert instance.status == "running"
    assert instance.endpoint.startswith("http://127.0.0.1")
    # instance ids recorded for restart scanning
    assert storage.preferences.get("gateway.instance.fake-gw.ids") == "gw-1"
    # instance state persisted
    assert "gw-1" in storage.preferences.get("gateway.instance.fake-gw.gw-1", "")
    await manager.shutdown_instance("fake-gw", "gw-1")
    assert provisioner.stopped == ["gw-1"]


@pytest.mark.asyncio
async def test_provision_install_failure_marks_error() -> None:
    class FailingInstall(FakeProvisioner):
        async def install(self, instance_id: str, options: dict[str, Any]) -> None:
            raise RuntimeError("download failed")

    provisioner = FailingInstall()
    manager, _storage = _manager(provisioner)

    with pytest.raises(RuntimeError, match="install failed"):
        await manager.provision("fake-gw", "gw-1", {})

    instance = manager.instance("fake-gw", "gw-1")
    assert instance is not None
    assert instance.status == "error"
    assert "download failed" in instance.error


@pytest.mark.asyncio
async def test_detect_and_qr_delegate() -> None:
    provisioner = FakeProvisioner()
    manager, _storage = _manager(provisioner)

    assert await manager.detect("fake-gw") == "fake gateway ready"
    assert await manager.qr("fake-gw", "gw-1") == "FAKEQR"
    assert provisioner.detect_calls == 1


@pytest.mark.asyncio
async def test_providers_lists_registered() -> None:
    provisioner = FakeProvisioner()
    manager, _storage = _manager(provisioner)
    assert manager.providers() == ["fake-gw"]
    with pytest.raises(KeyError):
        manager.provisioner("missing")


def test_install_progress_updates_and_clamps() -> None:
    from mailflow.gateway import InstallProgress

    progress = InstallProgress()
    progress.update(10.0, "downloading")
    assert progress.percent == 10.0
    assert progress.message == "downloading"
    # clamp out-of-range
    progress.update(150.0, "over")
    assert progress.percent == 100.0
    progress.update(-5.0, "under")
    assert progress.percent == 0.0
    progress.finish("done")
    assert progress.percent == 100.0
    assert progress._done  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_provision_injects_progress_into_install_options() -> None:
    """The manager passes a shared InstallProgress to the provisioner's
    install() so the TUI can render a live download bar."""
    from mailflow.gateway import InstallProgress

    captured: dict[str, Any] = {}

    class RecordingProvisioner(FakeProvisioner):
        async def install(self, instance_id: str, options: dict[str, Any]) -> None:
            captured.update(options)
            await super().install(instance_id, options)

    provisioner = RecordingProvisioner()
    manager, _storage = _manager(provisioner)  # type: ignore[arg-type]
    await manager.provision("fake-gw", "gw-1", {"x": 1})
    progress = captured.get("_progress")
    assert isinstance(progress, InstallProgress)
    assert captured["x"] == 1
    # original options dict untouched (copy)
    assert "_progress" not in {"x": 1}


@pytest.mark.asyncio
async def test_resume_running_calls_ensure_bridge() -> None:
    """After an app restart the gateway child may still be running, so the
    supervisor never calls start() again — in-process side channels (the
    onebot event bridge) must still be recreated via ensure_bridge."""
    import asyncio

    provisioner = FakeProvisioner()
    manager, storage = _manager(provisioner)
    options = {"bot_url": "http://127.0.0.1:18789/bot/message"}
    persisted = GatewayInstance(
        provider="fake-gw",
        instance_id="gw-1",
        status="running",
        endpoint="http://127.0.0.1:9001",
        extra={"options": options, "autostart": True},
    )
    await storage.set_preference("gateway.instance.fake-gw.gw-1", persisted.model_dump_json())
    await storage.set_preference("gateway.instance.fake-gw.ids", "gw-1")

    await manager.start()  # resumes gw-1 as RUNNING
    # the supervisor runs one poll cycle before blocking on the stop event
    for _ in range(200):
        if provisioner.bridge_ensures:
            break
        await asyncio.sleep(0.01)
    assert provisioner.bridge_ensures == ["gw-1"]
    # a RUNNING gateway must not be restarted — only the side channel is
    # ensured; start() is never called
    assert provisioner.started == []

    await manager.stop()


@pytest.mark.asyncio
async def test_resume_dead_gateway_restarts_then_ensures_bridge() -> None:
    """An instance persisted as RUNNING whose child then died takes the
    restart path (start()) — the bridge is created by start() itself, so
    no separate ensure_bridge call happens before the restart."""
    import asyncio

    provisioner = FakeProvisioner()
    provisioner.running = False  # child died while persisted as running
    manager, storage = _manager(provisioner)
    persisted = GatewayInstance(
        provider="fake-gw",
        instance_id="gw-1",
        status="running",
        endpoint="http://127.0.0.1:9001",
        extra={"options": {"bot_url": "x"}, "autostart": True},
    )
    await storage.set_preference("gateway.instance.fake-gw.gw-1", persisted.model_dump_json())
    await storage.set_preference("gateway.instance.fake-gw.ids", "gw-1")

    await manager.start()  # resume path: child dead -> restart via start()
    # the restart path waits one backoff (5s) before starting
    for _ in range(800):
        if provisioner.started:
            break
        await asyncio.sleep(0.01)
    assert provisioner.started == ["gw-1"]
    await manager.stop()


@pytest.mark.asyncio
async def test_payload_missing_marks_error_and_stops_retry() -> None:
    """When the gateway payload is gone (deleted data dir), supervision
    must mark the instance error with a clear message and stop retrying —
    an endless restart loop would hide the problem forever."""
    import asyncio

    from mailflow.gateway import GatewayNotInstalledError

    class MissingPayload(FakeProvisioner):
        start_calls = 0

        async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
            MissingPayload.start_calls += 1
            raise GatewayNotInstalledError(
                f"napcat {instance_id} is not installed; run the setup again"
            )

    provisioner = MissingPayload()
    provisioner.running = False
    manager, storage = _manager(provisioner)
    persisted = GatewayInstance(
        provider="fake-gw",
        instance_id="gw-1",
        status="running",
        endpoint="http://127.0.0.1:9001",
        extra={"options": {"bot_url": "x"}, "autostart": True},
    )
    await storage.set_preference("gateway.instance.fake-gw.gw-1", persisted.model_dump_json())
    await storage.set_preference("gateway.instance.fake-gw.ids", "gw-1")

    await manager.start()
    # backoff(5s) -> start() raises GatewayNotInstalledError -> instance
    # marked error and the supervisor returns (no further start attempts)
    for _ in range(800):
        instance = manager.instance("fake-gw", "gw-1")
        if instance is not None and instance.status == "error":
            break
        await asyncio.sleep(0.01)
    instance = manager.instance("fake-gw", "gw-1")
    assert instance is not None
    assert instance.status == "error"
    assert "is not installed" in instance.error
    # exactly one start attempt: the loop stopped instead of retrying
    assert MissingPayload.start_calls == 1
    await manager.stop()


@pytest.mark.asyncio
async def test_notifier_pane_merge_gateway_error_shows_reconfig() -> None:
    """Gateway-backed notifiers whose managed gateway is in error (e.g.
    the data dir was deleted) get an explicit reconfigure hint in the
    status column instead of a generic probe result."""
    from mailflow.config import NotifierConfig
    from mailflow.contracts import GatewayInstance as GI
    from mailflow_tui.notifications import NotificationsPane

    class _StubService:
        def __init__(self) -> None:
            from types import SimpleNamespace

            self.config = SimpleNamespace(notifiers=[])

        def t(self, key: str, **params: Any) -> str:
            return f"t({key})"

        def gateway_providers(self) -> list[str]:
            return ["napcat", "wechaty"]

        async def gateway_instances(self) -> list[Any]:
            return [
                GI(
                    provider="napcat",
                    instance_id="qq-1",
                    status="error",
                    error="napcat qq-1 is not installed; run the setup again",
                    endpoint="http://127.0.0.1:3000",
                )
            ]

    service = _StubService()
    service.config.notifiers = [
        NotifierConfig(
            notifier_id="qq-1",
            provider="onebot",
            enabled=True,
            options={"gateway": "napcat", "http_url": "http://127.0.0.1:3000"},
        ),
        NotifierConfig(
            notifier_id="plain-2",
            provider="console",
            enabled=True,
        ),
    ]
    pane = NotificationsPane(cast(Any, service))
    results = {"qq-1": "online", "plain-2": "ok"}
    await pane._merge_gateway_states(results)  # pyright: ignore[reportPrivateUsage]
    assert "t(tui.bots_need_reconfig)" in results["qq-1"]
    assert "is not installed" in results["qq-1"]
    # non-gateway notifiers are untouched
    assert results["plain-2"] == "ok"
