"""平台登录: IM bot instance management and login-state probes.

Thin adapter over externally-typed frameworks; checked in basic mode."""

# pyright: basic

from __future__ import annotations

from typing import Any, ClassVar

import httpx
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.markup import escape
from textual.widgets import Button, DataTable, Static


class _BotStatusProbe:
    """Connectivity probes for IM bot backends (OneBot v11 / WeChaty /
    OpenClaw). Each returns a human-readable status line."""

    @staticmethod
    async def probe(provider: str, options: dict[str, Any]) -> str:

        try:
            if provider == "onebot":
                url = str(options.get("http_url", "")).rstrip("/")
                if not url:
                    return "not configured"
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
                    return f"logged in as {nickname} ({user_id})"
                return f"HTTP {response.status_code}"
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
                return "online" if response.status_code == 200 else f"HTTP {response.status_code}"
            if provider == "openclaw-weixin":
                url = str(options.get("base_url", "")).rstrip("/")
                if not url:
                    return "not configured"
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(url)
                return (
                    "gateway reachable"
                    if response.status_code < 500
                    else f"HTTP {response.status_code}"
                )
        except Exception as exc:
            return f"{type(exc).__name__}: unreachable"
        return "unknown provider"


class BotsPane(Vertical):
    """平台登录: manage IM bot instances (OneBot/WeChaty/OpenClaw) and
    check their login state. QR scanning happens in the bot runtime itself
    (NapCat / WeChaty gateway / OpenClaw) — this tab verifies the session."""

    IM_PROVIDERS: ClassVar[frozenset[str]] = frozenset({"onebot", "wechaty", "openclaw-weixin"})

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service

    def compose(self) -> ComposeResult:
        yield Static(self._service.t("tui.bots_title"), id="bots-title")
        yield DataTable(id="bots-table")
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

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "bots-check":
            return
        status_node = self.query_one("#bots-status", Static)
        status_node.update(self._service.t("tui.loading"))
        results: dict[str, str] = {}
        for notifier_id, provider, options in self._im_instances():
            results[notifier_id] = await _BotStatusProbe.probe(provider, options)
        self._render_rows(results)
        status_node.update(self._service.t("tui.bots_checked"))


__all__ = ["BotsPane"]
