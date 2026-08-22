"""End-to-end coverage for the TUI settings surface: sidebar reload
serialization, the provider-aware entry form (defaults, required-field
validation, DuplicateIds regression) and the secret eye toggle."""

from __future__ import annotations

import asyncio
import queue as queue_module
from pathlib import Path
from typing import Any, cast

from mailflow.commands import CommandRouter
from mailflow.config import MailFlowConfig
from mailflow.service import MailFlowService
from mailflow_bundled import create_plugin_manager
from mailflow_storage_sqlite.plugin import plugin as storage_plugin  # noqa: F401
from mailflow_tui.app import MailFlowApp
from textual.widgets import Button, Input, Select, Static, TabbedContent


def build_config(db_path: Path) -> MailFlowConfig:
    config = MailFlowConfig()
    config.storage.path = str(db_path)
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


async def test_concurrent_reloads_do_not_duplicate_sections(
    tmp_path: Path,
) -> None:
    from mailflow_tui.settings import SettingsPane
    from textual.widgets import ListView

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-settings"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.3)

            pane = app.query_one(SettingsPane)
            listing = pane.query_one("#settings-sections", ListView)
            before = len(listing.children)

            # a save and a language-changed relabel can race two reloads;
            # serialized reloads must end with exactly one entry per section
            await asyncio.gather(pane.reload(), pane.reload(), pane.reload())
            await pilot.pause(0.2)

            after_ids = [str(child.query_one(Static).render()) for child in listing.children]
            assert len(after_ids) == before, f"duplicated entries: {after_ids} (before={before})"
    finally:
        await service.stop()


async def test_llm_form_default_provider_and_extras_rebuild(tmp_path: Path) -> None:
    from mailflow_tui.remote import RemoteUnsupported  # noqa: F401
    from mailflow_tui.settings import EntryFormScreen

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            results: list[dict[str, Any]] = []
            app.push_screen(
                EntryFormScreen(cast(Any, service), "llms"),
                lambda values: results.append(values),
            )
            await pilot.pause(0.2)

            provider_select = cast(Select[Any], app.screen.query_one("#field-provider", Select))
            # default provider for a brand-new LLM entry
            assert str(provider_select.value) == "openai-completions"

            # provider-specific extras are rendered immediately for the default
            assert app.screen.query_one("#extra-base-url", Input) is not None

            # switching provider rebuilds the extras without DuplicateIds
            provider_select.value = "google-vertex"
            await pilot.pause(0.3)
            assert len(app.screen.query("#extra-project")) == 1
            assert len(app.screen.query("#extra-base-url")) == 0

            provider_select.value = "openai-completions"
            await pilot.pause(0.3)
            assert len(app.screen.query("#extra-base-url")) == 1
            assert len(app.screen.query("#extra-api-key")) == 1
    finally:
        await service.stop()


async def test_llm_form_required_validation_and_eye_toggle(tmp_path: Path) -> None:
    from mailflow_tui.settings import EntryFormScreen

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            results: list[dict[str, Any]] = []
            app.push_screen(
                EntryFormScreen(cast(Any, service), "llms"),
                lambda values: results.append(values),
            )
            await pilot.pause(0.2)

            # eye toggle flips the masked password input
            api_input = app.screen.query_one("#extra-api-key", Input)
            assert api_input.password is True
            eye_button = app.screen.query_one("#extra-api-key-eye", Button)
            eye_button.press()
            await pilot.pause(0.1)
            assert api_input.password is False
            eye_button.press()
            await pilot.pause(0.1)
            assert api_input.password is True

            # leaving a required extra empty blocks the save with a red,
            # field-naming message instead of dismissing the form
            app.screen.query_one("#field-llm-id", Input).value = "test-llm"
            app.screen.query_one("#field-model", Input).value = "gpt-test"
            app.screen.query_one("#entry-form-save", Button).press()
            await pilot.pause(0.2)

            status_text = str(app.screen.query_one("#entry-form-status", Static).render())
            assert "api key is required" in status_text.lower()
            assert results == []  # form stays open

            # filling the remaining required field unblocks the save
            app.screen.query_one("#extra-api-key", Input).value = "sk-test"
            app.screen.query_one("#entry-form-save", Button).press()
            await pilot.pause(0.2)

            assert len(results) == 1
            saved = results[0]
            assert saved["provider"] == "openai-completions"
            assert saved["api_key"] == "sk-test"
    finally:
        await service.stop()
        await service.stop()
