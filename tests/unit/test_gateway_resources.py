"""Gateway resource ownership with fake processes and HTTP responses."""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from mailflow import gateway_resources as resources


class Process:
    pid = 424242

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminated = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


async def test_cancelled_launch_reaps_process_created_after_cancellation() -> None:
    entered = threading.Event()
    release = threading.Event()
    process = Process()

    def launch() -> Any:
        entered.set()
        release.wait(timeout=2)
        return process

    task = asyncio.create_task(resources.launch_owned(launch))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(100):
        if process.returncode is not None:
            break
        await asyncio.sleep(0.01)
    assert process.terminated == 1


async def test_failed_setup_removes_new_directory_but_keeps_existing_credentials(
    tmp_path: Path,
) -> None:
    owner = resources.GatewayFiles()
    existing = tmp_path / "existing"
    existing.mkdir()
    credentials = existing / "session.json"
    credentials.write_text("credentials", encoding="utf-8")
    owner.prepare_directory("old", existing)
    owner.prepare_directory("new", tmp_path / "new")
    owner.prepare_directory("saved", tmp_path / "saved")
    owner.commit_instance("saved")
    await owner.cleanup_failed("old")
    await owner.cleanup_failed("new")
    await owner.cleanup_failed("new")
    await owner.cleanup_failed("saved")
    assert credentials.read_text(encoding="utf-8") == "credentials"
    assert not (tmp_path / "new").exists()
    assert (tmp_path / "saved").exists()


def test_ports_are_reserved_across_gateway_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = tmp_path / "wechatpadpro-one"
    other.mkdir()
    (other / "instance.json").write_text(json.dumps({"api_port": 30000}), encoding="utf-8")
    monkeypatch.setattr(resources, "port_available", lambda _: True)
    first = tmp_path / "onebot-one"
    second = tmp_path / "openwechat-two"
    assert resources.allocate_port(first, "http_port", 30000) == 30001
    assert resources.allocate_port(second, "http_port", 30000) == 30002
    assert resources.allocate_port(first, "http_port", 30000) == 30001
    with pytest.raises(RuntimeError, match="in use"):
        resources.allocate_port(second, "http_port", 30000, explicit=True)
    assert resources.instance_token("Account") != resources.instance_token("account")
    assert resources.instance_token("a/b") != resources.instance_token("a?b")


def test_legacy_credentials_are_kept_and_claimed_by_only_one_instance(tmp_path: Path) -> None:
    legacy = tmp_path / "whatsapp-a-b"
    legacy.mkdir()
    (legacy / "session.json").write_text("old session", encoding="utf-8")
    assert resources.instance_directory(tmp_path, "whatsapp", "a/b") == legacy
    assert resources.instance_directory(tmp_path, "whatsapp", "a?b") != legacy
    assert (legacy / "session.json").read_text(encoding="utf-8") == "old session"


@pytest.mark.parametrize("provider", ["openwechat", "whatsapp"])
async def test_local_gateway_custom_port_restart_and_instance_isolation(
    provider: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module(f"mailflow_notify_{provider}.gateway")
    monkeypatch.setattr(module, "_data_root", lambda: tmp_path)
    if provider == "whatsapp":
        monkeypatch.setattr(module, "_find_node", lambda: "fake-node")
    provisioner = (
        module.OpenWechatProvisioner if provider == "openwechat" else module.WhatsappProvisioner
    )()
    calls: list[dict[str, Any]] = []
    processes: list[Process] = []
    ready: set[int] = set()

    def launch(command: list[str], **kwargs: Any) -> Any:
        calls.append(kwargs)
        ready.add(int(kwargs["env"]["GATEWAY_PORT"]))
        process = Process()
        processes.append(process)
        return process

    async def health(port: int, wait_seconds: float = 2) -> bool:
        return port in ready

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(resources, "port_available", lambda _: True)
    monkeypatch.setattr(provisioner, "_wait_http_port", health)
    bridge_name = "openwechat-bridge" if provider == "openwechat" else "whatsapp-bridge.mjs"
    for instance_id in ("a/b", "a?b"):
        directory = module._instance_dir(instance_id)
        directory.mkdir()
        (directory / bridge_name).write_bytes(b"fake")

    first = await provisioner.start("a/b", {"port": 32001, "bot_url": "http://old"})
    repeated = await provisioner.start("a/b", {"port": 32001, "bot_url": "http://old"})
    second = await provisioner.start("a?b", {"port": 32002})
    assert repeated.endpoint == first.endpoint == "http://127.0.0.1:32001"
    assert (await provisioner.status("a/b")).endpoint == first.endpoint
    assert second.endpoint == "http://127.0.0.1:32002"
    assert len(processes) == 2
    assert calls[0]["cwd"] != calls[1]["cwd"]
    assert calls[0]["env"]["MAILFLOW_BOT_URL"] == "http://old"
    ready.remove(32001)
    await provisioner.start("a/b", {"port": 32001, "bot_url": "http://new"})
    assert processes[0].terminated == 1
    assert calls[2]["env"]["MAILFLOW_BOT_URL"] == "http://new"
    await provisioner.stop("a/b")
    await provisioner.stop("a/b")
    assert (await provisioner.status("a?b")).status == "running"
    await provisioner.stop("a?b")
    assert [process.terminated for process in processes] == [1, 1, 1]


async def test_early_exit_cleans_process_reference_and_reports_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Gateway(resources.LocalProcessGateway):
        provider = "fake"

    owner = Gateway(lambda _: tmp_path, lambda _: 32003)
    process = Process(4)

    async def unhealthy(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(resources, "port_available", lambda _: True)
    monkeypatch.setattr(owner, "_wait_http_port", unhealthy)
    (tmp_path / "fake.log").write_text("failed to load runtime", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"exited early.*code 4"):
        await owner.start_process("one", {}, ["fake"], timeout_seconds=1)
    assert owner._processes == {}
    assert (await owner.status("one")).status == "stopped"


async def test_listener_stop_closes_accepted_connections_and_can_restart() -> None:
    from mailflow.lifecycle import OwnedListener

    listener = OwnedListener()
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    await listener.start_listener(handle, "127.0.0.1", 0)
    server = listener._server
    assert server is not None
    await listener.start_listener(handle, "127.0.0.1", 0)
    assert listener._server is server
    reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname()[:2])
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await listener.stop_listener()
        await listener.stop_listener()
        assert finished.is_set()
        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        await listener.start_listener(handle, "127.0.0.1", 0)
        assert listener.running
    finally:
        writer.close()
        await writer.wait_closed()
        await listener.stop_listener()
