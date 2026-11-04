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
import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from mailflow.contracts import GatewayInstance, GatewayProvisioner
from mailflow.gateway import GatewayNotInstalledError
from mailflow.gateway_resources import (
    LocalProcessGateway,
    instance_directory,
    instance_token,
    run_command,
)

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
    return instance_token(instance_id)


def _instance_dir(instance_id: str) -> Path:
    return instance_directory(_data_root(), "whatsapp", instance_id)


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


class WhatsappProvisioner(LocalProcessGateway, GatewayProvisioner):
    provider = "whatsapp"

    def __init__(self) -> None:
        super().__init__(_instance_dir, _port_for)

    async def detect(self) -> str:
        node = await asyncio.to_thread(_find_node)
        npm = _find_npm()
        parts: list[str] = []
        parts.append(f"node {node}" if node else f"node toolchain not found ({_NODE_HINT})")
        parts.append(f"npm {npm}" if npm else f"npm not found ({_NPM_HINT})")
        installed = any(_deps_installed(d) for d in _data_root().glob("whatsapp-*") if d.is_dir())
        parts.append("installed" if installed else "not installed")
        parts.append("running" if await self._any_running() else "not running")
        return "; ".join(parts)

    async def _any_running(self) -> bool:
        return any(process.poll() is None for process in self._processes.values())

    async def install(self, instance_id: str, options: dict[str, Any]) -> None:
        node = await asyncio.to_thread(_find_node)
        if node is None:
            raise RuntimeError(f"whatsapp needs the Node.js toolchain: {_NODE_HINT}")
        target = _instance_dir(instance_id)
        self.prepare_directory(instance_id, target)
        progress = options.get("_progress")
        if progress is not None:
            progress.update(5.0, "writing the WhatsApp bridge…", "installing")
        await asyncio.to_thread(_write_payload, target)
        if _deps_installed(target):
            logger.info("whatsapp %s: bridge updated; reusing installed dependencies", instance_id)
            if progress is not None:
                progress.update(100.0, "bridge updated", "installed")
            return
        npm = _find_npm()
        if npm is None:
            raise RuntimeError(f"whatsapp needs npm to install its bridge: {_NPM_HINT}")
        if progress is not None:
            progress.update(
                20.0,
                "installing bridge dependencies (npm install; the first run downloads them)",
                "installing",
            )
        logger.info("whatsapp %s: npm install in %s", instance_id, target)
        result = await run_command(
            [npm, "install", "--no-audit", "--no-fund"],
            timeout_seconds=_NPM_TIMEOUT,
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
        bridge = _instance_dir(instance_id) / _BRIDGE_NAME
        if not bridge.exists():
            raise GatewayNotInstalledError(
                f"whatsapp {instance_id} is not installed; run setup first"
            )
        node = await asyncio.to_thread(_find_node)
        if node is None:
            raise RuntimeError(f"whatsapp {instance_id} needs Node.js to start: {_NODE_HINT}")
        return await self.start_process(
            instance_id, options, [node, _BRIDGE_NAME], timeout_seconds=_READY_TIMEOUT
        )


__all__ = ["WhatsappProvisioner"]
