"""Regression coverage for the WeChatPadPro guided deployment path."""

from __future__ import annotations

import asyncio
import importlib
import json
import platform
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from mailflow.config import NotifierConfig
from mailflow_notify_wechatpadpro import gateway
from mailflow_notify_wechatpadpro.plugin import WechatPadProNotifier
from mailflow_tui.notifications import NotificationsPane


def test_find_docker_compose_accepts_legacy_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host with classic ``docker-compose`` must not be probed as Docker."""
    legacy = "/usr/bin/docker-compose"
    calls: list[list[str]] = []

    def fake_which(command: str) -> str | None:
        return legacy if command == "docker-compose" else None

    monkeypatch.setattr(gateway, "_docker_exe", lambda: None)
    monkeypatch.setattr(shutil, "which", fake_which)

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command == [legacy, "version"]:
            return subprocess.CompletedProcess(command, 0, stdout="docker-compose 1.29", stderr="")
        raise AssertionError(f"legacy compose received an invalid command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert gateway._find_docker_compose() == legacy  # pyright: ignore[reportPrivateUsage]
    assert calls == [[legacy, "version"]]


def test_compose_arguments_support_plugin_and_legacy_clients() -> None:
    """The same generated project can run on Compose v2 and classic Compose."""
    compose_arguments = gateway._compose_arguments  # pyright: ignore[reportPrivateUsage]
    assert compose_arguments("/usr/bin/docker", "-f", "compose.yml", "pull") == [
        "/usr/bin/docker",
        "compose",
        "-f",
        "compose.yml",
        "pull",
    ]
    assert compose_arguments("/usr/bin/docker-compose", "-f", "compose.yml", "pull") == [
        "/usr/bin/docker-compose",
        "-f",
        "compose.yml",
        "pull",
    ]


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


class _RecordingWriter:
    def __init__(self) -> None:
        self.payload = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.payload.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_webhook_bridge_waits_for_entire_declared_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keep-alive sender may split the JSON body after its headers."""
    bridge = gateway._WebhookBridge("wechat-1", 0, "http://bot", "secret")  # pyright: ignore[reportPrivateUsage]
    forwarded: list[dict[str, Any]] = []

    async def capture(payload: dict[str, Any]) -> None:
        forwarded.append(payload)

    monkeypatch.setattr(bridge, "_forward", capture)
    body = b'{"Data":{"Content":"mailflow help","MsgType":1}}'
    headers = b"POST /webhook HTTP/1.1\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n"
    reader = asyncio.StreamReader()
    writer = _RecordingWriter()
    reader.feed_data(headers + body[:9])
    task = asyncio.create_task(cast(Any, bridge)._handle(reader, writer))
    await asyncio.sleep(0)
    assert not task.done()

    reader.feed_data(body[9:])
    reader.feed_eof()
    await task

    assert forwarded == [{"Data": {"Content": "mailflow help", "MsgType": 1}}]
    assert bytes(writer.payload).startswith(b"HTTP/1.1 200")
    assert writer.closed


class _NotifierResponse:
    status_code = 200

    def json(self) -> dict[str, int]:
        return {"Code": 0}


class _NotifierClient:
    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, **_: Any) -> None:
        pass

    async def __aenter__(self) -> _NotifierClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass

    async def post(self, url: str, **kwargs: Any) -> _NotifierResponse:
        self.calls.append((url, kwargs))
        return _NotifierResponse()


@pytest.mark.asyncio
async def test_auto_deployed_notifier_uses_managed_auth_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guided setup must produce a notifier that can immediately send."""
    monkeypatch.chdir(tmp_path)
    instance = tmp_path / "data/gateways/wechatpadpro-wechat-1"
    instance.mkdir(parents=True)
    (instance / "instance.json").write_text(
        json.dumps({"auth_api_key": "managed-auth-key"}), encoding="utf-8"
    )
    _NotifierClient.calls.clear()
    notifier_module = importlib.import_module("mailflow_notify_wechatpadpro.plugin")
    monkeypatch.setattr(cast(Any, notifier_module).httpx, "AsyncClient", _NotifierClient)
    notifier = WechatPadProNotifier(
        NotifierConfig(
            notifier_id="wechat-1",
            provider="wechatpadpro",
            options={
                "base_url": "http://127.0.0.1:8101",
                "gateway": "wechatpadpro",
                "targets": ["wxid-owner"],
            },
        )
    )

    await notifier.push_text("A new mail arrived")

    assert _NotifierClient.calls == [
        (
            "http://127.0.0.1:8101/Msg/SendTxt",
            {
                "params": {"key": "managed-auth-key"},
                "json": {
                    "Wxid": "",
                    "ToWxid": "wxid-owner",
                    "Content": "A new mail arrived",
                    "Type": 0,
                },
            },
        )
    ]


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
async def test_install_avoids_a_taken_port_and_wires_linux_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Automatic deployment must not collide and must reach its host bridge."""
    monkeypatch.chdir(tmp_path)
    compose = "/usr/bin/docker"
    provisioner = gateway.WechatPadProProvisioner()
    blocked_api_port = gateway._port_for("wechat-1", 8100)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(gateway, "_find_docker_compose", lambda: compose)

    def available(port: int) -> bool:
        return port != blocked_api_port

    monkeypatch.setattr(gateway, "_port_is_available", available)
    pulled: list[tuple[str, Path]] = []

    async def fake_pull(_: Any, command: str, compose_file: Path, progress: Any) -> None:
        pulled.append((command, compose_file))

    monkeypatch.setattr(gateway.WechatPadProProvisioner, "_pull_with_progress", fake_pull)

    await provisioner.install("wechat-1", {"bot_url": "http://127.0.0.1:18789/bot/message"})

    instance_dir = tmp_path / "data" / "gateways" / "wechatpadpro-wechat-1"
    metadata = json.loads((instance_dir / "instance.json").read_text(encoding="utf-8"))
    api_port = int(metadata["api_port"])
    assert api_port != blocked_api_port
    assert provisioner._endpoint("wechat-1") == f"http://127.0.0.1:{api_port}"  # pyright: ignore[reportPrivateUsage]
    assert pulled[0][0] == compose
    assert pulled[0][1].resolve() == (instance_dir / "compose.yml").resolve()
    compose_text = (instance_dir / "compose.yml").read_text(encoding="utf-8")
    assert "host.docker.internal:host-gateway" in compose_text
    assert f'"{api_port}:1238"' in compose_text


@pytest.mark.asyncio
async def test_install_starts_stopped_windows_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dormant installed Docker Desktop must not require a manual retry."""
    monkeypatch.chdir(tmp_path)
    docker = "C:/Program Files/Docker/Docker/resources/bin/docker.exe"
    availability = iter((None, docker))
    starts: list[bool] = []
    pulled: list[tuple[str, Path]] = []
    monkeypatch.setattr(gateway, "_find_docker_compose", lambda: next(availability))
    monkeypatch.setattr(gateway, "_docker_exe", lambda: docker)
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    def daemon_down(_: str) -> bool:
        return False

    def no_wait(_: str, *, seconds: int, progress: Any) -> bool:
        return False

    def start_desktop(_: Any) -> bool:
        starts.append(True)
        return True

    async def fake_pull(_: Any, command: str, compose_file: Path, progress: Any) -> None:
        pulled.append((command, compose_file))

    monkeypatch.setattr(gateway, "_docker_daemon_up", daemon_down)
    monkeypatch.setattr(gateway, "_wait_daemon", no_wait)
    monkeypatch.setattr(gateway, "_start_docker_desktop", start_desktop)
    monkeypatch.setattr(gateway.WechatPadProProvisioner, "_pull_with_progress", fake_pull)

    await gateway.WechatPadProProvisioner().install("wechat-1", {"bot_url": "http://bot"})

    assert starts == [True]
    assert pulled[0][0] == docker
    assert (
        pulled[0][1].resolve()
        == (tmp_path / "data/gateways/wechatpadpro-wechat-1/compose.yml").resolve()
    )


def test_windows_installer_starts_fresh_docker_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful winget install continues through daemon startup itself."""
    docker = "C:/Program Files/Docker/Docker/resources/bin/docker.exe"
    availability = iter((None, docker))
    starts: list[bool] = []
    monkeypatch.setattr(gateway, "_find_docker_compose", lambda: next(availability))
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    def winget(_: str) -> str:
        return "winget"

    def daemon_down(_: str) -> bool:
        return False

    monkeypatch.setattr(shutil, "which", winget)
    monkeypatch.setattr(gateway, "_docker_exe", lambda: docker)
    monkeypatch.setattr(gateway, "_docker_daemon_up", daemon_down)

    def fake_start(_: Any) -> bool:
        starts.append(True)
        return True

    monkeypatch.setattr(gateway, "_start_docker_desktop", fake_start)

    def successful_winget(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", successful_winget)

    assert gateway.install_docker_dependencies() == docker
    assert starts == [True]
