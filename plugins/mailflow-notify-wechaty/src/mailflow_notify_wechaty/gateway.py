"""WeChaty gateway provisioner: auto-install and supervise a WeChaty
pad-protocol gateway for the Bots tab.

Install model:
- Requires Node.js and npm. We create ``<data>/gateways/wechaty-<instance>/``,
  ``npm install wechaty wechaty-puppet-padlocal`` there (pinned versions),
  copy the bundled ``wechaty-gateway.js`` bridge, and run it with ``node``.
- The pad-protocol token comes from the user (a paid service such as
  padlocal); it is passed via the process environment, never on the
  command line.
- Each instance gets its own directory and port (base 8788 + instance
  offset); the QR is served by the bridge at ``GET /qr`` and rendered in
  the TUI.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
from mailflow.contracts import GatewayInstance

logger = logging.getLogger("mailflow.gateway.wechaty")

_WECHATY_VERSION = "wechaty@1.20.2"
_PUPPET_VERSION = "wechaty-puppet-padlocal@1.20.1"
# free web-protocol puppet: scan-to-login, no platform token (ban risk —
# use a disposable account). Installed alongside padlocal so both modes
# work without a reinstall.
_WECHAT4U_VERSION = "wechaty-puppet-wechat4u@1.14.14"
_BASE_PORT = 8788
_QR_LOGGED_IN = "__MAILFLOW_LOGGED_IN__"
_READY_TIMEOUT = 45.0
_BRIDGE = Path(__file__).parent / "gateway" / "wechaty-gateway.js"


def _data_root() -> Path:
    return Path("data") / "gateways"


def _safe_token(instance_id: str) -> str:
    """Instance id -> filesystem/port-safe token (spaces, parentheses and
    other characters cannot appear in directory names or integer parsing);
    a short hash keeps distinct ids distinct after sanitizing."""
    import hashlib
    import re

    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "-", instance_id).strip("-") or "gw"
    digest = hashlib.sha1(instance_id.encode("utf-8")).hexdigest()[:6]
    return f"{cleaned}-{digest}"


def _instance_dir(instance_id: str) -> Path:
    return _data_root() / f"wechaty-{_safe_token(instance_id)}"


def _find_node() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    try:
        result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    version = (result.stdout or "").strip().lstrip("v")
    try:
        major = int(version.split(".")[0])
    except ValueError:
        return None
    return node if major >= 18 else None


class WechatyGatewayProvisioner:
    """WeChaty pad-protocol gateway managed as a child process."""

    backend_id = "wechaty"

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen[Any]] = {}

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _port_for(instance_id: str) -> int:
        try:
            suffix = int(_safe_token(instance_id).split("-")[-2])
        except (ValueError, IndexError):
            suffix = 0
        return _BASE_PORT + (suffix % 100)

    def _endpoint(self, instance_id: str) -> str:
        return f"http://127.0.0.1:{self._port_for(instance_id)}"

    async def _wait_http(self, instance_id: str, wait_seconds: float = _READY_TIMEOUT) -> bool:
        url = self._endpoint(instance_id)
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    response = await client.get(f"{url}/health")
                if response.status_code < 500:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.0)
        return False

    # -- GatewayProvisioner ----------------------------------------------------

    async def detect(self) -> str:
        node = _find_node()
        if node is None:
            return "node not found (WeChaty needs Node.js >= 18)"
        installed = any(p.is_dir() for p in _data_root().glob("wechaty-*"))
        running = await self._any_running()
        parts = [f"node {node}"]
        parts.append("installed" if installed else "not installed")
        parts.append("running" if running else "not running")
        return "; ".join(parts)

    async def _any_running(self) -> bool:
        for directory in _data_root().glob("wechaty-*"):
            if not directory.is_dir():
                continue
            token = directory.name[len("wechaty-") :]
            try:
                suffix = int(token.split("-")[-2])
            except (ValueError, IndexError):
                continue
            port = _BASE_PORT + (suffix % 100)
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
                    response = await client.get(f"{url}/health")
                if response.status_code < 500:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.3)
        return False

    async def install(self, instance_id: str, options: dict[str, Any]) -> None:
        node = _find_node()
        if node is None:
            raise RuntimeError("WeChaty needs Node.js >= 18; install Node first")
        target = _instance_dir(instance_id)
        if (target / "node_modules" / "wechaty").exists() and (
            target / "wechaty-gateway.js"
        ).exists():
            logger.info("wechaty %s already installed at %s", instance_id, target)
            return
        target.mkdir(parents=True, exist_ok=True)
        (target / "package.json").write_text(
            '{"name": "mailflow-wechaty-gateway", "private": true, "version": "0.1.0"}',
            encoding="utf-8",
        )
        # copy the bundled bridge next to node_modules
        shutil.copyfile(_BRIDGE, target / "wechaty-gateway.js")
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError("npm not found; install Node.js (includes npm)")
        command = [
            npm,
            "install",
            "--no-audit",
            "--no-fund",
            _WECHATY_VERSION,
            _PUPPET_VERSION,
            _WECHAT4U_VERSION,
        ]
        logger.info("wechaty %s: %s", instance_id, " ".join(command))
        result = await asyncio.to_thread(
            subprocess.run, command, cwd=str(target), capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"npm install failed: {(result.stderr or result.stdout).strip()[:400]}"
            )
        logger.info("wechaty %s: installed at %s", instance_id, target)

    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
        node = _find_node()
        if node is None:
            raise RuntimeError("WeChaty needs Node.js >= 18; install Node first")
        target = _instance_dir(instance_id)
        if not (target / "wechaty-gateway.js").exists():
            raise RuntimeError(f"wechaty {instance_id} is not installed")
        port = int(options.get("port") or self._port_for(instance_id))
        token = str(options.get("token") or "")
        env = dict(options.get("env") or {})
        env["WECHATY_TOKEN"] = token
        env["GATEWAY_PORT"] = str(port)
        try:
            process = await asyncio.to_thread(
                subprocess.Popen,
                [node, "wechaty-gateway.js"],
                cwd=str(target),
                env={**__import__("os").environ, **env},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"failed to launch wechaty {instance_id}: {exc}") from exc
        self._processes[instance_id] = process
        ready = await self._wait_http(instance_id, wait_seconds=_READY_TIMEOUT)
        endpoint = self._endpoint(instance_id)
        if not ready:
            self._terminate(instance_id)
            raise RuntimeError(f"wechaty {instance_id} did not answer on {endpoint} in time")
        return GatewayInstance(
            provider="wechaty",
            instance_id=instance_id,
            status="running",
            endpoint=endpoint,
            extra={"port": port, "pid": process.pid},
        )

    def _terminate(self, instance_id: str) -> None:
        process = self._processes.pop(instance_id, None)
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
        running = await self._wait_http(instance_id, wait_seconds=2.0)
        if running:
            return GatewayInstance(
                provider="wechaty",
                instance_id=instance_id,
                status="running",
                endpoint=endpoint,
            )
        process = self._processes.get(instance_id)
        if process is not None and process.poll() is None:
            return GatewayInstance(
                provider="wechaty",
                instance_id=instance_id,
                status="starting",
                error="HTTP not answering yet",
                endpoint=endpoint,
            )
        return GatewayInstance(
            provider="wechaty",
            instance_id=instance_id,
            status="stopped",
            error="process not running",
        )

    async def qr(self, instance_id: str) -> str:
        """Login state for the guide: QR payload while waiting, the
        logged-in sentinel once the session is up, '' when pending."""
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
            logger.warning("wechaty %s /qr failed: %s", instance_id, exc)
            return ""


__all__ = ["WechatyGatewayProvisioner"]
