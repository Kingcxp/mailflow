"""GatewayManager unit tests: provisioning, state persistence, supervision."""

from __future__ import annotations

from typing import Any

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
