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
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from mailflow.contracts import GatewayInstance, GatewayProvisioner
from mailflow.gateway import GatewayNotInstalledError, GatewaySetupError
from mailflow.gateway_resources import (
    LocalProcessGateway,
    instance_directory,
    instance_token,
    read_metadata,
    run_command,
)

logger = logging.getLogger("mailflow.gateway.openwechat")

_QR_LOGGED_IN = "__MAILFLOW_LOGGED_IN__"
_BASE_PORT = 8888
_READY_TIMEOUT = 30.0
_BRIDGE_GO = Path(__file__).parent / "gateway" / "openwechat-bridge.go"
_BRIDGE_NAME = "openwechat-bridge.exe" if os.name == "nt" else "openwechat-bridge"


def _data_root() -> Path:
    return Path("data") / "gateways"


def _instance_dir(instance_id: str) -> Path:
    """Filesystem-safe instance directory (spaces/parens -> dashes)."""
    return instance_directory(_data_root(), "openwechat", instance_id)


def _find_go() -> str | None:
    found = shutil.which("go")
    return found


class OpenWechatProvisioner(LocalProcessGateway, GatewayProvisioner):
    provider = "openwechat"

    def __init__(self) -> None:
        super().__init__(_instance_dir, self._port_for)

    @staticmethod
    def _port_for(instance_id: str) -> int:
        digest = hashlib.sha256(instance_token(instance_id).encode()).hexdigest()
        return _BASE_PORT + int(digest[:8], 16) % 97

    async def detect(self) -> str:
        go = await asyncio.to_thread(_find_go)
        installed = any(
            d.is_dir() and any((d / name).exists() for name in {_BRIDGE_NAME, "openwechat-bridge"})
            for d in _data_root().glob("openwechat-*")
        )
        if go is None and not installed:
            return "gateway.go_required"
        running = await self._any_running()
        parts = [f"go {go}"] if go else []
        parts.append("installed" if installed else "not installed")
        parts.append("running" if running else "not running")
        return "; ".join(parts)

    async def _any_running(self) -> bool:
        return any(process.poll() is None for process in self._processes.values())

    async def install(self, instance_id: str, options: dict[str, Any]) -> None:
        target = _instance_dir(instance_id)
        self.prepare_directory(instance_id, target)
        bridge = target / _BRIDGE_NAME
        sources = [_BRIDGE_GO, _BRIDGE_GO.with_name("go.mod"), _BRIDGE_GO.with_name("go.sum")]
        payload = await asyncio.to_thread(lambda: [source.read_bytes() for source in sources])
        source_hash = hashlib.sha256(b"\0".join(payload)).hexdigest()
        metadata = read_metadata(target)
        if bridge.exists() and metadata.get("bridge_source_hash") == source_hash:
            logger.info("openwechat %s: bridge already built", instance_id)
            return
        go = _find_go()
        if go is None:
            raise GatewaySetupError("gateway.go_required")
        progress = options.get("_progress")
        if progress is not None:
            progress.update(5.0, "building Go bridge (first run downloads Go modules)", "building")
        logger.info("openwechat %s: building bridge with %s", instance_id, go)
        # Installed package files may be read-only. Build a complete module
        # in the instance directory; retain its credentials on rebuilds.
        build_dir = target / "build"
        build_dir.mkdir(exist_ok=True)
        for source, data in zip(sources, payload, strict=True):
            await asyncio.to_thread((build_dir / source.name).write_bytes, data)
        result = await run_command(
            [go, "build", "-o", str(bridge.resolve()), "."],
            timeout_seconds=300,
            cwd=str(build_dir.resolve()),
            # CGO disabled: a cgo binary is dynamically linked against the
            # BUILD host's glibc and fails with a confusing
            # 'No such file or directory' (missing ELF interpreter) on
            # any other machine. A pure-Go static binary runs everywhere.
            env={**os.environ, "CGO_ENABLED": "0"},
        )
        if result.returncode != 0:
            logger.error("openwechat %s: bridge build failed: %s", instance_id, result.stderr)
            raise GatewaySetupError(
                "gateway.go_build_failed", detail=(result.stderr or result.stdout).strip()
            )
        await asyncio.to_thread(os.chmod, bridge, 0o755)
        await asyncio.to_thread(
            (target / "mailflow.json").write_text,
            json.dumps({**read_metadata(target), "bridge_source_hash": source_hash}),
            encoding="utf-8",
        )
        logger.info("openwechat %s: bridge ready", instance_id)

    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
        directory = _instance_dir(instance_id)
        bridge = directory / _BRIDGE_NAME
        legacy = directory / "openwechat-bridge"
        if os.name == "nt" and not bridge.exists() and legacy.exists():
            await asyncio.to_thread(shutil.copyfile, legacy, bridge)
        if not bridge.exists():
            raise GatewayNotInstalledError(
                f"openwechat {instance_id} is not installed; run setup first"
            )
        return await self.start_process(
            instance_id, options, [str(bridge.resolve())], timeout_seconds=_READY_TIMEOUT
        )
