"""GatewayManager unit tests: provisioning, state persistence, supervision."""

from __future__ import annotations

import asyncio
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


def _manager(
    provisioner: FakeProvisioner, config: MailFlowConfig | None = None
) -> tuple[GatewayManager, FakeStorage]:
    config = config or MailFlowConfig()
    registry = ComponentRegistry()
    registrar = PluginRegistrar(registry, config, "test-plugin")
    registrar.add_gateway_provisioner("fake-gw", lambda: provisioner)
    storage = FakeStorage()
    return GatewayManager(config, registry, storage), storage


async def test_stop_during_install_never_starts_gateway() -> None:
    installing = asyncio.Event()

    class SlowInstall(FakeProvisioner):
        async def install(self, instance_id: str, options: dict[str, Any]) -> None:
            installing.set()
            await asyncio.Event().wait()

    provisioner = SlowInstall()
    manager, _ = _manager(provisioner)
    operation = asyncio.create_task(manager.provision("fake-gw", "one", {}))
    await installing.wait()
    await manager.stop()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await manager.stop()
    assert provisioner.started == []
    assert provisioner.stopped == ["one"]


async def test_shutdown_also_cancels_setup_waiting_for_the_same_instance() -> None:
    entered = asyncio.Event()

    class Installing(FakeProvisioner):
        async def install(self, instance_id: str, options: dict[str, Any]) -> None:
            self.installed.append(instance_id)
            entered.set()
            await asyncio.Event().wait()

    provisioner = Installing()
    manager, _ = _manager(provisioner)
    first = asyncio.create_task(manager.provision("fake-gw", "one", {}))
    await entered.wait()
    second = asyncio.create_task(manager.provision("fake-gw", "one", {}))
    await asyncio.sleep(0)
    await manager.stop()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(outcomes[0], asyncio.CancelledError)
    assert isinstance(outcomes[1], RuntimeError)
    assert provisioner.installed == ["one"] and not provisioner.started


async def test_slow_stop_is_bounded_and_retry_joins_the_same_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    class SlowStop(FakeProvisioner):
        async def stop(self, instance_id: str) -> None:
            self.stopped.append(instance_id)
            await release.wait()

    monkeypatch.setattr("mailflow.gateway._STOP_TIMEOUT_SECONDS", 0.02)
    provisioner = SlowStop()
    manager, _ = _manager(provisioner)
    await manager.provision("fake-gw", "one", {})
    with pytest.raises(RuntimeError, match="stop failed"):
        await manager.stop()
    with pytest.raises(RuntimeError, match="shutdown is still in progress"):
        await manager.start()
    release.set()
    await manager.stop()
    await manager.stop()
    assert provisioner.stopped == ["one"]
    instance = manager.instance("fake-gw", "one")
    assert instance is not None and instance.status == "stopped"


async def test_login_timeout_cleans_failed_setup_and_preserves_error() -> None:
    provisioner = FakeProvisioner()
    manager, _ = _manager(provisioner)
    await manager.provision("fake-gw", "one", {"login_timeout_seconds": 0})
    supervisors = [task for task in asyncio.all_tasks() if task.get_name() == "gateway-one"]
    await asyncio.wait_for(asyncio.gather(*supervisors), timeout=1)
    instance = manager.instance("fake-gw", "one")
    assert instance is not None and instance.status == "error"
    assert "login timed out" in instance.error
    await manager.stop()
    assert provisioner.stopped == ["one"]


async def test_progress_and_supervisors_are_isolated_by_instance() -> None:
    provisioner = FakeProvisioner()
    manager, _ = _manager(provisioner)
    await asyncio.gather(
        manager.provision("fake-gw", "one", {}),
        manager.provision("fake-gw", "two", {}),
    )
    assert manager.progress("fake-gw", "one") is not manager.progress("fake-gw", "two")
    supervisors = {task for task in asyncio.all_tasks() if task.get_name().startswith("gateway-")}
    await manager.start()
    await manager.start()
    assert {
        task for task in asyncio.all_tasks() if task.get_name().startswith("gateway-")
    } == supervisors
    await manager.stop()
    await manager.stop()
    assert sorted(provisioner.stopped) == ["one", "two"]


async def test_service_restart_does_not_resume_removed_or_disabled_notifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from mailflow.config import NotifierConfig

    provisioner = FakeProvisioner()
    config = MailFlowConfig()
    manager, _storage = _manager(provisioner, config)
    for name in ("removed", "disabled"):
        await manager.provision("fake-gw", name, {})
    await manager.stop(persist_running=True)
    config.notifiers = [
        NotifierConfig(
            notifier_id="disabled",
            provider="fake-notify",
            enabled=False,
            options={"gateway": "fake-gw"},
        )
    ]
    monkeypatch.setattr(manager, "_service_ref", SimpleNamespace(bot_server=None))
    await manager.start()
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("gateway-")]
    assert all(instance.status == "stopped" for instance in await manager.instances())
    await manager.stop()
    assert sorted(provisioner.stopped) == ["disabled", "removed"]


async def test_plugin_reload_replaces_supervisor_and_stops_old_gateway() -> None:
    provisioner = FakeProvisioner()
    manager, storage = _manager(provisioner)
    await manager.provision("fake-gw", "gw-1", {})
    tasks = dict(manager._supervise_tasks)  # pyright: ignore[reportPrivateUsage]
    config = MailFlowConfig()
    registry = ComponentRegistry()
    PluginRegistrar(registry, config, "test-plugin").add_gateway_provisioner(
        "fake-gw", lambda: provisioner
    )
    await manager.reconfigure(config, registry, running=True)
    assert manager._supervise_tasks != tasks  # pyright: ignore[reportPrivateUsage]
    assert all(task.done() for task in tasks.values())
    assert provisioner.stopped == ["gw-1"]
    await manager.reconfigure(config, ComponentRegistry(), running=True)
    assert provisioner.stopped == ["gw-1", "gw-1"]
    assert all(task.done() for task in tasks.values())
    assert await manager.instances() == []
    assert '"running"' in storage.preferences["gateway.instance.fake-gw.gw-1"]
    await manager.reconfigure(config, registry, running=True)
    assert manager.instance("fake-gw", "gw-1") is not None
    await manager.stop()


@pytest.mark.asyncio
async def test_provision_installs_starts_and_persists() -> None:
    provisioner = FakeProvisioner()
    manager, storage = _manager(provisioner)
    assert await manager.detect("fake-gw") == "fake gateway ready"

    instance = await manager.provision("fake-gw", "gw-1", {"token": "x"})

    assert provisioner.installed == ["gw-1"]
    assert provisioner.started == ["gw-1"]
    assert instance.status == "running"
    assert instance.endpoint.startswith("http://127.0.0.1")
    assert await manager.qr("fake-gw", "gw-1") == "FAKEQR"
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
    assert provisioner.stopped == ["gw-1"]


@pytest.mark.asyncio
async def test_provision_start_failure_stops_partial_gateway() -> None:
    """A failed readiness check must not leave a launched child unmanaged."""

    class FailingStart(FakeProvisioner):
        async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
            self.started.append(instance_id)
            raise RuntimeError("gateway did not become ready")

    provisioner = FailingStart()
    manager, _storage = _manager(provisioner)

    with pytest.raises(RuntimeError, match="start failed"):
        await manager.provision("fake-gw", "gw-1", {})

    instance = manager.instance("fake-gw", "gw-1")
    assert instance is not None
    assert instance.status == "error"
    assert provisioner.stopped == ["gw-1"]


@pytest.mark.asyncio
async def test_reprovision_replaces_supervisor_and_persisted_options() -> None:
    """Re-login cannot leave a stale poller or a stale bridge endpoint."""
    provisioner = FakeProvisioner()
    manager, _storage = _manager(provisioner)

    await manager.provision("fake-gw", "gw-1", {"bot_url": "http://old"})
    first = manager._supervise_tasks["fake-gw.gw-1"]  # pyright: ignore[reportPrivateUsage]

    await manager.provision("fake-gw", "gw-1", {"bot_url": "http://new"}, autostart=False)

    second = manager._supervise_tasks["fake-gw.gw-1"]  # pyright: ignore[reportPrivateUsage]
    instance = manager.instance("fake-gw", "gw-1")
    assert instance is not None
    assert first is not second
    assert first.done()
    assert len(manager._supervise_tasks) == 1  # pyright: ignore[reportPrivateUsage]
    assert instance.extra["options"] == {"bot_url": "http://new"}
    assert instance.extra["autostart"] is False
    await manager.stop()


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
            return ["napcat"]

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
    assert results["qq-1"] == "t(chat.connection_error) (t(errors.operation))"
    assert "is not installed" not in results["qq-1"]
    # non-gateway notifiers are untouched
    assert results["plain-2"] == "ok"


@pytest.mark.asyncio
async def test_app_shutdown_persists_running_for_next_boot() -> None:
    """The TUI-exit path (persist_running=True) kills the children but
    keeps the persisted RUNNING status: the resume filter (running|error)
    must find the instance on the next boot and restart it. The old
    behavior wrote 'stopped' here, which read as a USER stop — after a
    normal TUI exit the gateways never came back (no logs, no retries)."""
    provisioner = FakeProvisioner()
    manager, storage = _manager(provisioner)
    await manager.provision("fake-gw", "gw-1", {"token": "x"})
    assert "running" in storage.preferences["gateway.instance.fake-gw.gw-1"]

    await manager.stop(persist_running=True)

    assert provisioner.stopped == ["gw-1"]  # the child WAS killed
    persisted = storage.preferences["gateway.instance.fake-gw.gw-1"]
    assert '"running"' in persisted, persisted  # and still marked running


@pytest.mark.asyncio
async def test_explicit_shutdown_marks_stopped() -> None:
    """A user-stop via the Bots tab keeps the old semantics: no autostart
    on the next boot."""
    provisioner = FakeProvisioner()
    manager, storage = _manager(provisioner)
    await manager.provision("fake-gw", "gw-1", {})

    await manager.shutdown_instance("fake-gw", "gw-1")

    persisted = storage.preferences["gateway.instance.fake-gw.gw-1"]
    assert '"stopped"' in persisted, persisted


@pytest.mark.asyncio
async def test_shutdown_failure_stays_visible() -> None:
    """A failed stop must not lie that a managed gateway is stopped."""

    class FailingStop(FakeProvisioner):
        async def stop(self, instance_id: str) -> None:
            self.stopped.append(instance_id)
            raise RuntimeError("docker refused stop")

    provisioner = FailingStop()
    manager, storage = _manager(provisioner)
    await manager.provision("fake-gw", "gw-1", {})

    with pytest.raises(RuntimeError, match="docker refused stop"):
        await manager.shutdown_instance("fake-gw", "gw-1")

    instance = manager.instance("fake-gw", "gw-1")
    assert instance is not None
    assert instance.status == "error"
    assert "stop failed" in storage.preferences["gateway.instance.fake-gw.gw-1"]


@pytest.mark.asyncio
async def test_memory_exhausted_gateway_is_recycled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway whose provisioner flags extra.memory_exhausted gets
    stopped and restarted immediately (no backoff sleep) — the graceful
    recycle that keeps a leaking QQ tree from eating the host."""
    import mailflow.gateway as gw_mod

    monkeypatch.setattr(gw_mod, "_host_free_mb", lambda: 8000.0)  # host healthy

    class Leaky(FakeProvisioner):
        async def status(self, instance_id: str) -> GatewayInstance:
            # after the recycle-stop, the fresh process reports starting
            # (fresh RSS below the limit) — an always-exhausted fake would
            # recycle forever without ever reaching start()
            if provisioner.stopped:
                return GatewayInstance(
                    provider="fake-gw",
                    instance_id=instance_id,
                    status="starting",
                    endpoint="http://127.0.0.1:9001",
                )
            return GatewayInstance(
                provider="fake-gw",
                instance_id=instance_id,
                status="running",
                endpoint="http://127.0.0.1:9001",
                extra={"memory_exhausted": True},
            )

    provisioner = Leaky()
    manager, _storage = _manager(provisioner)
    await manager.provision("fake-gw", "gw-1", {})
    await manager.stop()  # cancel the first supervisor

    # fresh supervision picks up the flag and recycles within the poll
    provisioner.started.clear()
    provisioner.stopped.clear()  # count only the recycle cycle from here
    manager._stop_event.clear()  # pyright: ignore[reportPrivateUsage]
    instance = manager.instance("fake-gw", "gw-1")
    assert instance is not None
    task = asyncio.create_task(manager._supervise(instance, resume=False))  # pyright: ignore[reportPrivateUsage]
    for _ in range(100):
        if provisioner.stopped:
            break
        await asyncio.sleep(0.02)
    for _ in range(100):
        if provisioner.started:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert provisioner.stopped.count("gw-1") >= 1, "leaky gateway was not recycled"
    assert provisioner.started == ["gw-1"], "recycle did not restart the gateway"


async def test_notification_settings_keep_connections_and_only_reconnect_changed_channel() -> None:
    from mailflow.config import NotifierConfig

    config = MailFlowConfig(
        notifiers=[
            NotifierConfig(
                notifier_id=name,
                provider="onebot",
                options={"gateway": "fake-gw", "http_url": "http://old"},
            )
            for name in ("one", "two")
        ]
    )
    provisioner = FakeProvisioner()
    manager, _ = _manager(provisioner, config)
    await manager.provision("fake-gw", "one", {})
    await manager.provision("fake-gw", "two", {})
    try:
        changed = config.model_copy(deep=True)
        changed.notifiers[0].options["hourly_summary"] = True
        changed.notifiers[0].minimum_urgency = "important"
        await manager.update_notifiers(config, changed)
        assert provisioner.stopped == []
        assert (await manager.instances())[0].status == "running"
        reconnected = changed.model_copy(deep=True)
        reconnected.notifiers[0].options["http_url"] = "http://new"
        await manager.update_notifiers(changed, reconnected)
        assert provisioner.stopped == ["one"]
        assert manager.instance("fake-gw", "two").status == "running"
    finally:
        await manager.stop()
