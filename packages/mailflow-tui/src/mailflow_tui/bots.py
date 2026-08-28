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
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from mailflow_tui.gateway_guide import GatewayGuideModal


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


class BotBasicsModal(ModalScreen[dict[str, Any] | None]):
    """Step 1 of the bot setup: ask for the instance name/id only."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss", "Close")]

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Static(self._t("tui.bots_basics_title"), id="basics-title")
        with Vertical(id="basics-body"):
            yield Label(self._t("tui.bots_basics_id"), classes="field-label")
            yield Input(placeholder="bot-1", id="basics-id")
            yield Static("", id="basics-status")
            with Horizontal(id="basics-actions"):
                yield Button(self._t("tui.bots_basics_next"), id="basics-next", variant="primary")
                yield Button(self._t("tui.btn_cancel"), id="basics-cancel", variant="error")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "basics-cancel":
            self.dismiss(None)
            return
        if event.button.id != "basics-next":
            return
        instance_id = self.query_one("#basics-id", Input).value.strip()
        if not instance_id:
            self.query_one("#basics-status", Static).update(
                f"[yellow]{self._t('tui.bots_basics_id')} required[/yellow]"
            )
            return
        self.dismiss({"instance_id": instance_id})


class BotProviderModal(ModalScreen[dict[str, Any] | None]):
    """Step 2 of the bot setup: pick the platform from the available
    gateway provisioners (plus notifier-only platforms)."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss", "Close")]

    def __init__(self, service: MailFlowService, instance_id: str) -> None:
        super().__init__()
        self._service = service
        self._instance_id = instance_id

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    @staticmethod
    def _provider_label(service: MailFlowService, provider: str) -> str:
        """Localized label for a known provider id; unknown ids stay raw."""
        key = f"tui.bots_provider_{provider}"
        translated = service.t(key)
        return translated if translated != key else provider

    def compose(self) -> ComposeResult:
        providers = self._service.gateway_providers()
        # notifier-only platforms (e.g. openclaw) stay selectable but are
        # configured manually
        from mailflow_tui.bots import BotsPane

        choices = [(self._provider_label(self._service, p), p) for p in providers]
        for extra in sorted(BotsPane.IM_PROVIDERS - set(providers)):
            choices.append((self._provider_label(self._service, extra), extra))
        yield Static(self._t("tui.bots_wizard_pick"), id="provider-title")
        with Vertical(id="provider-body"):
            yield Select(choices, id="provider-select", allow_blank=False)
            yield Static("", id="provider-status")
            with Horizontal(id="provider-actions"):
                yield Button(self._t("tui.btn_next"), id="provider-next", variant="primary")
                yield Button(self._t("tui.btn_cancel"), id="provider-cancel", variant="error")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "provider-cancel":
            self.dismiss(None)
            return
        if event.button.id != "provider-next":
            return
        select = self.query_one("#provider-select", Select)
        if select.value is Select.NULL:
            self.query_one("#provider-status", Static).update(
                f"[yellow]{self._t('tui.bots_wizard_pick_first')}[/yellow]"
            )
            return
        self.dismiss({"provider": str(select.value)})


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
        table.add_column(self._service.t("tui.market_provider", default="provider"), key="provider")
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
            # three-step setup: basics → provider → guided gateway
            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                BotBasicsModal(self._service),
                callback=self._after_basics,
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

    def _after_basics(self, values: dict[str, Any] | None) -> None:
        """Step 1 done → pick the provider."""
        if not values:
            return
        self._pending_instance_id = str(values.get("instance_id") or "bot-1")
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            BotProviderModal(self._service, self._pending_instance_id),
            callback=self._after_provider,
        )

    def _after_provider(self, values: dict[str, Any] | None) -> None:
        """Step 2 done → run the guided gateway setup."""
        if not values:
            return
        provider = str(values.get("provider") or "")
        instance_id = str(getattr(self, "_pending_instance_id", "bot-1"))
        if provider in self._service.gateway_providers():
            # gateway-backed platform: provision + QR, then save the notifier
            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                GatewayGuideModal(self._service, provider, instance_id, {}),
                callback=lambda result: self._after_guide(provider, instance_id, result),
            )
            return
        # notifier-only platform (openclaw): manual form as before
        from mailflow_tui.settings import EntryFormScreen

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            EntryFormScreen(self._service, "notifiers", values={"provider": provider}),
            callback=self._after_manual_form,
        )

    def _after_guide(self, provider: str, instance_id: str, result: dict[str, Any] | None) -> None:
        """Gateway provisioned and logged in → persist the notifier entry."""
        if not result:
            return
        self.run_worker(
            self._save_guided_notifier(provider, instance_id, result),
            exclusive=True,
            group="bots-setup",
            exit_on_error=False,
        )

    async def _save_guided_notifier(
        self, provider: str, instance_id: str, result: dict[str, Any]
    ) -> None:
        """Persist the notifier config for a provisioned gateway."""
        endpoint = str(result.get("endpoint") or "")
        options: dict[str, Any] = {}
        if provider == "napcat":
            options["http_url"] = endpoint
        else:
            options["gateway_url"] = endpoint
        values = {
            "notifier_id": instance_id,
            "provider": "onebot" if provider == "napcat" else provider,
            "options": options,
        }
        try:
            await self._service.add_config_entry("notifiers", values)
        except Exception as exc:
            self.query_one("#bots-status", Static).update(f"[red]{exc}[/red]")
            return
        self.refresh_data()
        self.query_one("#bots-status", Static).update(
            f"[green]{self._service.t('tui.bots_configured_ok', instance=instance_id)}[/green]"
        )

    def _after_manual_form(self, values: dict[str, Any] | None) -> None:
        """Notifier-only platform form dismissed → persist."""
        if not values:
            return
        self.run_worker(
            self._save_manual_notifier(values),
            exclusive=True,
            group="bots-setup",
            exit_on_error=False,
        )

    async def _save_manual_notifier(self, values: dict[str, Any]) -> None:
        try:
            await self._service.add_config_entry("notifiers", values)
        except Exception as exc:
            self.query_one("#bots-status", Static).update(f"[red]{exc}[/red]")
            return
        self.refresh_data()
        self.query_one("#bots-status", Static).update(
            f"[green]{self._service.t('tui.bots_saved_manual')}[/green]"
        )

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
