"""TUI runner: compose the bundled manager, start one service, attach a TUI
log handler, run the Textual app on the same event loop, stop the service.

Three launch shapes:
- default          — local service, no admin server
- ``--local``      — local service plus the embedded admin REST+WS server
                     (credentials auto-provisioned) so other frontends can
                     attach; the TUI itself stays logged in automatically
- ``--remote URL`` — no local service: login against a remote host
                     (address/username remembered, optional saved password,
                     auto-login until it fails), drive it over REST+WS
"""

from __future__ import annotations

import asyncio
import logging
import queue as queue_module
import secrets
from pathlib import Path
from typing import Any

from mailflow.commands import CommandRouter
from mailflow.config import MailFlowConfig, load_config
from mailflow.service import start_service
from mailflow_bundled import create_plugin_manager

logger = logging.getLogger("mailflow.tui")


class TuiLogHandler(logging.Handler):
    """Routes mailflow log records into a queue the TUI drains on its timer.

    Lines carry a ``HH:MM:SS LEVEL`` prefix so the Logs tab can colorize by
    severity while staying plain text (no ANSI/markup escapes)."""

    def __init__(self, log_queue: queue_module.Queue[Any]) -> None:
        super().__init__()
        self.setFormatter(
            logging.Formatter("%(asctime)s|%(levelname)s|%(name)s|%(message)s", "%H:%M:%S")
        )
        self._log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._log_queue.put_nowait(self.format(record))
        except Exception:
            self.handleError(record)


def tui_logging_config(config: MailFlowConfig) -> MailFlowConfig:
    """The Textual app owns the terminal: console logs must not be written to
    stdout while the TUI is rendering (they would corrupt the screen). File,
    JSONL and the injected Logs-tab handler still receive every record."""
    adjusted = config.model_copy(deep=True)
    adjusted.logging.console = False
    return adjusted


async def _start_embedded_server(
    service: Any, config: MailFlowConfig, log_queue: queue_module.Queue[Any]
) -> tuple[Any, Any]:
    """Run the admin server alongside a --local TUI; returns (server, task)."""
    import uvicorn
    from mailflow_server import create_app

    if not config.server.username:
        config.server.username = "local"
    if not config.server.password and not config.server.password_env:
        # fixed-for-this-session credential so remote clients can connect
        # without weakening any persisted configuration
        config.server.password = secrets.token_urlsafe(12)
        log_queue.put(
            f"admin server: user={config.server.username} password={config.server.password}"
        )
    app = create_app(service)
    uv_config = uvicorn.Config(
        app, host=config.server.host, port=config.server.port, log_level="warning"
    )
    server = uvicorn.Server(uv_config)
    task = asyncio.create_task(server.serve(), name="embedded-admin-server")
    log_queue.put(f"admin server listening on http://{config.server.host}:{config.server.port}")
    return server, task


def ensure_default_config(config_path: str | None) -> str | None:
    """Bootstrap a missing config file from the bundled example.

    ``make tui`` points at ``configs/development.toml``, which is
    per-developer and therefore not in version control — a fresh clone would
    start with nothing. When the requested file does not exist but
    ``configs/example.toml`` does, copy it so first launch has a starting
    point instead of pure defaults.
    """
    if not config_path:
        return config_path
    path = Path(config_path)
    if path.exists():
        return config_path
    example = Path("configs/example.toml")
    if not example.exists():
        return config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    example_content = example.read_text(encoding="utf-8")
    header = (
        "# Bootstrapped from configs/example.toml by 'mailflow tui' —\n"
        "# edit freely; this per-developer file is gitignored.\n"
    )
    path.write_text(header + example_content, encoding="utf-8")
    logger.info("bootstrapped %s from configs/example.toml", config_path)
    return config_path


async def _run_local(config_path: str | None, *, with_server: bool) -> None:
    config_path = ensure_default_config(config_path)
    config = tui_logging_config(load_config(config_path) if config_path else MailFlowConfig())
    manager = create_plugin_manager(config, discover_external=False)
    log_queue: queue_module.Queue[Any] = queue_module.Queue()
    service = await start_service(
        config,
        config_path=config_path,
        plugin_manager=manager,
        discover_plugins=False,
        extra_log_handlers=[TuiLogHandler(log_queue)],
    )
    CommandRouter(service)
    server = None
    server_task = None
    stopped = False
    try:
        if with_server:
            server, server_task = await _start_embedded_server(service, config, log_queue)
        from mailflow_tui.app import MailFlowApp

        app = MailFlowApp(service, log_queue, splash=True)
        try:
            await app.run_async()
        finally:
            if server is not None:
                server.should_exit = True
            if server_task is not None:
                await asyncio.gather(server_task, return_exceptions=True)
            await service.stop()
            stopped = True
    except Exception:
        if not stopped:
            await service.stop()
        raise


async def _run_remote() -> None:
    from mailflow.i18n import I18n
    from mailflow_server.client import RemoteClient
    from textual.app import App as _BaseApp

    from mailflow_tui.app import MailFlowApp
    from mailflow_tui.remote import (
        LoginScreen,
        RemoteServiceAdapter,
        clear_password,
        load_session,
        pump_async_to_thread,
    )

    class _Bootstrap(_BaseApp[dict[str, str] | None]):
        def compose(self) -> Any:
            from textual.containers import Vertical

            with Vertical():
                yield LoginScreen(load_session())

    async def _validate(url: str, user: str, password: str) -> dict[str, Any] | None:
        import httpx

        from mailflow_tui.remote import basic_header

        async with httpx.AsyncClient(base_url=url, timeout=10.0) as probe:
            response = await probe.get("/snapshot", headers=basic_header(user, password))
        if response.status_code != 200:
            return None
        payload: dict[str, Any] = response.json()
        return payload

    snapshot: dict[str, Any] | None = None
    creds: dict[str, str] | None = None
    session = load_session()
    if session.get("autologin") and session.get("saved_password"):
        snapshot = await _validate(
            str(session["url"]), str(session["username"]), str(session["saved_password"])
        )
        if snapshot is None:
            clear_password()
    if snapshot is None:
        bootstrap = _Bootstrap()
        creds = await bootstrap.run_async()
        if creds is None:
            return
        snapshot = await _validate(creds["url"], creds["username"], creds["password"])
        if snapshot is None:
            return

    assert creds is not None or session.get("saved_password")
    final_creds = creds or {
        "url": str(session["url"]),
        "username": str(session["username"]),
        "password": str(session["saved_password"]),
    }
    client = RemoteClient(final_creds["url"], final_creds["username"], final_creds["password"])
    i18n = I18n()
    adapter = RemoteServiceAdapter(client, cast_snapshot(snapshot), i18n)
    log_queue: queue_module.Queue[Any] = queue_module.Queue()
    pump = asyncio.create_task(pump_async_to_thread(client.log_queue, log_queue))
    await client.start_events()
    await client.enable_logs()

    app = MailFlowApp(adapter, log_queue, remote=True, splash=True)
    try:
        await app.run_async()
    finally:
        await client.stop_events()
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


def cast_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return snapshot


def run_tui(config_path: str | None, *, local: bool = False, remote_url: str | None = None) -> None:
    """Entry point used by the CLI and the console script."""

    async def _run() -> None:
        if remote_url:
            await _run_remote_with_override(remote_url)
            return
        await _run_local(config_path, with_server=local)

    asyncio.run(_run())


async def _run_remote_with_override(remote_url: str) -> None:
    """``--remote URL`` pre-seeds the session address, then runs the normal
    login flow (credentials still required)."""
    from mailflow_tui.remote import load_session, save_session

    session = load_session()
    session["url"] = remote_url.rstrip("/")
    save_session(session)
    await _run_remote()


def run_tui_cli() -> None:
    import typer

    typer.run(run_tui)


__all__ = ["run_tui", "tui_logging_config"]
