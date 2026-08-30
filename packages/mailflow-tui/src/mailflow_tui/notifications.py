"""通知: manage every notifier instance (delivery channels and gateway-backed
chat platforms) with live connection status.

The pane lists all configured notifiers — chat-platform gateways (NapCat /
WeChaty / OpenWeChat / OpenClaw) and plain delivery channels (console,
telegram, webhook, ntfy, smtp, ...). From here the user can add / edit /
delete instances, toggle them enabled, set the delivery urgency threshold,
probe connections and see live status. Gateway-backed providers route
"Add" through the guided setup (install + QR login in the TUI).

Thin adapter over externally-typed frameworks; checked in basic mode."""

# pyright: basic

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import httpx
from mailflow.domain import Urgency, parse_urgency
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.markup import escape
from textual.widgets import Button, DataTable, Select, Static

from mailflow_tui.gateway_guide import GatewayGuideModal

_URGENCY_ORDER = (Urgency.AD, Urgency.INFO, Urgency.IMPORTANT, Urgency.URGENT)


class _NotifierProbe:
    """Connectivity probes for notifier backends. Each returns a
    human-readable status line; providers without a probe report that
    honestly instead of pretending to be online."""

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
                    return str(t("tui.bots_not_configured"))
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
                    return str(t("tui.bots_not_configured"))
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(url)
                return (
                    t("tui.bots_gateway_reachable")
                    if response.status_code < 500
                    else http_status(response.status_code)
                )
            if provider == "openwechat":
                url = str(options.get("gateway_url", "")).rstrip("/")
                if not url:
                    return str(t("tui.bots_not_configured"))
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(f"{url}/health")
                return str(
                    t("tui.bots_online")
                    if response.status_code == 200
                    else http_status(response.status_code)
                )
            if provider == "console":
                return str(t("tui.bots_online"))
        except Exception as exc:
            return str(t("tui.bots_unreachable", error=type(exc).__name__))
        return str(t("tui.notifications_no_probe"))


class NotificationsPane(Vertical):
    """通知: manage every notifier instance and check live connection state.

    QR scanning for gateway-backed platforms happens in the bot runtime
    itself (NapCat / WeChaty gateway / OpenWeChat) — the pane verifies the
    session and reports it in the status column.
    """

    IM_PROVIDERS: ClassVar[frozenset[str]] = frozenset(
        {"onebot", "wechaty", "openwechat", "openclaw-weixin"}
    )
    _PROBE_INTERVAL = 30.0

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._selected_id: str | None = None
        self._timer: Any = None
        self._syncing_urgency = False

    def compose(self) -> ComposeResult:
        yield Static(self._service.t("tui.notifications_title"), id="notifications-title")
        yield Static(escape(self._service.t("tui.notifications_help")), id="notifications-help")
        with ScrollableContainer(id="notifications-table-wrap"):
            yield DataTable(id="notifications-table", cursor_type="row")  # pyright: ignore[reportUnknownMemberType]
        with Horizontal(id="notifications-actions"):
            yield Button(self._service.t("tui.btn_add"), id="notif-add", variant="success")
            yield Button(self._service.t("tui.btn_edit"), id="notif-edit", variant="primary")
            yield Button(self._service.t("tui.btn_delete"), id="notif-delete", variant="error")
            yield Button(
                self._service.t("tui.notifications_toggle"), id="notif-toggle", variant="primary"
            )
            yield Select(
                [(u.value, u.value) for u in _URGENCY_ORDER],
                id="notif-urgency",
                allow_blank=False,
            )
            yield Button(
                self._service.t("tui.notifications_check"), id="notif-check", variant="primary"
            )
        yield Static("", id="notifications-status")

    # -- data -----------------------------------------------------------------

    def _all_notifiers(self) -> list[dict[str, Any]]:
        """Every configured notifier (all providers, not just IM)."""
        out: list[dict[str, Any]] = []
        for notifier in self._service.config.notifiers:
            out.append(
                {
                    "notifier_id": notifier.notifier_id,
                    "provider": notifier.provider,
                    "enabled": bool(notifier.enabled),
                    "urgency": notifier.minimum_urgency.value,
                    "options": dict(notifier.options),
                }
            )
        return out

    def _selected_index(self) -> int | None:
        if self._selected_id is None:
            return None
        return next(
            (
                i
                for i, n in enumerate(self._service.config.notifiers)
                if n.notifier_id == self._selected_id
            ),
            None,
        )

    # -- table ----------------------------------------------------------------

    def _ensure_columns(self) -> None:
        table: DataTable[Any] = self.query_one("#notifications-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        if table.ordered_columns:  # pyright: ignore[reportUnknownMemberType]
            return
        table.add_column(self._service.t("plugin.header_name"), key="name")
        table.add_column(self._service.t("tui.market_provider", default="provider"), key="provider")
        table.add_column(self._service.t("tui.notifications_enabled"), key="enabled")
        table.add_column(self._service.t("tui.notifications_urgency"), key="urgency")
        table.add_column(self._service.t("tui.bots_targets"), key="targets")
        table.add_column(self._service.t("tui.bots_status"), key="status")

    def _render_rows(self, statuses: dict[str, str] | None = None) -> None:
        statuses = statuses or {}
        table: DataTable[Any] = self.query_one("#notifications-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        selected_key = self._selected_id
        table.clear()  # pyright: ignore[reportUnknownMemberType]
        for entry in self._all_notifiers():
            notifier_id = str(entry["notifier_id"])
            raw_targets = list(entry["options"].get("targets") or [])
            targets = ", ".join(str(t) for t in raw_targets) or "-"
            table.add_row(
                notifier_id,
                str(entry["provider"]),
                (
                    self._service.t("tui.notifications_on")
                    if entry["enabled"]
                    else self._service.t("tui.notifications_off")
                ),
                str(entry["urgency"]),
                escape(targets),
                statuses.get(notifier_id, "-"),
                key=notifier_id,
            )
        # re-rendering clears the table and drops the cursor: restore the
        # previously selected row so in-place actions keep targeting it
        if selected_key is not None:
            for index, key in enumerate(table.rows):  # pyright: ignore[reportUnknownMemberType]
                if str(getattr(key, "value", "")) == selected_key:
                    table.move_cursor(row=index, animate=False)  # pyright: ignore[reportUnknownMemberType]
                    break

    def on_mount(self) -> None:
        self._ensure_columns()
        self._render_rows()
        self._refresh_urgency_options()
        self._sync_selection_controls()
        # auto-connect every enabled notifier on startup (bounded,
        # non-blocking); failures render as "offline: <reason>" in the
        # status column
        self.run_worker(self._check_all(), exclusive=True, group="notif-check", exit_on_error=False)
        self._timer = self.set_interval(self._PROBE_INTERVAL, self._periodic_check)

    async def relabel(self) -> None:
        table: DataTable[Any] = self.query_one("#notifications-table", DataTable)
        table.clear(columns=True)  # pyright: ignore[reportUnknownMemberType]
        self._ensure_columns()
        self._render_rows()
        self._refresh_urgency_options()
        self._sync_selection_controls()

    def _periodic_check(self) -> None:
        if not self.is_mounted:
            return
        self.run_worker(self._check_all(), exclusive=True, group="notif-check", exit_on_error=False)

    def refresh_data(self) -> None:
        self._render_rows()

    def on_data_table_row_highlighted(self, event: Any) -> None:
        self._selected_id = str(event.row_key.value)
        self._sync_selection_controls()

    def _refresh_urgency_options(self) -> None:
        urgency = self.query_one_optional("#notif-urgency", Select)
        if urgency is None:
            return
        current = urgency.value
        urgency.set_options(  # pyright: ignore[reportUnknownMemberType]
            [(u.value, u.value) for u in _URGENCY_ORDER]
        )
        urgency.value = current  # pyright: ignore[reportUnknownMemberType]

    def _sync_selection_controls(self) -> None:
        """Mirror the selected row's enabled/urgency into the action controls."""
        index = self._selected_index()
        toggle = self.query_one_optional("#notif-toggle", Button)
        urgency = self.query_one_optional("#notif-urgency", Select)
        if index is None:
            if toggle is not None:
                toggle.disabled = True
            if urgency is not None:
                urgency.disabled = True
            return
        entry = self._service.config.notifiers[index]
        if toggle is not None:
            toggle.disabled = False
            toggle.label = (
                self._service.t("tui.notifications_disable")
                if entry.enabled
                else self._service.t("tui.notifications_enable")
            )
        if urgency is not None:
            urgency.disabled = False
            self._syncing_urgency = True
            urgency.value = entry.minimum_urgency.value  # pyright: ignore[reportUnknownMemberType]
            self._syncing_urgency = False

    # -- actions ----------------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "notif-add":
            from mailflow_tui.settings import EntryFormScreen

            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                EntryFormScreen(self._service, "notifiers"),
                callback=self._after_form,
            )
            return
        if button_id == "notif-edit":
            await self._edit_selected()
            return
        if button_id == "notif-delete":
            await self._delete_selected()
            return
        if button_id == "notif-toggle":
            await self._toggle_selected()
            return
        if button_id == "notif-check":
            self.run_worker(
                self._check_all(), exclusive=True, group="notif-check", exit_on_error=False
            )

    async def on_select_changed(self, event: Any) -> None:
        if getattr(event.select, "id", None) != "notif-urgency":
            return
        if self._syncing_urgency:
            return
        await self._set_selected_urgency()

    async def _set_selected_urgency(self) -> None:
        index = self._selected_index()
        urgency = self.query_one_optional("#notif-urgency", Select)
        if index is None or urgency is None:
            return
        selected = urgency.value
        if selected is Select.NULL or selected == "":
            return
        parsed = parse_urgency(str(selected))
        entry = self._service.config.notifiers[index]
        if entry.minimum_urgency == parsed:
            # unchanged (e.g. the programmatic re-sync after a re-render):
            # break the loop instead of persisting + re-rendering again
            return
        values = entry.model_dump()
        values["minimum_urgency"] = parsed.value
        try:
            await self._service.update_config_entry("notifiers", index, values)
        except Exception as exc:
            self.query_one("#notifications-status", Static).update(f"[red]{exc}[/red]")
            return
        self._render_rows()

    async def _toggle_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            self.query_one("#notifications-status", Static).update(
                f"[red]{self._service.t('tui.notifications_select_first')}[/red]"
            )
            return
        entry = self._service.config.notifiers[index]
        values = entry.model_dump()
        values["enabled"] = not entry.enabled
        try:
            await self._service.update_config_entry("notifiers", index, values)
        except Exception as exc:
            self.query_one("#notifications-status", Static).update(f"[red]{exc}[/red]")
            return
        self._render_rows()
        self._sync_selection_controls()

    async def _edit_selected(self) -> None:
        if self._selected_id is None:
            self.query_one("#notifications-status", Static).update(
                f"[red]{self._service.t('tui.notifications_select_first')}[/red]"
            )
            return
        found = [n for n in self._service.config.notifiers if n.notifier_id == self._selected_id]
        if not found:
            return
        from mailflow_tui.settings import EntryFormScreen

        values = found[0].model_dump()
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            EntryFormScreen(self._service, "notifiers", values=values),
            callback=self._after_edit,
        )

    def _after_edit(self, result: dict[str, Any] | None) -> None:
        if not result:
            return
        notifier_id = str(result.get("notifier_id") or self._selected_id or "")
        index = next(
            (
                i
                for i, n in enumerate(self._service.config.notifiers)
                if n.notifier_id == notifier_id
            ),
            None,
        )
        if index is None:
            return
        self.run_worker(
            self._save_edited_notifier(index, notifier_id, result),
            exclusive=True,
            group="notif-setup",
            exit_on_error=False,
        )

    async def _save_edited_notifier(
        self, index: int, notifier_id: str, values: dict[str, Any]
    ) -> None:
        try:
            await self._service.update_config_entry("notifiers", index, values)
        except Exception as exc:
            self.query_one("#notifications-status", Static).update(f"[red]{exc}[/red]")
            return
        self.refresh_data()
        self.query_one("#notifications-status", Static).update(
            f"[green]{self._service.t('tui.notifications_configured_ok', instance=notifier_id)}[/green]"
        )

    @staticmethod
    def _gateway_for(service: MailFlowService, provider: str) -> str | None:
        """The gateway provisioner id backing a notifier provider
        (napcat, wechaty map 1:1). None when manual-only."""
        if provider in service.gateway_providers():
            return provider
        return None

    @staticmethod
    def _gateway_from_options(options: dict[str, Any]) -> str | None:
        gateway = options.get("gateway")
        return str(gateway) if gateway else None

    def _after_form(self, values: dict[str, Any] | None) -> None:
        """Form dismissed → persist, or run the guided gateway setup."""
        if not values:
            return
        guided = values.pop("_guided", False)
        provider = str(values.get("provider") or "")
        gateway = self._gateway_for(self._service, provider)
        if guided and gateway is not None:
            instance_id = str(values.get("notifier_id") or f"{gateway}-1")
            options = dict(values.get("options") or {})
            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                GatewayGuideModal(self._service, gateway, instance_id, options),
                callback=lambda result, form_values=values: self._after_guide(
                    gateway, instance_id, result, form_values
                ),
            )
            return
        self.run_worker(
            self._save_manual_notifier(values),
            exclusive=True,
            group="notif-setup",
            exit_on_error=False,
        )

    def _after_guide(
        self,
        provider: str,
        instance_id: str,
        result: dict[str, Any] | None,
        form_values: dict[str, Any] | None = None,
    ) -> None:
        if not result:
            return
        self.run_worker(
            self._save_guided_notifier(provider, instance_id, result, form_values),
            exclusive=True,
            group="notif-setup",
            exit_on_error=False,
        )

    async def _save_guided_notifier(
        self,
        provider: str,
        instance_id: str,
        result: dict[str, Any],
        form_values: dict[str, Any] | None = None,
    ) -> None:
        """Persist the notifier config for a provisioned gateway."""
        endpoint = str(result.get("endpoint") or "")
        options: dict[str, Any] = {}
        if provider == "napcat":
            options["http_url"] = endpoint
        else:
            options["gateway_url"] = endpoint
        form_opts = dict(form_values.get("options") or {}) if form_values else {}
        merged_options = {**form_opts, **options, "gateway": provider}
        values = {
            "notifier_id": instance_id,
            "provider": "onebot" if provider == "napcat" else provider,
            "options": merged_options,
        }
        try:
            existing = [
                (i, n)
                for i, n in enumerate(self._service.config.notifiers)
                if n.notifier_id == instance_id
            ]
            if existing:
                await self._service.update_config_entry("notifiers", existing[0][0], values)
            else:
                await self._service.add_config_entry("notifiers", values)
        except Exception as exc:
            self.query_one("#notifications-status", Static).update(f"[red]{exc}[/red]")
            return
        self.refresh_data()
        self.query_one("#notifications-status", Static).update(
            f"[green]{self._service.t('tui.notifications_configured_ok', instance=instance_id)}[/green]"
        )

    async def _save_manual_notifier(self, values: dict[str, Any]) -> None:
        try:
            await self._service.add_config_entry("notifiers", values)
        except Exception as exc:
            self.query_one("#notifications-status", Static).update(f"[red]{exc}[/red]")
            return
        self.refresh_data()
        self.query_one("#notifications-status", Static).update(
            f"[green]{self._service.t('tui.notifications_saved_manual')}[/green]"
        )

    async def _delete_selected(self) -> None:
        if self._selected_id is None:
            self.query_one("#notifications-status", Static).update(
                f"[red]{self._service.t('tui.notifications_select_first')}[/red]"
            )
            return
        index = self._selected_index()
        if index is None:
            return
        entry = self._service.config.notifiers[index]
        gateway = self._gateway_from_options(dict(entry.options))
        if entry.provider in self._service.gateway_providers():
            gateway = gateway or entry.provider
        if gateway:
            try:
                await self._service.gateway_shutdown(gateway, entry.notifier_id)
            except Exception as exc:
                self.query_one("#notifications-status", Static).update(
                    f"[red]{self._service.t('tui.bots_stop_failed', instance=entry.notifier_id, error=str(exc))}[/red]"
                )
                return
        await self._service.remove_config_entry("notifiers", index)
        self._selected_id = None
        self._render_rows()
        self._sync_selection_controls()

    async def _check_all(self) -> None:
        status_node = self.query_one("#notifications-status", Static)
        status_node.update(self._service.t("tui.loading"))
        instances = [
            (str(e["notifier_id"]), str(e["provider"]), e["options"])
            for e in self._all_notifiers()
            if e["enabled"]
        ]
        results: dict[str, str] = {}
        if instances:
            sem = asyncio.Semaphore(4)

            async def _bounded(provider: str, options: dict[str, Any]) -> str:
                async with sem:
                    return await _NotifierProbe.probe(provider, options, self._service.t)

            probes = [_bounded(provider, options) for _, provider, options in instances]
            outcomes = await asyncio.gather(*probes)
            results = {
                instance_id: outcome
                for (instance_id, _, _), outcome in zip(instances, outcomes, strict=True)
            }
        self._render_rows(results)
        status_node.update(self._service.t("tui.notifications_checked"))


__all__ = ["NotificationsPane"]
