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
import logging
import os
import shutil
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

logger = logging.getLogger("mailflow.gateway.openwechat")

_QR_LOGGED_IN = "__MAILFLOW_LOGGED_IN__"
_BASE_PORT = 8888
_READY_TIMEOUT = 30.0
_BRIDGE_GO = Path(__file__).parent / "gateway" / "openwechat-bridge.go"


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
        return any(process.poll() is None for process in self._processes.values())

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
        self.prepare_directory(instance_id, target)
        bridge = target / "openwechat-bridge"
        if bridge.exists():
            logger.info("openwechat %s: bridge already built", instance_id)
            return
        progress = options.get("_progress")
        if progress is not None:
            progress.update(5.0, "building Go bridge (first run downloads Go modules)", "building")
        logger.info("openwechat %s: building bridge with %s", instance_id, go)
        # build INSIDE the module dir: single-file mode ignores go.mod and
        # fails with 'no required module provides package
        # github.com/eatmoreapple/openwechat' (the user's deploy log)
        result = await run_command(
            [go, "build", "-o", str(bridge.resolve()), "."],
            timeout_seconds=300,
            cwd=str(_BRIDGE_GO.parent.resolve()),
            # CGO disabled: a cgo binary is dynamically linked against the
            # BUILD host's glibc and fails with a confusing
            # 'No such file or directory' (missing ELF interpreter) on
            # any other machine. A pure-Go static binary runs everywhere.
            env={**os.environ, "CGO_ENABLED": "0"},
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"openwechat {instance_id}: bridge build failed: {result.stderr.strip()[:400]}"
            )
        await asyncio.to_thread(os.chmod, bridge, 0o755)
        logger.info("openwechat %s: bridge ready", instance_id)

    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
        bridge = _instance_dir(instance_id) / "openwechat-bridge"
        if not bridge.exists():
            raise GatewayNotInstalledError(
                f"openwechat {instance_id} is not installed; run setup first"
            )
        return await self.start_process(
            instance_id, options, [str(bridge.resolve())], timeout_seconds=_READY_TIMEOUT
        )
