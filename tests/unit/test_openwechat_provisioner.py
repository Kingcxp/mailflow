"""openwechat gateway provisioner tests: install (Go build), start, qr."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from mailflow_notify_openwechat.gateway import OpenWechatProvisioner


def _bridge_path(instance_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in instance_id).strip("-")
    return Path("data") / "gateways" / f"openwechat-{safe}" / "openwechat-bridge"


def _port_for(instance_id: str) -> int:
    suffix = 0
    for ch in instance_id:
        if ch.isdigit():
            suffix = (suffix * 10 + int(ch)) % 97
    return 8888 + suffix


@pytest.fixture(autouse=True)
def _clean_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "gateways").mkdir(parents=True, exist_ok=True)


@pytest.mark.asyncio
async def test_detect_reports_go_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mailflow_notify_openwechat.gateway._find_go", lambda: None)
    prov = OpenWechatProvisioner()
    assert "go toolchain not found" in await prov.detect()


@pytest.mark.asyncio
async def test_install_requires_go(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mailflow_notify_openwechat.gateway._find_go", lambda: None)
    prov = OpenWechatProvisioner()
    with pytest.raises(RuntimeError, match="apt-get install -y golang-go"):
        await prov.install("wx-1", {})


@pytest.mark.asyncio
async def test_install_builds_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mailflow_notify_openwechat.gateway._find_go", lambda: "/usr/bin/go")

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        bridge = _bridge_path("wx-1")
        bridge.parent.mkdir(parents=True, exist_ok=True)
        bridge.write_bytes(b"#!fake")
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr("mailflow_notify_openwechat.gateway.subprocess.run", fake_run)
    prov = OpenWechatProvisioner()
    await prov.install("wx-1", {})
    assert _bridge_path("wx-1").exists()


@pytest.mark.asyncio
async def test_start_requires_install() -> None:
    prov = OpenWechatProvisioner()
    with pytest.raises(RuntimeError, match="not installed"):
        await prov.start("wx-1", {})


@pytest.mark.asyncio
async def test_start_reuses_running_port(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _port_for("wx-1")
    bridge = _bridge_path("wx-1")
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_bytes(b"#!fake")

    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self, *args: Any) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: Any) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
            pass

    http.server.HTTPServer.allow_reuse_address = True
    server = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        prov = OpenWechatProvisioner()
        instance = await prov.start("wx-1", {"port": str(port)})
        assert instance.status == "running"
        assert instance.extra and instance.extra.get("reused")
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_qr_surfaces_bridge_error(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _port_for("wx-1")

    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self, *args: Any) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "error", "error": "no QR within 60s"}')

        def log_message(self, *args: Any) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
            pass

    http.server.HTTPServer.allow_reuse_address = True
    server = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        prov = OpenWechatProvisioner()
        result = await prov.qr("wx-1")
        assert result.startswith("ERROR:")
        assert "no QR within 60s" in result
    finally:
        server.shutdown()
        server.server_close()
