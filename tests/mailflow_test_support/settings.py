"""Headless UI fixtures with fake external mail and model services."""

from __future__ import annotations

from pathlib import Path

from mailflow.config import MailFlowConfig
from mailflow.service import MailFlowService
from mailflow_bundled import create_plugin_manager


def build_config(db_path: Path) -> MailFlowConfig:
    config = MailFlowConfig()
    config.storage.path = str(db_path)
    config.plugins.repositories = []
    return config


async def start_service_quiet(tmp_path: Path) -> MailFlowService:
    from mailflow.service import start_service

    config_path = tmp_path / "cfg.toml"
    config_path.write_text("", encoding="utf-8")
    config = build_config(tmp_path / "tui.db")
    manager = create_plugin_manager(config, discover_external=False)
    return await start_service(
        config,
        config_path=config_path,
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
