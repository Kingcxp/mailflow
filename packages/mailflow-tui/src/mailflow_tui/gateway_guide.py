"""Gateway provisioning guide: the Bots-tab modal that walks the user
through installing and starting a chat-platform gateway (NapCat, WeChaty).

The modal is a bordered dialog: a title, a live log pane (timestamped,
level-tagged, scrollable) and a QR area inside the frame; the buttons
live outside the frame like every other MailFlow dialog. Every network or
process operation runs in a worker; the log pane streams each step.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, ClassVar

from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Static  # pyright: ignore[reportUnknownVariableType]

_LEVEL_COLORS = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "WARN": "yellow",
    "ERROR": "red",
}


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
        with Vertical(id="guide-dialog"):
            with Vertical(id="guide-log-wrap"):
                yield RichLog(
                    id="guide-log", wrap=True, highlight=True, markup=True, max_lines=2000
                )
            yield Static("", id="guide-qr")
            yield Static("", id="guide-status")
        with Horizontal(id="guide-actions"):
            yield Button(self._t("tui.btn_cancel"), id="guide-cancel", variant="error")

    async def on_mount(self) -> None:
        self._log(
            "INFO",
            self._t("tui.bots_guide_starting", provider=self._provider),
        )
        self.run_worker(
            self._run_guide(), exclusive=True, group="gateway-guide", exit_on_error=False
        )

    # -- log pane ---------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        """Append one timestamped, level-tagged line to the log pane."""
        node = self.query_one_optional("#guide-log", RichLog)  # pyright: ignore[reportUnknownVariableType]
        if node is None:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        color = _LEVEL_COLORS.get(level, "")
        if color:
            node.write(f"[{color}]{stamp} {level:5}[/{color}] {message}")  # pyright: ignore[reportUnknownMemberType]
        else:
            node.write(f"{stamp} {level:5} {message}")  # pyright: ignore[reportUnknownMemberType]

    def _set_status(self, text: str, style: str = "") -> None:
        node = self.query_one_optional("#guide-status", Static)
        if node is not None:
            node.update(f"[{style}]{text}[/{style}]" if style else text)

    def _set_qr(self, text: str) -> None:
        node = self.query_one_optional("#guide-qr", Static)
        if node is not None:
            node.update(text)

    # -- guide flow --------------------------------------------------------------

    async def _run_guide(self) -> None:
        service = self._service
        provider = self._provider
        try:
            # 1. detect
            self._log("INFO", self._t("tui.bots_guide_detecting"))
            detected = await service.gateway_detect(provider)
            self._log("INFO", detected or self._t("tui.bots_guide_detected"))
            await asyncio.sleep(0.2)
            # 2. provision (install + start)
            self._log("INFO", self._t("tui.bots_guide_provisioning"))
            instance = await service.gateway_provision(provider, self._instance_id, self._options)
            self._log("INFO", self._t("tui.bots_guide_running", endpoint=instance.endpoint))
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
            self._log("ERROR", str(exc))
            self._set_status(str(exc), "red")
            return

    async def _qr_loop(self, service: MailFlowService, provider: str) -> None:
        """Poll the QR endpoint until the session logs in or 2 min passes."""
        self._log("INFO", self._t("tui.bots_guide_qr_wait"))
        self._set_status(self._t("tui.bots_guide_qr_scan"), "yellow")
        deadline = time.monotonic() + 120
        last_qr = ""
        while time.monotonic() < deadline:
            qr = await service.gateway_qr(provider, self._instance_id)
            if qr and qr != last_qr:
                last_qr = qr
                self._set_qr(_ascii_qr(qr))
                self._log("INFO", self._t("tui.bots_guide_qr_scan"))
            elif not qr:
                self._set_qr("")
            # wait, then check whether the session is up
            await asyncio.sleep(3.0)
            state = await service.gateway_instances()
            current = next((i for i in state if i.instance_id == self._instance_id), None)
            if current is not None and current.status == "running":
                login = await service.gateway_qr(provider, self._instance_id)
                if not login:
                    self._log("INFO", self._t("tui.bots_guide_logged_in"))
                    self._set_status(self._t("tui.bots_guide_logged_in"), "green")
                    return
        self._log("ERROR", self._t("tui.bots_guide_qr_timeout"))
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
