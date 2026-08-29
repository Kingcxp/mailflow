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
from textual.widgets import (  # pyright: ignore[reportUnknownVariableType]
    Button,
    ProgressBar,
    RichLog,
    Static,
)

_LEVEL_COLORS = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "WARN": "yellow",
    "ERROR": "red",
}


class GatewayGuideModal(ModalScreen[dict[str, Any] | None]):
    """One gateway instance: provision (install/start) and show the QR."""

    DEFAULT_CSS = """
    GatewayGuideModal {
        align: center middle;
    }
    """

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
        # one single child (the dialog) so Textual's ModalScreen centers it
        # like every other MailFlow dialog (entry-form etc.)
        with Vertical(id="guide-dialog"):
            yield Static(
                self._t("tui.bots_guide_title", provider=self._provider),
                id="guide-title",
            )
            # QR first so it is never pushed off by the log pane
            yield Static("", id="guide-qr")
            with Vertical(id="guide-log-wrap"):
                # download/install progress lives inside the log pane so
                # it reads as part of the stream, not a separate widget
                yield ProgressBar(
                    total=100,
                    show_percentage=True,
                    id="guide-progress",
                )
                yield Static("", id="guide-progress-label")
                yield RichLog(
                    id="guide-log", wrap=True, highlight=True, markup=True, max_lines=2000
                )
            yield Static("", id="guide-status")
            with Horizontal(id="guide-actions"):
                yield Button(
                    self._t("tui.btn_done", default="Done"),
                    id="guide-done",
                    variant="success",
                    disabled=True,
                )
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

    # -- install progress ---------------------------------------------------------

    def _show_progress(self, visible: bool) -> None:
        bar = self.query_one_optional("#guide-progress", ProgressBar)
        label = self.query_one_optional("#guide-progress-label", Static)
        for node in (bar, label):
            if node is not None:
                node.display = visible

    def _update_progress(self, percent: float, message: str) -> None:
        bar = self.query_one_optional("#guide-progress", ProgressBar)
        if bar is not None:
            bar.progress = percent  # pyright: ignore[reportUnknownMemberType]
        label = self.query_one_optional("#guide-progress-label", Static)
        if label is not None:
            label.update(message)
        # throttle log lines: one per ~5% so the pane scrolls instead of
        # flooding on every 64 KB chunk
        bucket = int(percent // 5)
        if bucket != getattr(self, "_progress_bucket", -1):
            self._progress_bucket = bucket
            self._log("INFO", f"{percent:.0f}% — {message}")

    # -- guide flow --------------------------------------------------------------

    async def _run_guide(self) -> None:
        service = self._service
        provider = self._provider
        try:
            # WeChaty: pad protocol when a token is set, else the web
            # protocol (wechat4u) as a best-effort fallback
            # 1. detect
            self._log("INFO", self._t("tui.bots_guide_detecting"))
            detected = await service.gateway_detect(provider)
            self._log("INFO", detected or self._t("tui.bots_guide_detected"))
            await asyncio.sleep(0.2)
            # 2. provision (install + start); while the provisioner works,
            # poll the shared InstallProgress (injected into its options)
            # to render a live download/install progress bar
            self._log("INFO", self._t("tui.bots_guide_provisioning"))
            self._update_progress(
                0.0, self._t("tui.bots_guide_downloading", provider=self._provider)
            )
            self._show_progress(True)
            try:
                instance = await self._provision_with_progress(service, provider)
            finally:
                self._show_progress(False)
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

    async def _provision_with_progress(self, service: MailFlowService, provider: str) -> Any:
        from mailflow.gateway import InstallProgress

        progress: InstallProgress | None = None
        last_pct = -1.0

        def _poll() -> None:
            nonlocal progress, last_pct
            if progress is None or progress._done:  # pyright: ignore[reportPrivateUsage]
                return
            if progress.percent != last_pct or progress.message:
                last_pct = progress.percent
                self._update_progress(progress.percent, progress.message)

        # keep the TUI live while the provisioner installs
        task = asyncio.create_task(
            service.gateway_provision(provider, self._instance_id, self._options)
        )
        while not task.done():
            await asyncio.sleep(0.2)
            # the provisioner mutates the options dict in place? no — it
            # receives a copy with _progress injected; read it from the
            # options copy the service made? Simplest: re-inject by
            # querying the manager — use the service's last progress
            progress = getattr(service.gateways, "_last_progress", None)
            _poll()
        return await task

    _QR_LOGGED_IN = "__MAILFLOW_LOGGED_IN__"

    async def _qr_loop(self, service: MailFlowService, provider: str) -> None:
        """Poll the QR endpoint until the session logs in or 2 min passes.

        Login is decided by the provisioner's sentinel, never by the
        absence of a QR payload (an empty response also happens before
        the QR exists and would fake a login)."""
        self._log("INFO", self._t("tui.bots_guide_qr_wait", provider=self._provider))
        self._set_status(self._t("tui.bots_guide_qr_pending", provider=self._provider), "yellow")
        deadline = time.monotonic() + 120
        last_qr = ""
        self._logged_no_qr = False
        while time.monotonic() < deadline:
            qr = await service.gateway_qr(provider, self._instance_id)
            if qr.startswith("ERROR:"):
                # the provisioner diagnosed why no QR exists: show it
                self._log("ERROR", qr[len("ERROR:") :].strip())
                self._set_status(qr[len("ERROR:") :].strip(), "red")
                return
            if qr == self._QR_LOGGED_IN:
                self._set_qr("")
                self._log("INFO", self._t("tui.bots_guide_logged_in"))
                self._set_status(self._t("tui.bots_guide_logged_in"), "green")
                self._finish_ready()
                return
            if qr and qr != last_qr:
                last_qr = qr
                self._set_qr(_ascii_qr(qr))
                self._log("INFO", self._t("tui.bots_guide_qr_scan", provider=self._provider))
            elif qr:
                # same QR as before: still waiting for the scan
                self._set_status(
                    self._t("tui.bots_guide_qr_wait_scan", provider=self._provider), "yellow"
                )
            else:
                self._set_qr("")
                # no QR yet: keep the user informed instead of a blank box
                if not self._logged_no_qr:
                    self._log("INFO", self._t("tui.bots_guide_qr_pending", provider=self._provider))
                    self._logged_no_qr = True
                self._set_status(
                    self._t("tui.bots_guide_qr_pending", provider=self._provider), "yellow"
                )
            await asyncio.sleep(3.0)
        self._log("ERROR", self._t("tui.bots_guide_qr_timeout"))
        self._set_status(self._t("tui.bots_guide_qr_timeout"), "red")

    def _finish_ready(self) -> None:
        """Login done: enable the Done button so the user explicitly
        finishes the flow (Cancel keeps meaning abort)."""
        done = self.query_one_optional("#guide-done", Button)
        if done is not None:
            done.disabled = False
            done.focus()  # pyright: ignore[reportUnknownMemberType]

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "guide-done":
            # the gateway is up and logged in: finish with the result so
            # the caller persists the notifier entry
            if self._result is not None:
                self.dismiss(self._result)
            return
        if event.button.id == "guide-cancel":
            await self._cancel_and_cleanup()
            return

    async def _cancel_and_cleanup(self) -> None:
        """Abort the wizard: stop the gateway we started so no orphan
        process keeps running, then close without saving."""
        self._log("WARN", self._t("tui.bots_guide_cancelled"))
        try:
            await self._service.gateway_shutdown(self._provider, self._instance_id)
            self._log("INFO", self._t("tui.bots_guide_stopped"))
        except Exception as exc:
            self._log("ERROR", self._t("tui.bots_guide_stop_failed", error=str(exc)))
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        # escape = abort, same as Cancel
        self._cleanup_task = asyncio.create_task(self._cancel_and_cleanup())


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
