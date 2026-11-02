"""Headless UI fixtures with fake external mail and model services."""

from __future__ import annotations

import queue as queue_module
from pathlib import Path
from typing import Any

from mailflow.plugins import PluginManager
from mailflow.service import start_service
from mailflow_tui.app import MailFlowApp

from mailflow_test_support.tui import TUIPlugin, build_config


async def make_app(db_path: Path, *, splash: bool = False) -> tuple[Any, Any]:
    from mailflow_notify_console.plugin import plugin as notify_plugin
    from mailflow_storage_sqlite.plugin import plugin as storage_plugin

    manager = PluginManager(build_config(db_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    manager.register(notify_plugin)
    service = await start_service(
        build_config(db_path / "db.sqlite"),
        plugin_manager=manager,
        discover_plugins=False,
    )
    app = MailFlowApp(service, queue_module.Queue(), splash=splash)
    return app, service
