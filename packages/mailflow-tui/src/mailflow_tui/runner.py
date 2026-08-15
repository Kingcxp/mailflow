"""TUI runner: compose the bundled manager, start one service, attach a TUI
log handler, run the Textual app on the same event loop, stop the service."""

from __future__ import annotations

import asyncio
import logging
import queue as queue_module
from typing import Any

from mailflow.commands import CommandRouter
from mailflow.config import MailFlowConfig, load_config
from mailflow.service import start_service
from mailflow_bundled import create_plugin_manager

logger = logging.getLogger("mailflow.tui")


class TuiLogHandler(logging.Handler):
    """Routes mailflow log records into a queue the TUI drains on its timer."""

    def __init__(self, log_queue: queue_module.Queue[Any]) -> None:
        super().__init__()
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


def run_tui(config_path: str | None) -> None:
    """Entry point used by the CLI and the console script."""

    async def _run() -> None:
        config = tui_logging_config(load_config(config_path) if config_path else MailFlowConfig())
        manager = create_plugin_manager(config, discover_external=False)
        log_queue: queue_module.Queue[Any] = queue_module.Queue()
        service = await start_service(
            config,
            plugin_manager=manager,
            discover_plugins=False,
            extra_log_handlers=[TuiLogHandler(log_queue)],
        )
        CommandRouter(service)
        from mailflow_tui.app import MailFlowApp

        app = MailFlowApp(service, log_queue)
        try:
            await app.run_async()
        finally:
            await service.stop()

    asyncio.run(_run())


def run_tui_cli() -> None:
    import typer

    typer.run(run_tui)
