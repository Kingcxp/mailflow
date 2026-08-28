"""Gateway provisioning guide: the Bots-tab modal that walks the user
through installing and starting a chat-platform gateway (NapCat, WeChaty).

Flow: detect → install (first use) → start → QR (NapCat/WeChaty) → done.
Every network or process operation runs in a worker; the modal stays
responsive and reports the current step with a Cancel button.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar

from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class GatewayGuideModal(ModalScreen[dict[str, Any] | None]):
    """One gateway instance: provision (install/start) and show the QR."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss", "Close")]

    def __init__(
        self,
        service: MailFlowService,
        provider: str,
        instance_id: str,
        options: dict[str, Any],
    ) -> None:
        super().__init__()
        self._service = service
        self._provider = provider
        self._instance_id = instance_id
        self._options = options
        self._result: dict[str, Any] | None = None

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Static(
            self._t("tui.bots_guide_title", provider=self._provider),
            id="guide-title",
        )
        yield Static("", id="guide-status")
        yield Static("", id="guide-qr")
        with Horizontal(id="guide-actions"):
            yield Button(self._t("tui.btn_cancel"), id="guide-cancel", variant="error")

    async def on_mount(self) -> None:
        self.run_worker(
            self._run_guide(), exclusive=True, group="gateway-guide", exit_on_error=False
        )

    def _set_status(self, text: str, style: str = "") -> None:
        node = self.query_one_optional("#guide-status", Static)
        if node is not None:
            node.update(f"[{style}]{text}[/{style}]" if style else text)

    def _set_qr(self, text: str) -> None:
        node = self.query_one_optional("#guide-qr", Static)
        if node is not None:
            node.update(text)

    async def _run_guide(self) -> None:
        service = self._service
        provider = self._provider
        try:
            # 1. detect
            self._set_status(self._t("tui.bots_guide_detecting"), "cyan")
            detected = await service.gateway_detect(provider)
            self._set_status(detected or self._t("tui.bots_guide_detected"), "dim")
            await asyncio.sleep(0.3)
            # 2. provision (install + start)
            self._set_status(self._t("tui.bots_guide_provisioning"), "cyan")
            instance = await service.gateway_provision(provider, self._instance_id, self._options)
            self._set_status(self._t("tui.bots_guide_running", endpoint=instance.endpoint), "green")
            # 3. QR login loop (NapCat / WeChaty)
            await self._qr_loop(service, provider)
            self._result = {
                "provider": provider,
                "instance_id": self._instance_id,
                "endpoint": instance.endpoint,
                "options": self._options,
            }
        except Exception as exc:
            self._set_status(str(exc), "red")
            return

    async def _qr_loop(self, service: MailFlowService, provider: str) -> None:
        """Poll the QR endpoint until the session logs in or 2 min passes."""
        self._set_status(self._t("tui.bots_guide_qr_wait"), "cyan")
        deadline = time.monotonic() + 120
        last_qr = ""
        while time.monotonic() < deadline:
            qr = await service.gateway_qr(provider, self._instance_id)
            if qr and qr != last_qr:
                last_qr = qr
                self._set_qr(_ascii_qr(qr))
                self._set_status(self._t("tui.bots_guide_qr_scan"), "yellow")
            elif not qr:
                # '' means logged in or not supported: check login state
                self._set_qr("")
            # wait, then check whether the session is up
            await asyncio.sleep(3.0)
            state = await service.gateway_instances()
            current = next((i for i in state if i.instance_id == self._instance_id), None)
            if current is not None and current.status == "running":
                # probe login via the provisioner's health
                login = await service.gateway_qr(provider, self._instance_id)
                if not login:
                    self._set_status(self._t("tui.bots_guide_logged_in"), "green")
                    return
        self._set_status(self._t("tui.bots_guide_qr_timeout"), "red")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "guide-cancel":
            self.dismiss(self._result)
            return

    def action_dismiss_modal(self) -> None:
        self.dismiss(self._result)


def _ascii_qr(image: str) -> str:
    """Render a QR image payload as an ASCII block (base64 PNG)."""
    import base64 as _b64

    if not image:
        return "(empty)"
    if image.startswith(("http://", "https://")):
        return f"(qr image: {image[:80]})"
    try:
        raw = _b64.b64decode(image, validate=False)
    except Exception:
        return f"(qr payload {image[:40]}…)"
    import struct
    import zlib

    try:
        pos = raw.find(b"IDAT") + 4
        compressed = raw[pos : raw.find(b"IEND")]
        pixels: bytes = zlib.decompress(compressed)
        width: int = struct.unpack(">I", raw[16:20])[0]
        height: int = struct.unpack(">I", raw[20:24])[0]
        stride = width * 4 + 1
        out: list[str] = []
        for y in range(0, min(height, 40), 2):
            row: list[str] = []
            for x in range(0, min(width, 80), 2):
                idx = y * stride + 1 + x * 4
                if idx + 2 < len(pixels):
                    r: int = pixels[idx]
                    g: int = pixels[idx + 1]
                    b: int = pixels[idx + 2]
                    row.append("  " if (r + g + b) // 3 > 128 else "██")
            out.append("".join(row))
        return "\n".join(out) or "(qr)"
    except Exception:
        return f"(qr payload {len(raw)} bytes)"


__all__ = ["GatewayGuideModal", "_ascii_qr"]
