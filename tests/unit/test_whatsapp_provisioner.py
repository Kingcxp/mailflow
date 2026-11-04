"""whatsapp gateway provisioner + notifier tests: install (npm), start, qr."""

from __future__ import annotations

import http.server
import importlib
import subprocess
import threading
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from mailflow.config import NotifierConfig
from mailflow_notify_whatsapp import gateway as gw_mod
from mailflow_notify_whatsapp.gateway import WhatsappProvisioner
from mailflow_notify_whatsapp.plugin import WhatsappNotifier


def _bridge_path(instance_id: str) -> Path:
    return gw_mod._instance_dir(instance_id) / "whatsapp-bridge.mjs"  # pyright: ignore[reportPrivateUsage]


def _port_for(instance_id: str) -> int:
    return gw_mod._port_for(instance_id)  # pyright: ignore[reportPrivateUsage]


def _deps(target: Path) -> None:
    (target / "node_modules" / "@whiskeysockets" / "baileys").mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _clean_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "gateways").mkdir(parents=True, exist_ok=True)


def _serve(port: int, body: bytes, status: int = 200) -> http.server.HTTPServer:
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self, *args: Any) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
            pass

    http.server.HTTPServer.allow_reuse_address = True
    server = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_port_is_re_derivable_from_the_instance_directory() -> None:
    """detect() probes a directory's real port, so the scheme must hash the
    same sanitized token the directory name carries."""
    instance_id = "My Bot (wa 2)"
    directory = _bridge_path(instance_id).parent.name
    from_directory = gw_mod._port_for(directory[len("whatsapp-") :])  # pyright: ignore[reportPrivateUsage]
    assert from_directory == _port_for(instance_id)
    assert _port_for(instance_id) != _port_for("wa-1")
    band = gw_mod._PORT_BAND  # pyright: ignore[reportPrivateUsage]
    base = gw_mod._BASE_PORT  # pyright: ignore[reportPrivateUsage]
    assert base <= _port_for(instance_id) < base + band


async def _never_ready(port: int, wait_seconds: float = 2.0) -> bool:
    """The port never answers: force the launch/early-exit paths."""
    return False


@pytest.mark.asyncio
async def test_install_requires_node_and_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw_mod, "_find_node", lambda: None)
    monkeypatch.setattr(gw_mod, "_find_npm", lambda: None)
    provisioner = WhatsappProvisioner()
    assert "node toolchain not found" in await provisioner.detect()
    assert (await provisioner.status("wa-1")).status == "stopped"
    with pytest.raises(RuntimeError, match="not installed"):
        await provisioner.start("wa-1", {})
    with pytest.raises(RuntimeError, match=r"nodejs\.org"):
        await WhatsappProvisioner().install("wa-1", {})
    monkeypatch.setattr(gw_mod, "_find_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(gw_mod, "_find_npm", lambda: None)
    with pytest.raises(RuntimeError, match="npm"):
        await WhatsappProvisioner().install("wa-1", {})


@pytest.mark.asyncio
async def test_install_runs_npm_and_writes_the_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gw_mod, "_find_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(gw_mod, "_find_npm", lambda: "/usr/bin/npm")
    calls: list[tuple[list[str], str | None]] = []

    async def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs.get("cwd")))
        _deps(Path(str(kwargs["cwd"])))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(gw_mod, "run_command", fake_run)
    await WhatsappProvisioner().install("wa-1", {})

    assert _bridge_path("wa-1").exists()
    assert calls == [
        (
            ["/usr/bin/npm", "install", "--no-audit", "--no-fund"],
            str(Path("data/gateways/whatsapp-wa-1")),
        )
    ]
    manifest = (_bridge_path("wa-1").parent / "package.json").read_text(encoding="utf-8")
    assert '"@whiskeysockets/baileys": "6.7.24"' in manifest
    bridge = _bridge_path("wa-1")
    bridge.write_text("old bridge", encoding="utf-8")
    credentials = bridge.parent / "auth" / "creds.json"
    credentials.parent.mkdir()
    credentials.write_text("saved login", encoding="utf-8")
    monkeypatch.setattr(gw_mod, "_find_npm", lambda: None)
    await WhatsappProvisioner().install("wa-1", {})
    assert len(calls) == 1
    assert bridge.read_bytes() == gw_mod._BRIDGE_SOURCE.read_bytes()  # pyright: ignore[reportPrivateUsage]
    assert credentials.read_text(encoding="utf-8") == "saved login"


@pytest.mark.asyncio
async def test_start_refuses_an_unmanaged_http_service(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _port_for("wa-1")
    bridge = _bridge_path("wa-1")
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_text("// bridge", encoding="utf-8")
    monkeypatch.setattr(gw_mod, "_find_node", lambda: "/usr/bin/node")

    server = _serve(port, b"{}")
    try:
        with pytest.raises(RuntimeError, match="unmanaged process"):
            await WhatsappProvisioner().start("wa-1", {"port": str(port)})
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_start_surfaces_early_exit_with_log_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = _bridge_path("wa-1")
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_text("// bridge", encoding="utf-8")
    monkeypatch.setattr(gw_mod, "_find_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(gw_mod, "_READY_TIMEOUT", 5.0)

    class FakeProc:
        returncode = 1

        def poll(self) -> int:
            return 1

        def terminate(self) -> None:  # pragma: no cover - never reached
            pass

        def kill(self) -> None:  # pragma: no cover - never reached
            pass

        def wait(self, timeout: float | None = None) -> int:  # pragma: no cover
            return 1

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProc:
        # the bridge is launched with the resolved script name and its cwd
        assert command == ["/usr/bin/node", "whatsapp-bridge.mjs"]
        assert Path(str(kwargs["cwd"])) == bridge.parent.resolve()
        with open(kwargs["stdout"].name, "ab") as handle:  # pyright: ignore[reportUnknownMemberType]
            handle.write(b"[whatsapp-bridge] boom: session start failed\n")
        return FakeProc()

    monkeypatch.setattr(gw_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        WhatsappProvisioner,
        "_wait_http_port",
        staticmethod(_never_ready),
    )

    with pytest.raises(RuntimeError, match="exited early") as excinfo:
        await WhatsappProvisioner().start("wa-1", {})
    assert "session start failed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_start_passes_bot_url_and_provider_to_the_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _bridge_path("wa-1")
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_text("// bridge", encoding="utf-8")
    monkeypatch.setattr(gw_mod, "_find_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(gw_mod, "_READY_TIMEOUT", 5.0)
    captured: dict[str, str] = {}

    class FakeProc:
        returncode = 0

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:  # pragma: no cover - never reached
            pass

        def wait(self, timeout: float | None = None) -> int:  # pragma: no cover
            return 0

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProc:
        captured.update(cast("dict[str, str]", kwargs["env"]))
        return FakeProc()

    monkeypatch.setattr(gw_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        WhatsappProvisioner,
        "_wait_http_port",
        staticmethod(_never_ready),
    )

    with pytest.raises(RuntimeError):
        await WhatsappProvisioner().start(
            "wa-1", {"bot_url": "http://127.0.0.1:18789/bot/message", "port": "8899"}
        )
    assert captured["MAILFLOW_BOT_URL"] == "http://127.0.0.1:18789/bot/message"
    assert captured["MAILFLOW_PROVIDER"] == "whatsapp"
    assert captured["MAILFLOW_INSTANCE"] == "wa-1"
    assert captured["GATEWAY_PORT"] == "8899"


@pytest.mark.asyncio
async def test_qr_maps_every_bridge_state(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _port_for("wa-1")
    payload = b'{"status": "scanning", "qrcode": "iVBORw0KGgo="}'
    server = _serve(port, payload)
    try:
        assert await WhatsappProvisioner().qr("wa-1") == "iVBORw0KGgo="
    finally:
        server.shutdown()
        server.server_close()

    server = _serve(port, b'{"status": "logged_in"}')
    try:
        assert await WhatsappProvisioner().qr("wa-1") == "__MAILFLOW_LOGGED_IN__"
    finally:
        server.shutdown()
        server.server_close()

    server = _serve(port, b'{"status": "error", "error": "no QR within 60s"}')
    try:
        result = await WhatsappProvisioner().qr("wa-1")
        assert result.startswith("ERROR:")
        assert "no QR within 60s" in result
    finally:
        server.shutdown()
        server.server_close()


class _NotifierResponse:
    def json(self) -> dict[str, Any]:
        return {"ok": True}

    status_code = 200

    def raise_for_status(self) -> None:
        pass


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


def _notifier(monkeypatch: pytest.MonkeyPatch, **options: Any) -> WhatsappNotifier:
    _NotifierClient.calls.clear()
    notifier_module = importlib.import_module("mailflow_notify_whatsapp.plugin")
    monkeypatch.setattr(cast(Any, notifier_module).httpx, "AsyncClient", _NotifierClient)
    return WhatsappNotifier(
        NotifierConfig(notifier_id="wa-1", provider="whatsapp", options=options)
    )


def _record() -> Any:
    from datetime import UTC, datetime

    from mailflow.domain import MailAddress, MailMessage, MailRecord, Urgency

    mail = MailMessage(
        message_id="<wa@e>",
        account_id="acct-1",
        sender=MailAddress(address="a@example.com"),
        recipients=[],
        subject="Exam on Friday",
        body_text="body",
        date=datetime.now(UTC),
        received_at=datetime.now(UTC),
    )
    return MailRecord(record_id="m1", mail=mail, auto_urgency=Urgency.URGENT)


@pytest.mark.asyncio
async def test_notify_posts_one_request_per_target(monkeypatch: pytest.MonkeyPatch) -> None:
    notifier = _notifier(
        monkeypatch,
        gateway_url="http://127.0.0.1:8898/",
        targets=["invalid", "group:", "group:12345@g.us", "user:67890"],
    )
    await notifier.notify(_record())

    calls = _NotifierClient.calls
    assert [url for url, _ in calls] == ["http://127.0.0.1:8898/send"] * 2
    assert [values["json"]["to"] for _, values in calls] == [
        {"type": "group", "name": "12345@g.us"},
        {"type": "contact", "name": "67890"},
    ]
    assert all(
        "Exam on Friday" in values["json"]["text"] and "m1" in values["json"]["text"]
        for _, values in calls
    )


@pytest.mark.asyncio
async def test_push_to_target_reports_missing_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifier = _notifier(monkeypatch, targets=["user:67890"])
    from mailflow.notifications import DeliveryError

    with pytest.raises(DeliveryError):
        await notifier.notify(_record())
    assert _NotifierClient.calls == []
    with pytest.raises(DeliveryError):
        await notifier.push_to_target("user:67890", "hourly briefing")
    assert _NotifierClient.calls == []


@pytest.mark.asyncio
async def test_push_text_and_push_to_target_use_the_send_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifier = _notifier(
        monkeypatch, gateway_url="http://127.0.0.1:8898", targets=["group:12345@g.us"]
    )
    await notifier.push_text("daily digest")
    await notifier.push_to_target("user:67890", "hourly briefing")

    assert _NotifierClient.calls == [
        (
            "http://127.0.0.1:8898/send",
            {
                "json": {
                    "to": {"type": "group", "name": "12345@g.us"},
                    "text": "daily digest",
                }
            },
        ),
        (
            "http://127.0.0.1:8898/send",
            {
                "json": {
                    "to": {"type": "contact", "name": "67890"},
                    "text": "hourly briefing",
                }
            },
        ),
    ]
