"""WeChat gateway provisioner based on the openwechat Go SDK.

Scan-to-login with **no platform token**: the openwechat SDK talks the
web protocol directly, renders the login QR to a PNG and serves it at
``GET /qr``; the TUI shows it inline. Session hot-reload keeps the login
across restarts (``openwechat-session.json`` in the instance dir).

Requires a Go toolchain (>= 1.21) to build the bridge; MailFlow never
installs system packages itself — when Go is missing the provisioner
reports the exact install command (``apt-get install golang-go``).

This is a reference bridge: any service implementing the same three
endpoints (``/health``, ``/qr``, ``/send``) works with MailFlow.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
from mailflow.contracts import GatewayInstance, GatewayProvisioner

logger = logging.getLogger("mailflow.gateway.openwechat")

_QR_LOGGED_IN = "__MAILFLOW_LOGGED_IN__"
_BASE_PORT = 8888
_READY_TIMEOUT = 30.0
_BRIDGE_GO = Path(__file__).parent / "gateway" / "openwechat-bridge.go"


def _data_root() -> Path:
    return Path("data") / "gateways"


def _instance_dir(instance_id: str) -> Path:
    """Filesystem-safe instance directory (spaces/parens -> dashes)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in str(instance_id)).strip("-")
    return _data_root() / f"openwechat-{safe}"


def _find_go() -> str | None:
    found = shutil.which("go")
    return found


class OpenWechatProvisioner(GatewayProvisioner):
    provider = "openwechat"

    def _endpoint(self, instance_id: str) -> str:
        return f"http://127.0.0.1:{self._port_for(instance_id)}"

    @staticmethod
    def _port_for(instance_id: str) -> int:
        suffix = 0
        for ch in str(instance_id):
            if ch.isdigit():
                suffix = (suffix * 10 + int(ch)) % 97
        return _BASE_PORT + suffix

    async def detect(self) -> str:
        go = _find_go()
        if go is None:
            return "go toolchain not found (apt-get install golang-go)"
        installed = any(
            d.is_dir() and (d / "openwechat-bridge").exists()
            for d in _data_root().glob("openwechat-*")
        )
        running = await self._any_running()
        parts = [f"go {go}"]
        parts.append("installed" if installed else "not installed")
        parts.append("running" if running else "not running")
        return "; ".join(parts)

    async def _any_running(self) -> bool:
        for directory in _data_root().glob("openwechat-*"):
            if not directory.is_dir():
                continue
            token = directory.name[len("openwechat-") :]
            try:
                suffix = int(token.split("-")[-1]) % 97 if token.split("-")[-1].isdigit() else 0
            except (ValueError, IndexError):
                continue
            port = _BASE_PORT + suffix
            if await self._wait_http_port(port, wait_seconds=2.0):
                return True
        return False

    @staticmethod
    async def _wait_http_port(port: int, wait_seconds: float = 2.0) -> bool:
        url = f"http://127.0.0.1:{port}"
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(url)
                if response.status_code < 500:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.3)
        return False

    async def install(self, instance_id: str, options: dict[str, Any]) -> None:
        go = _find_go()
        if go is None:
            raise RuntimeError(
                "openwechat needs the Go toolchain to build its bridge — "
                "install it first:\n  apt-get install -y golang-go\n"
                "(or set PATH so `go` resolves; MailFlow never installs "
                "system packages itself)"
            )
        target = _instance_dir(instance_id)
        target.mkdir(parents=True, exist_ok=True)
        bridge = target / "openwechat-bridge"
        if bridge.exists():
            logger.info("openwechat %s: bridge already built", instance_id)
            return
        logger.info("openwechat %s: building bridge with %s", instance_id, go)
        result = await asyncio.to_thread(
            subprocess.run,
            [go, "build", "-o", str(bridge), str(_BRIDGE_GO)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"openwechat {instance_id}: bridge build failed: {result.stderr.strip()[:400]}"
            )
        await asyncio.to_thread(os.chmod, bridge, 0o755)
        logger.info("openwechat %s: bridge ready", instance_id)

    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
        target = _instance_dir(instance_id)
        bridge = target / "openwechat-bridge"
        if not bridge.exists():
            raise RuntimeError(
                f"openwechat {instance_id} is not installed (no bridge "
                f"binary); run the setup to install it"
            )
        port = int(options.get("port") or self._port_for(instance_id))
        if await self._wait_http_port(port, wait_seconds=1.0):
            logger.info("openwechat %s: reusing running gateway on :%d", instance_id, port)
            return GatewayInstance(
                provider="openwechat",
                instance_id=instance_id,
                status="running",
                endpoint=f"http://127.0.0.1:{port}",
                extra={"port": port, "reused": True},
            )
        log_file = target / "openwechat.log"

        def _launch() -> subprocess.Popen[Any]:
            env = {**os.environ, **dict(options.get("env") or {})}
            env["GATEWAY_PORT"] = str(port)
            with open(log_file, "ab") as handle:
                return subprocess.Popen(
                    [str(bridge)],
                    cwd=str(target),
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )

        try:
            process = await asyncio.to_thread(_launch)
        except OSError as exc:
            raise RuntimeError(f"failed to launch openwechat {instance_id}: {exc}") from exc
        self._processes = getattr(self, "_processes", {})
        self._processes[instance_id] = process
        endpoint = self._endpoint(instance_id)
        deadline = asyncio.get_running_loop().time() + _READY_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if await self._wait_http_port(port, wait_seconds=2.0):
                return GatewayInstance(
                    provider="openwechat",
                    instance_id=instance_id,
                    status="running",
                    endpoint=endpoint,
                    extra={"port": port},
                )
            proc: subprocess.Popen[Any] | None = self._processes.get(instance_id)
            if proc is not None and proc.poll() is not None:
                self._processes.pop(instance_id, None)
                tail = self._tail_log(log_file)
                raise RuntimeError(
                    f"openwechat {instance_id} exited early (code "
                    f"{proc.returncode}); see {log_file}{tail}"
                )
            await asyncio.sleep(2.0)
        self._terminate(instance_id)
        tail = self._tail_log(log_file)
        raise RuntimeError(
            f"openwechat {instance_id} did not answer on {endpoint} in "
            f"{_READY_TIMEOUT:.0f}s; see {log_file}{tail}"
        )

    def _terminate(self, instance_id: str) -> None:
        processes = getattr(self, "_processes", {})
        process = processes.pop(instance_id, None)
        if process is None:
            return
        if process.poll() is None:
            with __import__("contextlib").suppress(Exception):
                process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                with __import__("contextlib").suppress(Exception):
                    process.kill()

    async def stop(self, instance_id: str) -> None:
        await asyncio.to_thread(self._terminate, instance_id)

    async def status(self, instance_id: str) -> GatewayInstance:
        endpoint = self._endpoint(instance_id)
        running = await self._wait_http_port(self._port_for(instance_id), wait_seconds=2.0)
        if running:
            return GatewayInstance(
                provider="openwechat",
                instance_id=instance_id,
                status="running",
                endpoint=endpoint,
            )
        return GatewayInstance(
            provider="openwechat",
            instance_id=instance_id,
            status="stopped",
            error="bridge not answering",
        )

    async def qr(self, instance_id: str) -> str:
        """Login state: QR png base64, logged-in sentinel, or ERROR: …"""
        endpoint = self._endpoint(instance_id)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(f"{endpoint}/qr")
                response.raise_for_status()
                payload: Any = response.json()
                if payload.get("status") == "logged_in":
                    return _QR_LOGGED_IN
                if payload.get("status") == "error":
                    return f"ERROR: {payload.get('error') or 'bridge failed'}"
                return str(payload.get("qrcode") or "")
        except Exception as exc:
            logger.warning("openwechat %s /qr failed: %s", instance_id, exc)
            return ""

    @staticmethod
    def _tail_log(log_file: Path, lines: int = 10) -> str:
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = content[-lines:]
            return "\n  log: " + "\n  log: ".join(tail) if tail else ""
        except OSError:
            return ""
