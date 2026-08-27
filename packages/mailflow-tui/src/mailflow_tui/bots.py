"""平台登录: IM bot instance management and login-state probes.

Thin adapter over externally-typed frameworks; checked in basic mode."""

# pyright: basic

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import httpx
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static


class _BotStatusProbe:
    """Connectivity probes for IM bot backends (OneBot v11 / WeChaty /
    OpenClaw). Each returns a human-readable status line."""

    @staticmethod
    async def probe(provider: str, options: dict[str, Any], t: Any) -> str:
        def http_status(code: int) -> str:
            return str(t("tui.bots_http_status", status=code))

        try:
            if provider == "onebot":
                url = str(options.get("http_url", "")).rstrip("/")
                if not url:
                    return str(t("tui.bots_not_configured"))
                onebot_headers: dict[str, str] = {"Content-Type": "application/json"}
                token = str(options.get("access_token", ""))
                if token:
                    onebot_headers["Authorization"] = f"Bearer {token}"
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.post(
                        f"{url}/get_login_info", json={}, headers=onebot_headers
                    )
                if response.status_code == 200:
                    payload_data: dict[str, Any] = dict(response.json().get("data") or {})
                    nickname = str(payload_data.get("nickname", "?"))
                    user_id = str(payload_data.get("user_id", "?"))
                    return str(t("tui.bots_logged_in_as", name=nickname, uid=user_id))
                return http_status(response.status_code)
            if provider == "wechaty":
                url = str(options.get("gateway_url", "")).rstrip("/")
                if not url:
                    return "not configured"
                wechaty_headers: dict[str, str] = {}
                token_w = str(options.get("token", ""))
                if token_w:
                    wechaty_headers["Authorization"] = f"Bearer {token_w}"
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(f"{url}/health", headers=wechaty_headers)
                return str(
                    t("tui.bots_online")
                    if response.status_code == 200
                    else http_status(response.status_code)
                )
            if provider == "openclaw-weixin":
                url = str(options.get("base_url", "")).rstrip("/")
                if not url:
                    return "not configured"
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(url)
                return (
                    t("tui.bots_gateway_reachable")
                    if response.status_code < 500
                    else http_status(response.status_code)
                )
        except Exception as exc:
            return str(t("tui.bots_unreachable", error=type(exc).__name__))
        return str(t("tui.bots_unknown_provider"))


async def _probe_local_runtimes() -> list[dict[str, str]]:
    """Detect bot runtimes on well-known local ports.

    Returns one entry per reachable service:
    - onebot: NapCat / Lagrange / go-cqhttp HTTP (default 3000, also 5700, 6099)
    - wechaty: a WeChaty-style gateway (default 8788, also 8080, 10086)
    """
    found: list[dict[str, str]] = []

    async def _http(url: str, budget: float = 1.5) -> bool:
        try:
            async with httpx.AsyncClient(timeout=budget) as client:
                response = await client.get(url)
                return response.status_code < 500
        except Exception:
            return False

    import asyncio

    probes: list[tuple[str, str, str]] = []
    for port in ("3000", "5700", "6099", "8081"):
        probes.append(("onebot", f"http://127.0.0.1:{port}", port))
    for port in ("8788", "8080", "10086"):
        probes.append(("wechaty", f"http://127.0.0.1:{port}", port))

    results = await asyncio.gather(
        *[_http(url) for _kind, url, _port in probes], return_exceptions=True
    )
    for (kind, url, port), ok in zip(probes, results, strict=True):
        if ok:
            found.append({"provider": kind, "url": url, "port": port})
    return found


class NapCatQrModal(ModalScreen[str | None]):
    """Drives the NapCat QR login inside the TUI.

    OneBot action ``get_qrcode`` returns the QR image; we render it as an
    ASCII block (terminal-agnostic) and poll ``get_login_info`` until the
    session comes online. Returns the OneBot base URL on success."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss", "Close")]

    def __init__(self, service: MailFlowService, base_url: str) -> None:
        super().__init__()
        self._service = service
        self._base_url = base_url.rstrip("/")

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Static(self._t("tui.bots_qr_title"), id="bots-qr-title")
        yield Static("", id="bots-qr-image")
        yield Static("", id="bots-qr-status")
        with Horizontal(id="bots-qr-actions"):
            yield Button(self._t("tui.btn_cancel"), id="bots-qr-cancel", variant="error")

    async def on_mount(self) -> None:
        self.run_worker(self._login_loop(), exclusive=True, group="napcat-qr", exit_on_error=False)

    async def _login_loop(self) -> None:
        headers = {"Content-Type": "application/json"}
        for _ in range(120):  # 2 minutes of polling
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    qr = await client.post(f"{self._base_url}/get_qrcode", json={}, headers=headers)
                    qr.raise_for_status()
                    data = qr.json().get("data") or {}
                    image = data.get("qrcode") or data.get("image") or ""
                    if image:
                        self.query_one("#bots-qr-image", Static).update(_ascii_qr(image))
                async with httpx.AsyncClient(timeout=8.0) as client:
                    login = await client.post(
                        f"{self._base_url}/get_login_info", json={}, headers=headers
                    )
                    login.raise_for_status()
                    payload = login.json().get("data") or {}
                    if payload.get("user_id"):
                        self.query_one("#bots-qr-status", Static).update(
                            f"[green]{self._t('tui.bots_qr_ok')}[/green]"
                        )
                        self.dismiss(self._base_url)
                        return
            except Exception as exc:
                self.query_one("#bots-qr-status", Static).update(
                    f"[yellow]{escape(str(exc)[:120])}[/yellow]"
                )
            await asyncio.sleep(1.0)
        self.query_one("#bots-qr-status", Static).update(
            f"[red]{self._t('tui.bots_qr_timeout')}[/red]"
        )


def _ascii_qr(image: str) -> str:
    """Render a QR image payload as an ASCII block.

    Accepts base64 PNG data or an image URL; the URL case is rendered as a
    hint since the TUI cannot fetch binary images without extra deps."""
    import base64 as _b64

    if not image:
        return "(empty)"
    if image.startswith(("http://", "https://")):
        return f"(qr image: {image[:80]})"
    try:
        raw = _b64.b64decode(image, validate=False)
    except Exception:
        return f"(qr payload {image[:40]}…)"
    # PNG → 8x8 luminance blocks: decode the IDAT via zlib (small QR PNGs)
    import struct
    import zlib

    try:
        pos = raw.find(b"IDAT") + 4
        compressed = raw[pos : raw.find(b"IEND")]
        pixels = zlib.decompress(compressed)
        width = struct.unpack(">I", raw[16:20])[0]
        height = struct.unpack(">I", raw[20:24])[0]
        # RGBA scanlines with a filter byte each; skip filter bytes
        stride = width * 4 + 1
        out: list[str] = []
        for y in range(0, min(height, 40), 2):
            row: list[str] = []
            for x in range(0, min(width, 80), 2):
                idx = y * stride + 1 + x * 4
                if idx + 2 < len(pixels):
                    r, g, b = pixels[idx], pixels[idx + 1], pixels[idx + 2]
                    row.append("  " if (r + g + b) // 3 > 128 else "██")
            out.append("".join(row))
        return "\n".join(out) or "(qr)"
    except Exception:
        return f"(qr payload {len(raw)} bytes)"


class BotsPane(Vertical):
    """平台登录: manage IM bot instances (OneBot/WeChaty/OpenClaw) and
    check their login state. QR scanning happens in the bot runtime itself
    (NapCat / WeChaty gateway / OpenClaw) — this tab verifies the session."""

    IM_PROVIDERS: ClassVar[frozenset[str]] = frozenset({"onebot", "wechaty", "openclaw-weixin"})

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._selected_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(self._service.t("tui.bots_title"), id="bots-title")
        yield Static(escape(self._service.t("tui.bots_login_help")), id="bots-login-help")
        with ScrollableContainer(id="bots-table-wrap"):
            yield DataTable(id="bots-table", cursor_type="row")  # pyright: ignore[reportUnknownMemberType]
        with Horizontal(id="bots-actions"):
            yield Button(self._service.t("tui.btn_add"), id="bots-add", variant="success")
            yield Button(self._service.t("tui.btn_delete"), id="bots-delete", variant="error")
            yield Button(self._service.t("tui.bots_check"), id="bots-check", variant="primary")
        yield Static("", id="bots-status")

    def _im_instances(self) -> list[tuple[str, str, dict[str, Any]]]:
        out: list[tuple[str, str, dict[str, Any]]] = []
        for notifier in self._service.config.notifiers:
            if notifier.provider in self.IM_PROVIDERS:
                out.append((notifier.notifier_id, notifier.provider, notifier.options))
        return out

    def _ensure_columns(self) -> None:
        table: DataTable[Any] = self.query_one("#bots-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        table.clear(columns=True)
        table.add_column(self._service.t("plugin.header_name"), key="name")
        table.add_column(
            self._service.t("plugin.market_provider", default="provider"), key="provider"
        )
        table.add_column(self._service.t("tui.bots_targets"), key="targets")
        table.add_column(self._service.t("tui.bots_status"), key="status")

    def _render_rows(self, statuses: dict[str, str] | None = None) -> None:
        statuses = statuses or {}
        table: DataTable[Any] = self.query_one("#bots-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        table.clear()
        for notifier_id, provider, options in self._im_instances():
            raw_targets = list(options.get("targets") or [])
            targets = ", ".join(str(t) for t in raw_targets) or "-"
            table.add_row(
                notifier_id,
                provider,
                escape(targets),
                statuses.get(notifier_id, "-"),
                key=notifier_id,
            )

    def on_mount(self) -> None:
        self._ensure_columns()
        self._render()

    async def relabel(self) -> None:
        self.on_mount()

    def refresh_data(self) -> None:
        self._ensure_columns()
        self._render()

    def on_data_table_row_selected(self, event: Any) -> None:
        self._selected_id = str(event.row_key.value)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "bots-add":
            self.run_worker(
                self._auto_setup_flow(),
                exclusive=True,
                group="bots-setup",
                exit_on_error=False,
            )
            return
        if button_id == "bots-delete":
            await self._delete_selected()
            return
        if button_id != "bots-check":
            return
        # probes hit the network (8s timeout each): never run them on the
        # button handler or a down gateway freezes the whole UI
        self.run_worker(self._check_all(), exclusive=True, group="bots-check", exit_on_error=False)

    async def _auto_setup_flow(self) -> None:
        """Detect local runtimes and guide the user through setup; for
        NapCat also drive the QR login inside the TUI."""
        from mailflow_tui.settings import EntryFormScreen

        status = self.query_one("#bots-status", Static)
        status.update(self._service.t("tui.bots_detecting"))
        found = await _probe_local_runtimes()
        if not found:
            status.update(f"[yellow]{self._service.t('tui.bots_no_runtime')}[/yellow]")
            return
        # 探测到多个时优先 onebot
        chosen = next((f for f in found if f["provider"] == "onebot"), found[0])
        provider = chosen["provider"]
        url = chosen["url"]
        if provider == "onebot":
            # NapCat：先在 TUI 内完成扫码登录，成功后自动写入配置
            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                NapCatQrModal(self._service, url),
                lambda base: self._finish_onebot_setup(str(base)) if base else None,
            )
            return
        prefill: dict[str, Any] = {"provider": provider, "options": {}}
        prefill["options"]["gateway_url"] = url
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            EntryFormScreen(self._service, "notifiers", values=prefill)
        )
        self.call_after_refresh(self.refresh_data)

    def _finish_onebot_setup(self, base_url: str) -> None:
        """Write the detected NapCat instance as a notifier and refresh."""

        self.run_worker(self._persist_onebot(base_url), exclusive=True, group="bots-setup")

    async def _persist_onebot(self, base_url: str) -> None:
        notifiers = list(self._service.config.notifiers)
        if any(
            n.provider == "onebot"
            and str(n.options.get("http_url", "")).rstrip("/") == base_url.rstrip("/")
            for n in notifiers
        ):
            self.query_one("#bots-status", Static).update(
                f"[green]{self._service.t('tui.bots_already_configured')}[/green]"
            )
            self.refresh_data()
            return
        instance_id = "onebot"
        suffix = 1
        while any(n.notifier_id == instance_id for n in notifiers):
            instance_id = f"onebot-{suffix}"
            suffix += 1
        values = {
            "notifier_id": instance_id,
            "provider": "onebot",
            "enabled": True,
            "minimum_urgency": "important",
            "options": {"http_url": base_url},
        }
        await self._service.add_config_entry("notifiers", values)
        self.query_one("#bots-status", Static).update(
            f"[green]{self._service.t('tui.bots_configured_ok', instance=instance_id)}[/green]"
        )
        self.refresh_data()

    async def _delete_selected(self) -> None:
        if getattr(self, "_selected_id", None) is None:
            self.query_one("#bots-status", Static).update(self._service.t("tui.repos_pick_first"))
            return
        notifiers = self._service.config.notifiers
        index = next(
            (i for i, n in enumerate(notifiers) if n.notifier_id == self._selected_id),
            None,
        )
        if index is None:
            return
        await self._service.remove_config_entry("notifiers", index)
        self._selected_id = None
        self._ensure_columns()
        self._render()

    async def _check_all(self) -> None:
        status_node = self.query_one("#bots-status", Static)
        status_node.update(self._service.t("tui.loading"))
        instances = self._im_instances()
        # probe every instance concurrently: three down gateways must not
        # cost 3 x timeout of waiting
        results: dict[str, str] = {}
        if instances:
            probes = [
                _BotStatusProbe.probe(provider, options, self._service.t)
                for _, provider, options in instances
            ]
            outcomes = await asyncio.gather(*probes)
            results = {
                instance_id: outcome
                for (instance_id, _, _), outcome in zip(instances, outcomes, strict=True)
            }
        self._render_rows(results)
        status_node.update(self._service.t("tui.bots_checked"))


__all__ = ["BotsPane"]
