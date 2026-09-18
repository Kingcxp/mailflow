"""WhatsApp gateway provisioner based on the Baileys Node.js library.

Scan-to-login with **no platform token**: the bridge drives the WhatsApp
multi-device protocol, renders the login QR to a PNG and serves it at
``GET /qr``; the TUI shows it inline. The Baileys multi-file auth state
lives in the instance dir (``auth/``), so the login survives restarts.

Requires a Node.js toolchain (>= 18) with npm to install the bridge
dependencies; MailFlow never installs system packages itself — when Node
is missing the provisioner reports the exact install hint.

This is a reference bridge: any service implementing the same three
endpoints (``/health``, ``/qr``, ``/send``) works with MailFlow.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
from mailflow.contracts import GatewayInstance, GatewayProvisioner
from mailflow.gateway import GatewayNotInstalledError

logger = logging.getLogger("mailflow.gateway.whatsapp")

_QR_LOGGED_IN = "__MAILFLOW_LOGGED_IN__"
# distinct band: openwechat owns 8888-8984 (digit-hash over its own ids), so
# a 8898 base would let a WhatsApp instance collide with a WeChat bridge and
# be "reused" as if it were already running
_BASE_PORT = 9000
_PORT_BAND = 900
_READY_TIMEOUT = 60.0  # the first Baileys handshake plus npm-cold start
_NPM_TIMEOUT = 900.0
_BRIDGE_SOURCE = Path(__file__).parent / "gateway" / "whatsapp-bridge.mjs"
_BRIDGE_NAME = "whatsapp-bridge.mjs"

# pinned: Baileys 7.x is a release candidate, and the 6.7.x line is the
# verified stable one on this host
_BAILEYS_VERSION = "6.7.24"

_NODE_HINT = (
    "install Node.js >= 18 (https://nodejs.org/en/download) and make sure "
    "`node` resolves on PATH; MailFlow never installs system packages itself"
)
_NPM_HINT = (
    "install Node.js >= 18 with npm (https://nodejs.org/en/download) and "
    "make sure `npm` resolves on PATH; MailFlow never installs system "
    "packages itself"
)


def _data_root() -> Path:
    return Path("data") / "gateways"


def _safe_token(instance_id: str) -> str:
    """Filesystem-safe instance token (spaces/parens -> dashes)."""
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in str(instance_id)).strip("-")


def _instance_dir(instance_id: str) -> Path:
    return _data_root() / f"whatsapp-{_safe_token(instance_id)}"


def _port_for(instance_id: str) -> int:
    """Stable, collision-resistant per-instance port.

    The digest is taken over the *sanitized* token — exactly the string in
    the instance dir name — so a port can be re-derived from a directory
    (``detect``) without a second, subtly different scheme.
    """
    digest = hashlib.sha1(_safe_token(instance_id).encode()).hexdigest()
    return _BASE_PORT + int(digest[:4], 16) % _PORT_BAND


def _find_node() -> str | None:
    """Path to a usable ``node`` binary (Baileys needs Node >= 18)."""
    node = shutil.which("node")
    if node is None:
        return None
    try:
        result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        major = int((result.stdout or "").strip().lstrip("v").split(".")[0])
    except ValueError:
        return None
    return node if major >= 18 else None


def _find_npm() -> str | None:
    """Path to the npm CLI (Windows resolves the ``npm.cmd`` shim)."""
    return shutil.which("npm")


def _deps_installed(target: Path) -> bool:
    return (target / "node_modules" / "@whiskeysockets" / "baileys").is_dir()


def _write_payload(target: Path) -> None:
    """Copy the bridge source and write its pinned dependency manifest."""
    shutil.copyfile(_BRIDGE_SOURCE, target / _BRIDGE_NAME)
    (target / "package.json").write_text(
        json.dumps(
            {
                "name": "mailflow-whatsapp-bridge",
                "version": "0.1.0",
                "private": True,
                "type": "module",
                "dependencies": {
                    "@whiskeysockets/baileys": _BAILEYS_VERSION,
                    "pino": "^9.6.0",
                    "qrcode": "^1.5.4",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


class WhatsappProvisioner(GatewayProvisioner):
    provider = "whatsapp"

    def _endpoint(self, instance_id: str) -> str:
        return f"http://127.0.0.1:{_port_for(instance_id)}"

    async def detect(self) -> str:
        node = _find_node()
        npm = _find_npm()
        parts: list[str] = []
        parts.append(f"node {node}" if node else f"node toolchain not found ({_NODE_HINT})")
        parts.append(f"npm {npm}" if npm else f"npm not found ({_NPM_HINT})")
        installed = any(_deps_installed(d) for d in _data_root().glob("whatsapp-*") if d.is_dir())
        parts.append("installed" if installed else "not installed")
        parts.append("running" if await self._any_running() else "not running")
        return "; ".join(parts)

    async def _any_running(self) -> bool:
        for directory in _data_root().glob("whatsapp-*"):
            if not directory.is_dir():
                continue
            # _port_for hashes the sanitized token, so the directory name
            # maps back onto the instance's real port
            token = directory.name[len("whatsapp-") :]
            if await self._wait_http_port(_port_for(token), wait_seconds=2.0):
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
        node = _find_node()
        if node is None:
            raise RuntimeError(f"whatsapp needs the Node.js toolchain: {_NODE_HINT}")
        npm = _find_npm()
        if npm is None:
            raise RuntimeError(f"whatsapp needs npm to install its bridge: {_NPM_HINT}")
        target = _instance_dir(instance_id)
        if (target / _BRIDGE_NAME).exists() and _deps_installed(target):
            logger.info("whatsapp %s: bridge already installed at %s", instance_id, target)
            progress_done = options.get("_progress")
            if progress_done is not None:
                progress_done.update(100.0, "bridge already installed", "installed")
            return
        target.mkdir(parents=True, exist_ok=True)
        progress = options.get("_progress")
        if progress is not None:
            progress.update(5.0, "writing the WhatsApp bridge…", "installing")
        await asyncio.to_thread(_write_payload, target)
        if progress is not None:
            progress.update(
                20.0,
                "installing bridge dependencies (npm install; the first run downloads them)",
                "installing",
            )
        logger.info("whatsapp %s: npm install in %s", instance_id, target)
        result = await asyncio.to_thread(
            subprocess.run,
            [npm, "install", "--no-audit", "--no-fund"],
            capture_output=True,
            text=True,
            timeout=_NPM_TIMEOUT,
            cwd=str(target),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-400:]
            raise RuntimeError(f"whatsapp {instance_id}: npm install failed: {detail}")
        if not _deps_installed(target):
            raise RuntimeError(
                f"whatsapp {instance_id}: npm install finished but "
                f"@whiskeysockets/baileys is missing under {target}"
            )
        if progress is not None:
            progress.update(100.0, "bridge installed", "installed")
        logger.info("whatsapp %s: bridge ready", instance_id)

    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
        target = _instance_dir(instance_id)
        bridge = target / _BRIDGE_NAME
        if not bridge.exists():
            raise GatewayNotInstalledError(
                f"whatsapp {instance_id} is not installed (no {_BRIDGE_NAME} "
                f"under {target}); run the setup to install it"
            )
        node = _find_node()
        if node is None:
            raise RuntimeError(f"whatsapp {instance_id} needs Node.js to start: {_NODE_HINT}")
        port = int(options.get("port") or _port_for(instance_id))
        if await self._wait_http_port(port, wait_seconds=1.0):
            logger.info("whatsapp %s: reusing running gateway on :%d", instance_id, port)
            return GatewayInstance(
                provider="whatsapp",
                instance_id=instance_id,
                status="running",
                endpoint=f"http://127.0.0.1:{port}",
                extra={"port": port, "reused": True},
            )
        log_file = target / "whatsapp.log"

        def _launch() -> subprocess.Popen[Any]:
            env = {**os.environ, **dict(options.get("env") or {})}
            env["GATEWAY_PORT"] = str(port)
            # chat command dispatch endpoint (see mailflow.bot_server); the
            # bridge forwards incoming text messages here and sends every
            # returned reply page back to the same chat
            env["MAILFLOW_BOT_URL"] = str(options.get("bot_url") or "")
            env["MAILFLOW_PROVIDER"] = "whatsapp"
            env["MAILFLOW_INSTANCE"] = instance_id
            with open(log_file, "ab") as handle:
                return subprocess.Popen(
                    [node, _BRIDGE_NAME],
                    cwd=str(target),
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )

        # resolve: the bridge script, the log and the cwd must survive the
        # directory they are launched from, and a clear ENOENT beats a
        # confusing relative one
        bridge = bridge.resolve()
        log_file = log_file.resolve()
        target = target.resolve()
        try:
            process = await asyncio.to_thread(_launch)
        except OSError as exc:
            raise RuntimeError(f"failed to launch whatsapp {instance_id}: {exc}") from exc
        self._processes = getattr(self, "_processes", {})
        self._processes[instance_id] = process
        endpoint = self._endpoint(instance_id)
        deadline = asyncio.get_running_loop().time() + _READY_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if await self._wait_http_port(port, wait_seconds=2.0):
                return GatewayInstance(
                    provider="whatsapp",
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
                    f"whatsapp {instance_id} exited early (code "
                    f"{proc.returncode}); see {log_file}{tail}"
                )
            await asyncio.sleep(2.0)
        self._terminate(instance_id)
        tail = self._tail_log(log_file)
        raise RuntimeError(
            f"whatsapp {instance_id} did not answer on {endpoint} in "
            f"{_READY_TIMEOUT:.0f}s; see {log_file}{tail}"
        )

    def _terminate(self, instance_id: str) -> None:
        processes = getattr(self, "_processes", {})
        process = processes.pop(instance_id, None)
        if process is None:
            return
        if process.poll() is None:
            with contextlib.suppress(Exception):
                process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                with contextlib.suppress(Exception):
                    process.kill()

    async def stop(self, instance_id: str) -> None:
        await asyncio.to_thread(self._terminate, instance_id)

    async def status(self, instance_id: str) -> GatewayInstance:
        endpoint = self._endpoint(instance_id)
        running = await self._wait_http_port(_port_for(instance_id), wait_seconds=2.0)
        if running:
            return GatewayInstance(
                provider="whatsapp",
                instance_id=instance_id,
                status="running",
                endpoint=endpoint,
            )
        return GatewayInstance(
            provider="whatsapp",
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
            logger.warning("whatsapp %s /qr failed: %s", instance_id, exc)
            return ""

    @staticmethod
    def _tail_log(log_file: Path, lines: int = 10) -> str:
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = content[-lines:]
            return "\n  log: " + "\n  log: ".join(tail) if tail else ""
        except OSError:
            return ""


__all__ = ["WhatsappProvisioner"]
