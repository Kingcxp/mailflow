"""通知: manage every notifier instance (delivery channels and gateway-backed
chat platforms) with live connection status.

The pane lists all configured notifiers — chat-platform gateways (NapCat /
OpenWeChat) and plain delivery channels (console,
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
from mailflow.errors import error_text
from mailflow.service import ConfigReloadError, MailFlowService
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.markup import escape
from textual.widgets import Button, DataTable, Select, Static

from mailflow_tui.gateway_guide import GatewayGuideModal
from mailflow_tui.labels import (
    error_detail,
    error_message,
    redact_operation_logs,
    relabel_buttons,
    urgency_label,
)
from mailflow_tui.view_state import TableView
from mailflow_tui.widgets import SafeSelect

_URGENCY_ORDER = (Urgency.AD, Urgency.INFO, Urgency.IMPORTANT, Urgency.URGENT)


class NotifierProbe:
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
                    payload = response.json()
                    if payload.get("status") == "failed" or payload.get("retcode", 0) != 0:
                        return str(t("tui.bots_not_logged_in"))
                    payload_data: dict[str, Any] = dict(payload.get("data") or {})
                    nickname = str(payload_data.get("nickname", "?"))
                    user_id = str(payload_data.get("user_id", "?"))
                    return str(t("tui.bots_logged_in_as", name=nickname, uid=user_id))
                return http_status(response.status_code)
            if provider == "whatsapp":
                url = str(options.get("gateway_url", "")).rstrip("/")
                if not url:
                    return str(t("tui.bots_not_configured"))
                async with httpx.AsyncClient(timeout=6.0) as client:
                    response = await client.get(f"{url}/health")
                if response.status_code == 200:
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("logged_in"):
                        return str(t("tui.bots_online"))
                    return str(t("tui.bots_not_logged_in"))
                return http_status(response.status_code)
            if provider == "telegram":
                token = str(options.get("bot_token", "")).strip()
                if not token:
                    return str(t("tui.bots_not_configured"))
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(
                        f"{str(options.get('api_base') or 'https://api.telegram.org').rstrip('/')}/bot{token}/getMe"
                    )
                if response.status_code == 200:
                    payload = response.json()
                    if payload.get("ok") is not True:
                        return str(t("tui.bots_not_logged_in"))
                    result: Any = payload.get("result") if isinstance(payload, dict) else None
                    name = (
                        str((result or {}).get("username") or "")
                        if isinstance(result, dict)
                        else ""
                    )
                    return str(t("tui.bots_logged_in_as", name=name or "bot", uid=name))
                return http_status(response.status_code)
            if provider == "openwechat":
                url = str(options.get("gateway_url", "")).rstrip("/")
                if not url:
                    return str(t("tui.bots_not_configured"))
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(f"{url}/health")
                if response.status_code != 200:
                    return http_status(response.status_code)
                payload = response.json()
                return str(
                    t("tui.bots_online")
                    if payload.get("logged_in") is True
                    else t("tui.bots_not_logged_in")
                )
            if provider == "wechatpadpro":
                url = str(options.get("base_url", "")).rstrip("/")
                if not url:
                    return str(t("tui.bots_not_configured"))
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(url)
                return str(
                    t("chat.connection_unverified")
                    if 200 <= response.status_code < 300
                    else http_status(response.status_code)
                )
            if provider == "console":
                return str(t("tui.bots_online"))
        except Exception as exc:
            return str(t("tui.bots_unreachable", error=error_text(exc, t)))
        return str(t("tui.notifications_no_probe"))


class NotificationsPane(Vertical):
    """通知: manage every notifier instance and check live connection state.

    QR scanning for gateway-backed platforms happens in the bot runtime
    itself (NapCat / OpenWeChat) — the pane verifies the session and
    reports it in the status column.
    """

    IM_PROVIDERS: ClassVar[frozenset[str]] = frozenset(
        {"onebot", "openwechat", "wechatpadpro", "telegram", "whatsapp"}
    )
    _PROBE_INTERVAL = 30.0

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._selected_id: str | None = None
        self._timer: Any = None
        self._syncing_urgency = False
        self._mutating = False
        self._statuses: dict[str, str] = {}

    def _urgency_options(self) -> list[tuple[str, str]]:
        return [
            (urgency_label(self._service, urgency), urgency.value) for urgency in _URGENCY_ORDER
        ]

    def compose(self) -> ComposeResult:
        yield Static(self._service.t("tui.notifications_title"), id="notifications-title")
        yield Static(escape(self._service.t("tui.notifications_help")), id="notifications-help")
        with ScrollableContainer(id="notifications-table-wrap"):
            yield DataTable(id="notifications-table", cursor_type="row")  # pyright: ignore[reportUnknownMemberType]
        with Horizontal(id="notifications-actions"):
            yield Button(self._service.t("tui.btn_add"), id="notif-add", variant="success")
            yield Button(self._service.t("tui.btn_edit"), id="notif-edit", variant="primary")
            yield Button(
                self._service.t("tui.notif_relogin"), id="notif-relogin", variant="warning"
            )
            yield Button(self._service.t("tui.btn_delete"), id="notif-delete", variant="error")
            yield Button(
                self._service.t("tui.notifications_toggle"), id="notif-toggle", variant="primary"
            )
            yield SafeSelect(self._urgency_options(), id="notif-urgency", allow_blank=False)
            yield Button(
                self._service.t("tui.notifications_check"), id="notif-check", variant="primary"
            )
            yield Button(self._service.t("ux.failed_deliveries"), id="notif-failed")
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
        table.add_column(self._service.t("tui.market_provider"), key="provider")
        table.add_column(self._service.t("tui.notifications_enabled"), key="enabled")
        table.add_column(self._service.t("tui.notifications_urgency"), key="urgency")
        table.add_column(self._service.t("tui.bots_targets"), key="targets")
        table.add_column(self._service.t("tui.bots_status"), key="status")

    def _render_rows(self, statuses: dict[str, str] | None = None) -> None:
        if statuses is not None:
            self._statuses.update(statuses)
        statuses = self._statuses
        table: DataTable[Any] = self.query_one("#notifications-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        view = TableView.capture(table, self._selected_id)
        table.clear()  # pyright: ignore[reportUnknownMemberType]
        for entry in self._all_notifiers():
            notifier_id = str(entry["notifier_id"])
            from mailflow.config import NotifierConfig
            from mailflow.notifications import targets_of

            raw_targets = targets_of(
                NotifierConfig(
                    notifier_id=notifier_id,
                    provider=str(entry["provider"]),
                    options=entry["options"],
                )
            )
            targets = ", ".join(str(t) for t in raw_targets) or "-"
            table.add_row(
                notifier_id,
                str(entry["provider"]),
                (
                    self._service.t("tui.notifications_on")
                    if entry["enabled"]
                    else self._service.t("tui.notifications_off")
                ),
                urgency_label(self._service, str(entry["urgency"])),
                escape(targets),
                Text(error_detail(self._service, statuses.get(notifier_id, "-"))),
                key=notifier_id,
            )
        # re-rendering clears the table and drops the cursor: restore the
        # previously selected row so in-place actions keep targeting it
        self._selected_id = view.restore(table)
        if table.row_count == 0:
            self.query_one("#notifications-status", Static).update(self._service.t("tui.empty"))

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
        relabel_buttons(
            self,
            {
                "notif-add": "tui.btn_add",
                "notif-edit": "tui.btn_edit",
                "notif-delete": "tui.btn_delete",
                "notif-relogin": "tui.notif_relogin",
                "notif-toggle": "tui.notifications_toggle",
                "notif-check": "tui.notifications_check",
            },
        )
        self.query_one("#notifications-title", Static).update(
            self._service.t("tui.notifications_title")
        )
        self.query_one("#notifications-help", Static).update(
            escape(self._service.t("tui.notifications_help"))
        )
        table: DataTable[Any] = self.query_one("#notifications-table", DataTable)
        view = TableView.capture(table, self._selected_id)
        table.clear(columns=True)  # pyright: ignore[reportUnknownMemberType]
        self._ensure_columns()
        self._render_rows()
        self._selected_id = view.restore(table)
        self._refresh_urgency_options()
        self._sync_selection_controls()

    def _periodic_check(self) -> None:
        if not self.is_mounted:
            return
        self.run_worker(self._check_all(), exclusive=True, group="notif-check", exit_on_error=False)

    def refresh_data(self) -> None:
        self._render_rows()

    def _show_error(self, error: BaseException | str) -> None:
        """Keep backend failure details localized, escaped, and credential-safe."""
        self.query_one("#notifications-status", Static).update(
            f"[red]{escape(error_message(self._service, error))}[/red]"
        )

    def on_data_table_row_highlighted(self, event: Any) -> None:
        self._selected_id = str(event.row_key.value)
        self._sync_selection_controls()

    async def on_data_table_row_selected(self, event: Any) -> None:
        self._selected_id = str(event.row_key.value)
        self._sync_selection_controls()
        from mailflow_tui.settings import _double_clicked

        if _double_clicked("notifiers", self._selected_id):
            await self._edit_selected()

    def _refresh_urgency_options(self) -> None:
        urgency = self.query_one_optional("#notif-urgency", Select)
        if urgency is None:
            return
        current = urgency.value
        # a pane remount can tear the Select down between the mount check and
        # the poke; its watcher's internal '#label' query would then raise
        # NoMatches and kill the app — swallow exactly that race
        from textual.css.query import QueryError

        try:
            urgency.set_options(self._urgency_options())  # pyright: ignore[reportUnknownMemberType]
            urgency.value = current  # pyright: ignore[reportUnknownMemberType]
        except QueryError:
            pass

    def _sync_selection_controls(self) -> None:
        """Mirror the selected row's enabled/urgency into the action controls."""
        index = self._selected_index()
        for ident in ("notif-edit", "notif-delete", "notif-relogin"):
            button = self.query_one_optional("#" + ident, Button)
            if button is not None:
                button.disabled = index is None or self._mutating
                if ident == "notif-relogin" and index is not None:
                    entry = self._service.config.notifiers[index]
                    button.disabled = button.disabled or (
                        not entry.options.get("gateway")
                        and self._gateway_for(self._service, entry.provider) is None
                    )
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
            toggle.disabled = self._mutating
            toggle.label = (
                self._service.t("tui.notifications_disable")
                if entry.enabled
                else self._service.t("tui.notifications_enable")
            )
        if urgency is not None:
            urgency.disabled = self._mutating
            from textual.css.query import QueryError

            self._syncing_urgency = True
            try:
                urgency.value = entry.minimum_urgency.value  # pyright: ignore[reportUnknownMemberType]
            except QueryError:
                pass
            finally:
                self._syncing_urgency = False

    # -- actions ----------------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.disabled or self._mutating:
            return
        self._mutating = True
        self._sync_selection_controls()
        try:
            await self._button_action(event)
        finally:
            self._mutating = False
            if self.is_mounted:
                self._sync_selection_controls()

    async def _button_action(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "notif-failed":
            from mailflow_tui.mail_workflows import DeliveriesScreen

            self.app.push_screen(DeliveriesScreen(self._service))
            return
        if button_id == "notif-add":
            from mailflow_tui.settings import EntryFormScreen

            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                EntryFormScreen(
                    self._service,
                    "notifiers",
                    on_save=lambda values: self._submit_form(None, values),
                ),
                callback=self._form_closed,
            )
            return
        if button_id == "notif-edit":
            await self._edit_selected()
            return
        if button_id == "notif-relogin":
            await self._relogin_selected()
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
        if index is None or urgency is None or self._mutating:
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
        self._mutating = True
        self._sync_selection_controls()
        try:
            await self._service.update_config_entry("notifiers", index, values)
        except Exception as exc:
            self._show_error(exc)
            return
        finally:
            self._mutating = False
            self._sync_selection_controls()
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
            self._show_error(exc)
            return
        self._render_rows()
        self._sync_selection_controls()

    async def _relogin_selected(self) -> None:
        """Re-run the guided setup for an existing gateway-backed notifier
        (NapCat logged out / session expired): restarts the gateway
        process and shows the QR again, then re-saves the endpoint."""
        if self._selected_id is None:
            self.query_one("#notifications-status", Static).update(
                f"[red]{self._service.t('tui.notifications_select_first')}[/red]"
            )
            return
        found = [n for n in self._service.config.notifiers if n.notifier_id == self._selected_id]
        if not found:
            return
        entry = found[0]
        provider = str(entry.provider or "")
        # onebot notifiers ride a gateway recorded in options.gateway
        # (napcat/openwechat/...); the provider id itself ('onebot') is
        # the bot protocol, not a gateway
        gateway = str((entry.options or {}).get("gateway") or "")
        if gateway not in self._service.gateway_providers():
            gateway = self._gateway_for(self._service, provider) or ""
        if gateway not in self._service.gateway_providers():
            self.query_one("#notifications-status", Static).update(
                f"[yellow]{self._service.t('tui.notif_relogin_manual')}[/yellow]"
            )
            return
        instance_id = entry.notifier_id
        options = dict(entry.options or {})
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            GatewayGuideModal(self._service, gateway, instance_id, options),
            callback=lambda result, values={"notifier_id": instance_id, "provider": provider, "options": options}: (
                self._after_guide(gateway, instance_id, result, values)
            ),
        )

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
        index = self._service.config.notifiers.index(found[0])
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            EntryFormScreen(
                self._service,
                "notifiers",
                values=values,
                on_save=lambda result: self._submit_form(index, result),
            ),
            callback=self._form_closed,
        )

    def _form_closed(self, result: dict[str, Any] | None) -> None:
        if result is None:
            self.query_one("#notifications-status", Static).update(
                self._service.t("tui.form_cancelled")
            )

    async def _submit_form(self, index: int | None, values: dict[str, Any]) -> bool:
        values = dict(values)
        guided = values.pop("_guided", False)
        provider = str(values.get("provider") or "")
        gateway = self._gateway_for(self._service, provider) if guided else None
        instance_id = str(values.get("notifier_id") or "")
        if gateway is not None:
            result = await self.app.push_screen_wait(
                GatewayGuideModal(
                    self._service, gateway, instance_id, dict(values.get("options") or {})
                )
            )
            if result is None:
                return False
            endpoint_key = {
                "napcat": "http_url",
                "wechatpadpro": "base_url",
                "telegram": "api_base",
            }.get(gateway, "gateway_url")
            values["options"] = {
                **dict(values.get("options") or {}),
                endpoint_key: result.get("endpoint", ""),
                "gateway": gateway,
            }
            values["provider"] = "onebot" if gateway == "napcat" else gateway
        elif provider == "napcat":
            values["provider"] = "onebot"
        warning = ""
        try:
            if index is None:
                await self._service.add_config_entry("notifiers", values)
            else:
                await self._service.update_config_entry("notifiers", index, values)
        except ConfigReloadError as exc:
            warning = error_detail(self._service, exc)
        except Exception:
            if gateway is not None:
                await self._service.gateway_shutdown(gateway, instance_id)
            raise
        self.refresh_data()
        self.query_one("#notifications-status", Static).update(
            escape(
                warning or self._service.t("tui.notifications_configured_ok", instance=instance_id)
            )
        )
        return True

    @staticmethod
    def _gateway_for(service: MailFlowService, provider: str) -> str | None:
        """The gateway provisioner id backing a notifier provider
        (napcat). None when manual-only."""
        if provider in service.gateway_providers():
            return provider
        return None

    @staticmethod
    def _gateway_from_options(options: dict[str, Any]) -> str | None:
        gateway = options.get("gateway")
        return str(gateway) if gateway else None

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
        elif provider == "wechatpadpro":
            options["base_url"] = endpoint
        elif provider == "telegram":
            # Telegram talks to the public Bot API: there is no local
            # endpoint to store, and the bridge runs in-process, so the
            # only settings are the token the form collected plus admins.
            options["api_base"] = endpoint
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
            self._show_error(exc)
            return
        self.refresh_data()
        self.query_one("#notifications-status", Static).update(
            f"[green]{self._service.t('tui.notifications_configured_ok', instance=instance_id)}[/green]"
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
                message = self._service.t(
                    "tui.bots_stop_failed",
                    instance=entry.notifier_id,
                    error=escape(error_detail(self._service, exc)),
                )
                self.query_one("#notifications-status", Static).update(f"[red]{message}[/red]")
                return
        warning = ""
        try:
            await self._service.remove_config_entry("notifiers", index)
        except ConfigReloadError as exc:
            warning = error_detail(self._service, exc)
        except Exception as exc:
            self._show_error(exc)
            return
        self._selected_id = None
        self._render_rows()
        self._sync_selection_controls()
        self.query_one("#notifications-status", Static).update(
            escape(
                warning
                or self._service.t(
                    "tui.entry_removed", group=self._service.t("tui.tab_notifications")
                )
            )
        )

    async def _check_all(self) -> None:
        with redact_operation_logs(self._service):
            await self._check_connections()

    async def _check_connections(self) -> None:
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
                    from mailflow.domain import ComponentKind

                    probe = self._service.registry.probe(ComponentKind.NOTIFIER, provider)
                    try:
                        if probe is not None:
                            return await probe(options, self._service.t)
                        return await NotifierProbe.probe(provider, options, self._service.t)
                    except Exception as exc:
                        return error_message(self._service, exc)

            probes = [_bounded(provider, options) for _, provider, options in instances]
            outcomes = await asyncio.gather(*probes)
            results = {
                instance_id: outcome
                for (instance_id, _, _), outcome in zip(instances, outcomes, strict=True)
            }
        # gateway-backed notifiers: surface managed-gateway failures (e.g.
        # the gateway's data dir was deleted while the app was stopped) as
        # an explicit "re-run setup" hint instead of a generic unreachable
        await self._merge_gateway_states(results)
        self._render_rows(results)
        status_node.update(self._service.t("tui.notifications_checked"))

    async def _merge_gateway_states(self, results: dict[str, str]) -> None:
        """Overlay managed-gateway errors onto notifier status rows.

        A notifier whose gateway instance is in ``error`` (missing
        install, deleted data dir, ...) cannot deliver or answer chat
        commands; replacing the probe line with the reconfig hint makes
        the required action obvious."""
        try:
            instances = await self._service.gateway_instances()
        except Exception:
            return
        gateway_by_id: dict[str, str] = {}
        for instance in instances:
            status = str(getattr(instance, "status", ""))
            if status == "error":
                error = error_detail(
                    self._service, RuntimeError(getattr(instance, "error", "") or "")
                )
                gateway_by_id[str(instance.instance_id)] = (
                    f"{self._service.t('chat.connection_error')} ({error})"
                )
        if not gateway_by_id:
            return
        providers = self._service.gateway_providers()  # pyright: ignore[reportUnknownMemberType]
        for entry in self._all_notifiers():
            notifier_id = str(entry["notifier_id"])
            provider = str(entry["provider"])
            gateway = (
                str(entry["options"].get("gateway") or "")
                if entry["options"].get("gateway")
                else (provider if provider in providers else "")
            )
            if not gateway:
                continue
            hint = gateway_by_id.get(notifier_id)
            if hint:
                results[notifier_id] = hint


__all__ = ["NotificationsPane"]
