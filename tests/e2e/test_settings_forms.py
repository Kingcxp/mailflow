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
from mailflow_tui.app import MailFlowApp
from mailflow_tui.ask_correct import AskCorrectModal
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


def _result_collector(results: list[dict[str, Any]]) -> Any:
    """push_screen callback capturing a form's result payload."""

    def on_done(values: dict[str, Any] | None) -> None:
        if values is not None:
            results.append(values)

    return on_done


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


async def _wait_until(pilot: Any, predicate: Any, budget: float = 4.0) -> None:
    """Pilot pauses are not synchronization points on slow runners — poll
    the predicate instead of sleeping a fixed 0.3s."""
    import time as _time

    deadline = _time.monotonic() + budget
    while _time.monotonic() < deadline:
        if predicate():
            await pilot.pause()
            return
        await pilot.pause(0.05)
    await pilot.pause()


async def test_llm_form_default_provider_and_extras_rebuild(tmp_path: Path) -> None:
    from mailflow_tui.settings import EntryFormScreen

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            results: list[dict[str, Any]] = []

            def on_form_done(values: dict[str, Any] | None) -> None:
                if values is not None:
                    results.append(values)

            app.push_screen(
                cast(Any, EntryFormScreen(cast(Any, service), "llms")),
                on_form_done,
            )
            await pilot.pause(0.2)

            provider_select = cast(Select[Any], app.screen.query_one("#field-provider", Select))
            # default provider for a brand-new LLM entry
            assert str(provider_select.value) == "openai-completions"

            # provider-specific extras are rendered immediately for the default
            await _wait_until(pilot, lambda: bool(app.screen.query("#extra-base-url")))
            assert app.screen.query_one("#extra-base-url", Input) is not None

            # switching provider rebuilds the extras without DuplicateIds.
            # Wait on the FULL target state: a partial intermediate render
            # (one field present, another not yet) must not satisfy the wait
            provider_select.value = "google-vertex"
            await _wait_until(
                pilot,
                lambda: (
                    len(app.screen.query("#extra-project")) == 1
                    and not app.screen.query("#extra-base-url")
                    and not app.screen.query("#extra-api-key")
                ),
            )
            assert len(app.screen.query("#extra-project")) == 1
            assert len(app.screen.query("#extra-base-url")) == 0

            provider_select.value = "openai-completions"
            await _wait_until(
                pilot,
                lambda: (
                    len(app.screen.query("#extra-base-url")) == 1
                    and len(app.screen.query("#extra-api-key")) == 1
                ),
            )
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

            def on_form_done(values: dict[str, Any] | None) -> None:
                if values is not None:
                    results.append(values)

            app.push_screen(
                cast(Any, EntryFormScreen(cast(Any, service), "llms")),
                on_form_done,
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


async def test_account_form_opens_with_imap_default(tmp_path: Path) -> None:
    """Regression: a NULL initial value crashed Select._on_mount with
    InvalidSelectValueError when opening the add-mailbox form."""
    from mailflow_tui.settings import EntryFormScreen

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            results: list[dict[str, Any]] = []
            app.push_screen(
                cast(Any, EntryFormScreen(cast(Any, service), "accounts")),
                _result_collector(results),
            )
            await pilot.pause(0.3)

            provider_select = cast(Select[Any], app.screen.query_one("#field-provider", Select))
            assert str(provider_select.value) == "imap"
            # imap-specific extras rendered for the default provider
            assert app.screen.query_one("#extra-username", Input) is not None
            assert results == []  # nothing saved by merely opening
    finally:
        await service.stop()


async def test_language_change_propagates_to_other_tabs(tmp_path: Path) -> None:
    from textual.widgets import TabbedContent

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-mail"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.3)

            refresh_before = str(app.query_one("#btn-refresh", Button).label)

            await cast(Any, service).set_setting("general.language", "zh-CN")
            # the language.changed worker remounts every composed pane
            await pilot.pause(1.0)

            assert service.i18n.language == "zh-CN"
            refresh_after = str(app.query_one("#btn-refresh", Button).label)
            assert refresh_after != refresh_before
            assert refresh_after == "刷新"

            mail_tab_title = str(tabs.get_tab("tab-mail").label)  # pyright: ignore[reportUnknownMemberType]
            assert mail_tab_title == "邮件"
    finally:
        await service.stop()


async def test_entry_form_fields_scroll(tmp_path: Path) -> None:
    """The LLM form is taller than its dialog: the fields container must be
    scrollable (regression: a nested 1fr Vertical collapsed scrolling)."""
    from mailflow_tui.settings import EntryFormScreen

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(
                cast(Any, EntryFormScreen(cast(Any, service), "llms")),
                _result_collector([]),
            )
            await pilot.pause(0.3)

            fields = app.screen.query_one("#entry-form-fields")
            assert fields.virtual_size.height > fields.size.height, (
                "form content does not overflow: nothing to scroll"
            )
            fields.scroll_down()
            await pilot.pause(0.1)
            assert fields.scroll_y > 0
    finally:
        await service.stop()


async def test_ask_correct_modal_opens_and_sends(tmp_path: Path) -> None:
    """The 提问修正 flow: the button opens the Ask & Correct chat modal with
    the mail info panel and a bottom input; sending without an LLM yields a
    friendly notice; closing discards the (ephemeral) chat."""
    from mailflow.domain import MailAnalysis, MailRecord, Urgency
    from mailflow_testkit.fakes import make_mail as make_test_mail
    from textual.widgets import DataTable

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(160, 50)) as pilot:
            record = MailRecord(
                record_id="m-ask",
                mail=make_test_mail(message_id="m-ask", subject="会议通知"),
                auto_urgency=Urgency.URGENT,
                analysis=MailAnalysis(summary="会议", urgency=Urgency.URGENT),
            )
            await service.storage.save_mail(record)
            await pilot.pause()
            app.query_one("#btn-refresh", Button).press()
            await pilot.pause()

            table = cast(DataTable[Any], app.query_one("#mail-table", DataTable))
            for _ in range(60):
                if table.row_count >= 1:
                    break
                await pilot.pause(0.05)
            assert table.row_count >= 1

            app.query_one("#btn-ask-correct", Button).press()

            # modal mounts asynchronously: poll for its input field
            for _ in range(60):
                if isinstance(app.screen, AskCorrectModal) and app.screen.query_one_optional(
                    "#ask-correct-input", Input
                ):
                    break
                await pilot.pause(0.05)
            assert isinstance(app.screen, AskCorrectModal)

            # right pane shows the current urgency, header carries the reminder
            assert "urgent" in str(app.screen.query_one("#ask-correct-urgency").render())
            title = str(app.screen.query_one("#ask-correct-title").render())
            assert "temporary" in title.lower() or "临时" in title

            # sending a message without an LLM yields a friendly notice
            input_box = app.screen.query_one("#ask-correct-input", Input)
            input_box.value = "这是重要的会议吗?"
            app.screen.query_one("#ask-correct-send", Button).press()
            for _ in range(60):
                if "No LLM" in str(app.screen.query_one("#ask-correct-messages").render()):
                    break
                await pilot.pause(0.05)
            messages = str(app.screen.query_one("#ask-correct-messages").render())
            assert "No LLM" in messages

            # closing dismisses the modal and discards the chat
            app.screen.action_close()
            await pilot.pause()
            assert not isinstance(app.screen, AskCorrectModal)
    finally:
        await service.stop()


async def test_account_form_test_button_probes_imap_not_llm(tmp_path: Path) -> None:
    """Regression: the form's test button used to run the LLM probe for
    every group, surfacing "no llm_backend component 'imap'" in the
    mailbox form. It must dispatch to the IMAP credential check."""
    from textual.widgets import Button, Static

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            from textual.widgets import TabbedContent

            app.query_one(TabbedContent).active = "tab-mailboxes"
            for _ in range(40):
                if app.query("#account-add"):
                    break
                await pilot.pause(0.05)
            app.query_one("#account-add", Button).press()
            await pilot.pause(0.3)
            for _ in range(40):
                if app.screen.query("#entry-form-test"):
                    break
                await pilot.pause(0.05)
            # default provider imap, no credentials yet
            app.screen.query_one("#entry-form-test", Button).press()
            status = app.screen.query_one("#entry-form-status", Static)
            text = ""
            for _ in range(40):
                text = str(status.content)
                if text.strip():
                    break
                await pilot.pause(0.05)
            assert "llm_backend" not in text
            assert text.strip()
    finally:
        await service.stop()


async def test_history_bulk_load_beyond_hundred_mails(tmp_path: Path) -> None:
    """Loading 5+ consecutive pages (125 mails) must not crash the app:
    regression for the >100-mails report."""

    from textual.widgets import Button, DataTable, TabbedContent

    service = await start_service_quiet(tmp_path)
    # the built-in mailflow-mail-fake plugin builds its source from the
    # account's options.mails list — declare 125 mails there
    service.config.accounts = [
        _fake_account(
            mails=[{"message_id": f"bulk-{i}", "subject": f"历史邮件 {i}"} for i in range(125)]
        )
    ]
    await service.reload_runtime()
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = "tab-mailboxes"
            await _wait_until(pilot, lambda: bool(app.query("#history-more")))
            pane = _accounts_pane(app)
            pane._selected = 0
            pane._history_account = "acct-1"

            more = app.query_one("#history-more", Button)
            for _expected in range(25, 126, 25):  # 5 x 25 = 125 > 100
                before = len(pane._history)
                more.press()
                # a page can yield fewer NEW rows (server resends overlap);
                # wait for progress, not an exact count
                await _wait_until(
                    pilot,
                    lambda: len(pane._history) > before,  # noqa: B023
                )
            table = cast(DataTable[Any], app.query_one("#history-table", DataTable))
            assert table.row_count == 125
    finally:
        await service.stop()


def _fake_account(mails: list[dict[str, Any]] | None = None) -> Any:
    from mailflow.config import MailAccountConfig

    return MailAccountConfig(
        account_id="acct-1",
        provider="fake",
        email="a@b.c",
        options={"mails": mails or []},
    )


def _accounts_pane(app: Any) -> Any:
    from mailflow_tui.settings import AccountsPane

    return app.query_one(AccountsPane)


async def test_notifier_form_mounts_with_valid_provider_default(tmp_path: Path) -> None:
    """Regression: clicking Add notification must not crash with
    InvalidSelectValueError. The notifier form's provider choices are the
    IM/gateway platforms, and the default provider must be one of them
    (the old default "console" was not, so Textual Select rejected it at
    mount)."""
    from mailflow_tui.settings import EntryFormScreen

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.push_screen(cast(Any, EntryFormScreen(cast(Any, service), "notifiers")))
            await pilot.pause(0.2)

            provider_select = cast(Select[Any], app.screen.query_one("#field-provider", Select))
            # the preselected default is always one of the listed providers
            value = str(provider_select.value)
            option_values = {
                str(o[1])
                for o in provider_select._options  # pyright: ignore[reportPrivateUsage]
            }
            assert value in option_values, f"default {value!r} not in {option_values}"
            assert value == "onebot"
            # the form is actually interactive (switch provider rebuilds)
            provider_select.value = "wechaty"
            await pilot.pause(0.2)
            assert str(provider_select.value) == "wechaty"
    finally:
        await service.stop()
