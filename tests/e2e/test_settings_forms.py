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
from mailflow_tui.app import MailFlowApp, MailPane
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


async def test_editing_an_llm_id_saves_and_rewrites_references(tmp_path: Path) -> None:
    """Editing an LLM must not be rejected for its own references.

    Renaming an id used to fail with "does not match any configured llm"
    because the entry's stale fallback list (and every processor binding) was
    validated before the derived chain was rebuilt.
    """
    from mailflow.settings import add_entry, update_entry
    from mailflow_tui.settings import EntryFormScreen

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    config = add_entry(
        service.config, "llms", {"llm_id": "alpha", "model": "m-a", "api_key": "sk-a"}
    )
    # a real key so the form's required credential extra is already satisfied
    config = add_entry(config, "llms", {"llm_id": "beta", "model": "m-b", "api_key": "sk-b"})
    config = add_entry(
        config,
        "processors",
        {
            "processor_id": "llm-importance",
            "provider": "llm-importance",
            "llm": "beta",
            "fallback_llms": [],
        },
    )
    # the first LLM falls back to the entry that is about to be renamed: the
    # stale reference that used to be rejected on save
    config = update_entry(config, "llms", 0, {"fallback": ["beta"]})
    service.config = config

    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.2)
            results: list[dict[str, Any] | None] = []
            app.push_screen(
                cast(
                    Any,
                    EntryFormScreen(
                        cast(Any, service),
                        "llms",
                        values=config.llms[1].model_dump(mode="json"),
                    ),
                ),
                results.append,
            )
            await _wait_until(pilot, lambda: bool(app.screen.query("#field-llm-id")))
            app.screen.query_one("#field-llm-id", Input).value = "beta-2"
            await _wait_until(
                pilot,
                lambda: not app.screen.query_one("#entry-form-save", Button).disabled,
            )
            app.screen.query_one("#entry-form-save", Button).press()
            await _wait_until(pilot, lambda: bool(results))

            payload = results[0]
            assert payload is not None, "the edit form reported no payload"
            saved = await service.update_config_entry("llms", 1, payload)

            assert [llm.llm_id for llm in saved.llms] == ["alpha", "beta-2"]
            # the derived chain and the processor binding follow the rename
            assert saved.llms[0].fallback == ["beta-2"]
            assert saved.processors[0].llm == "beta-2"
            # and the repaired config is what got written to disk
            written = (tmp_path / "cfg.toml").read_text(encoding="utf-8")
            assert 'llm_id = "beta-2"' in written
            assert '"beta"' not in written
            app.exit()
            await pilot.pause()
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

            urgency_filter = cast(Select[Any], app.query_one("#mail-urgency-filter", Select))
            urgency_select = cast(Select[Any], app.query_one("#urgency-select", Select))
            filter_labels = {str(option[0]) for option in urgency_filter._options}  # pyright: ignore[reportPrivateUsage]
            select_labels = {str(option[0]) for option in urgency_select._options}  # pyright: ignore[reportPrivateUsage]
            assert "全部紧急度" in filter_labels
            assert "跟随自动判定" in select_labels

            mail_tab_title = str(tabs.get_tab("tab-mail").label)  # pyright: ignore[reportUnknownMemberType]
            assert mail_tab_title == "邮件"
    finally:
        await service.stop()


async def test_modal_escape_bindings_follow_active_language(tmp_path: Path) -> None:
    """Every modal's visible Escape hint follows ``general.language``."""
    from mailflow_tui.export import BotExportScreen
    from mailflow_tui.install import InstallScreen
    from mailflow_tui.repos import ReposScreen
    from mailflow_tui.scaffold import PluginScaffoldScreen

    service = await start_service_quiet(tmp_path)
    try:
        await service.set_setting("general.language", "zh-CN")
        descriptions = [
            screen(service)._bindings.get_bindings_for_key("escape")[0].description  # pyright: ignore[reportPrivateUsage]
            for screen in (BotExportScreen, InstallScreen, ReposScreen, PluginScaffoldScreen)
        ]

        assert descriptions == ["取消"] * 4
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

            # The modal fills its info panel in on_mount, which runs after the
            # widgets exist: poll for the rendered content, not for the input,
            # or a slower runner reads an empty pane (this failed on Windows CI).
            def _panel_shows_urgency() -> bool:
                screen = app.screen
                if not isinstance(screen, AskCorrectModal):
                    return False
                node = screen.query_one_optional("#ask-correct-urgency", Static)
                return bool(node) and "urgent" in str(node.render()).lower()

            for _ in range(120):
                if _panel_shows_urgency():
                    break
                await pilot.pause(0.05)
            assert isinstance(app.screen, AskCorrectModal)

            # right pane shows the current urgency, header carries the reminder
            assert "urgent" in str(app.screen.query_one("#ask-correct-urgency").render()).lower()
            title = str(app.screen.query_one("#ask-correct-title").render())
            assert "temporary" in title.lower() or "临时" in title

            # sending a message without an LLM yields a friendly notice
            input_box = app.screen.query_one("#ask-correct-input", Input)
            input_box.value = "这是重要的会议吗?"
            app.screen.query_one("#ask-correct-send", Button).press()
            for _ in range(60):
                messages = list(app.screen.query("#ask-correct-messages Markdown"))
                rendered = (
                    " ".join(str(node.render()) for node in messages[-1].query("*"))
                    if messages
                    else ""
                )
                if "No LLM" in rendered:
                    break
                await pilot.pause(0.05)
            messages = list(app.screen.query("#ask-correct-messages Markdown"))
            assert messages
            rendered = " ".join(str(node.render()) for node in messages[-1].query("*"))
            assert "No LLM" in rendered

            # closing dismisses the modal and discards the chat
            app.screen.action_close()
            await pilot.pause()
            assert not isinstance(app.screen, AskCorrectModal)
    finally:
        await service.stop()


async def test_failed_analysis_is_disclosed_without_a_fake_summary(tmp_path: Path) -> None:
    """A subject fallback is shown as content but labelled as not generated."""
    from datetime import UTC
    from datetime import datetime as _dt

    from mailflow.domain import MailAnalysis, MailRecord, ProcessorNote, Urgency
    from mailflow_testkit.fakes import make_mail as make_test_mail

    service = await start_service_quiet(tmp_path)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    mail = make_test_mail(
        message_id="failed-analysis",
        subject="Source subject, not a summary",
        body_text="The complete original mail body remains readable.",
    )
    record = MailRecord(
        record_id="failed-analysis",
        mail=mail,
        auto_urgency=Urgency.INFO,
        analysis=MailAnalysis(
            summary=mail.subject,
            urgency=Urgency.INFO,
            summary_is_fallback=True,
        ),
        processor_notes=[
            ProcessorNote(
                processor_id="llm-importance",
                plugin_id="mailflow-core",
                status="failed",
                message="failed: HTTP 500: Internal Server Error",
                started_at=_dt.now(UTC),
                finished_at=_dt.now(UTC),
            )
        ],
    )
    await service.storage.save_mail(record)
    try:
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause(0.2)
            app.query_one("#btn-refresh", Button).press()
            await _wait_until(
                pilot,
                lambda: (
                    "complete original mail body"
                    in str(app.query_one("#mail-body", Static).render()).lower()
                ),
            )
            assert (
                "analysis failed"
                in str(app.query_one("#mail-analysis-status", Static).render()).lower()
            )
            # the failure reason of the processor is a real cause, never the
            # empty "failed: " a message-less exception used to leave behind
            assert (
                "http 500" in str(app.query_one("#mail-analysis-status", Static).render()).lower()
            )
            # the stored content stays visible, labelled as not generated
            summary = str(app.query_one("#mail-summary", Static).render()).lower()
            assert "source subject, not a summary" in summary
            assert "not generated" in summary
            assert (
                "no generated reason" in str(app.query_one("#mail-reason", Static).render()).lower()
            )
            assert (
                "complete original mail body"
                in str(app.query_one("#mail-body", Static).render()).lower()
            )

            assert record.analysis is not None
            record.analysis.reason = "A real urgency reason remains visible."
            await service.storage.save_mail(record)
            app.query_one("#btn-refresh", Button).press()
            await _wait_until(
                pilot,
                lambda: (
                    "real urgency reason"
                    in str(app.query_one("#mail-reason", Static).render()).lower()
                ),
            )
            assert (
                "real urgency reason" in str(app.query_one("#mail-reason", Static).render()).lower()
            )

            def _panel_shows_failure() -> bool:
                screen = app.screen
                if not isinstance(screen, AskCorrectModal):
                    return False
                node = screen.query_one_optional("#ask-correct-analysis-status", Static)
                return bool(node) and "analysis failed" in str(node.render()).lower()

            app.push_screen(AskCorrectModal(service, record))
            await _wait_until(pilot, _panel_shows_failure)
            assert (
                "analysis failed"
                in str(
                    app.screen.query_one("#ask-correct-analysis-status", Static).render()
                ).lower()
            )
            assert (
                "real urgency reason"
                in str(app.screen.query_one("#ask-correct-reason", Static).render()).lower()
            )
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


async def test_ask_correct_worker_keeps_modal_responsive_and_renders_markdown(
    tmp_path: Path,
) -> None:
    """The LLM call runs in a modal worker and the reply is real Markdown."""
    from mailflow.domain import MailAnalysis, MailRecord, Urgency
    from mailflow_testkit.fakes import make_mail as make_test_mail

    service = await start_service_quiet(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_chat(record_id: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        started.set()
        await release.wait()
        return {"reply": "**strong reply**\n\n- one item", "corrections": {}}

    service.chat_about_mail = fake_chat  # type: ignore[method-assign]
    record = MailRecord(
        record_id="m-worker",
        mail=make_test_mail(message_id="m-worker", subject="Worker test"),
        auto_urgency=Urgency.INFO,
        analysis=MailAnalysis(summary="Worker test", urgency=Urgency.INFO),
    )
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            app.push_screen(AskCorrectModal(service, record))
            await _wait_until(pilot, lambda: isinstance(app.screen, AskCorrectModal))
            modal = cast(AskCorrectModal, app.screen)
            modal.query_one("#ask-correct-input", Input).value = "Explain this"
            modal.query_one("#ask-correct-send", Button).press()
            await _wait_until(pilot, started.is_set)

            # The event loop can process this assertion while the fake LLM is
            # blocked, and the input is gated against reordering messages.
            assert modal.query_one("#ask-correct-send", Button).disabled
            release.set()
            await _wait_until(
                pilot,
                lambda: len(list(modal.query("#ask-correct-messages Markdown"))) >= 2,
            )
            messages = list(modal.query("#ask-correct-messages Markdown"))
            rendered = " ".join(str(node.render()) for node in messages[-1].query("*"))
            assert "strong reply" in rendered
            assert "one item" in rendered
            assert not modal.query_one("#ask-correct-send", Button).disabled
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
            provider_select.value = "openwechat"
            await pilot.pause(0.2)
            assert str(provider_select.value) == "openwechat"
    finally:
        await service.stop()


async def test_custom_todo_create_show_delete(tmp_path: Path) -> None:
    """The Actions tab 'Add todo' button opens the create form; a filled
    form lands in the custom-action store, shows up in the table, and is
    deleted for real (custom todos have no dismissal semantics)."""
    from mailflow.domain import ActionItem
    from textual.widgets import DataTable

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.2)
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-actions"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.2)

            # open the create form
            app.query_one("#actions-add", Button).press()
            await pilot.pause(0.2)
            from mailflow_tui.todo_create import TodoCreateModal

            assert isinstance(app.screen, TodoCreateModal)
            summary_input = app.screen.query_one("#todo-summary", Input)
            summary_input.value = "给导师发进度报告"
            due_input = app.screen.query_one("#todo-due", Input)
            due_input.value = "2099-01-01 09:00"
            notes_input = app.screen.query_one("#todo-notes", Input)
            notes_input.value = "每周例会前"
            app.screen.query_one("#todo-save", Button).press()
            await pilot.pause(0.3)

            # persisted in the custom store with the right fields
            custom = await service.storage.list_custom_actions()
            assert len(custom) == 1
            item = custom[0]
            assert isinstance(item, ActionItem)
            assert item.summary == "给导师发进度报告"
            assert item.mail_id == ""  # user-created: no source mail
            assert item.due_at.year == 2099

            # shows up in the actions table
            table = cast(DataTable[Any], app.query_one("#actions-table", DataTable))
            rows = " ".join(
                " ".join(str(c) for c in table.get_row_at(i)) for i in range(table.row_count)
            )
            assert "给导师发进度报告" in rows

            # edit it from the table selection: the Edit button opens the same
            # form pre-filled and the change is persisted
            idx = next(
                i
                for i in range(table.row_count)
                if "给导师发进度报告" in str(table.get_row_at(i)[2])
            )
            table.move_cursor(row=idx, animate=False)  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.05)
            app.query_one("#actions-edit", Button).press()
            await pilot.pause(0.3)
            from mailflow_tui.todo_create import TodoEditModal

            assert isinstance(app.screen, TodoEditModal)
            edit_summary = app.screen.query_one("#todo-summary", Input)
            assert str(edit_summary.value) == "给导师发进度报告"  # pre-filled
            edit_summary.value = "给导师发周报"
            app.screen.query_one("#todo-due", Input).value = "2099-02-03 10:30"
            app.screen.query_one("#todo-save", Button).press()
            await _wait_until(
                pilot,
                lambda: not isinstance(app.screen, (TodoEditModal,)),
            )
            edited = await service.storage.list_custom_actions()
            assert [item.summary for item in edited] == ["给导师发周报"]
            assert edited[0].due_at.year == 2099 and edited[0].due_at.month == 2
            # the table shows the edited row without a manual refresh
            await _wait_until(
                pilot,
                lambda: any(
                    "给导师发周报" in str(table.get_row_at(i)[2]) for i in range(table.row_count)
                ),
            )
            rows = " ".join(
                " ".join(str(c) for c in table.get_row_at(i)) for i in range(table.row_count)
            )
            assert "给导师发周报" in rows

            # delete it: custom todos are removed for real
            idx = next(
                i for i in range(table.row_count) if "给导师发周报" in str(table.get_row_at(i)[2])
            )
            table.move_cursor(row=idx, animate=False)  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.05)
            app.query_one("#actions-delete", Button).press()
            await pilot.pause(0.3)
            assert await service.storage.list_custom_actions() == []
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


async def test_seminar_review_requires_explicit_import(tmp_path: Path) -> None:
    """A discovered proposal stays out of the schedule until the user confirms it."""
    import json
    from datetime import UTC, datetime

    from mailflow.domain import ActionOrigin, SeminarCandidate
    from mailflow_tui.seminars import SeminarReviewModal

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    candidate = SeminarCandidate(
        candidate_id="seminar-test-1",
        mail_id="mail-1",
        title="Initial title",
        starts_at=datetime(2099, 1, 2, 9, 0, tzinfo=UTC),
        timezone="UTC",
        location="Room 201",
        url="https://events.example.test/seminar",
        description="Bring questions.",
        confidence=92,
    )
    await service.storage.set_preference(
        "seminars.candidates", json.dumps([candidate.model_dump(mode="json")])
    )
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.2)
            assert await service.storage.list_custom_actions() == []
            app.push_screen(SeminarReviewModal(service, [candidate]))
            await pilot.pause(0.2)
            assert isinstance(app.screen, SeminarReviewModal)

            app.screen.query_one("#seminar-title", Input).value = "Edited seminar"
            app.screen.query_one("#seminar-import", Button).press()
            await _wait_until(pilot, lambda: not isinstance(app.screen, SeminarReviewModal))

            custom = await service.storage.list_custom_actions()
            assert len(custom) == 1
            imported = custom[0]
            assert imported.summary == "Edited seminar"
            assert imported.mail_id == "mail-1"
            assert imported.origin is ActionOrigin.SEMINAR
            assert imported.location == "Room 201"
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


async def test_logs_export_saves_buffer_to_file(tmp_path: Path) -> None:
    """The Logs tab export button opens a directory tree + filename form;
    saving writes the buffered lines to <name>.log (suffix appended when
    missing) and confirms the path inside the log itself."""
    import logging as _logging

    from mailflow_tui.runner import TuiLogHandler

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    log_queue: queue_module.Queue[str] = queue_module.Queue()
    # enable_logging=False (quiet) never mounts the runtime, so the TUI
    # handler is attached directly to the mailflow logger like the real
    # runner would
    handler = TuiLogHandler(log_queue)
    ml = _logging.getLogger("mailflow")
    # quiet mode skips configure_logging, so the mailflow logger keeps the
    # root's WARNING default — INFO chat lines would be dropped at source
    ml.setLevel(_logging.INFO)
    ml.addHandler(handler)
    app = MailFlowApp(cast(Any, service), log_queue)
    export_dir = tmp_path / "exported"
    export_dir.mkdir()
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.3)
            _logging.getLogger("mailflow.bot_server").info("chat[probe] group:1: '#mailflow help'")
            from textual.widgets import TabbedContent

            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-logs"  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(1.0)
            from mailflow_tui.app import LogsPane

            pane = app.query_one(LogsPane)
            await pilot.pause(1.0)
            assert list(pane._buffer), "log buffer must contain lines"  # pyright: ignore[reportPrivateUsage]

            app.query_one("#logs-export", Button).press()
            await pilot.pause(0.3)
            from mailflow_tui.log_export import LogExportScreen

            assert isinstance(app.screen, LogExportScreen)

            # navigate the tree to the export dir: simulate by patching the
            # tree cursor node with the target directory
            from mailflow_tui.log_export import DirectoryTree  # reuse import path

            tree = app.screen.query_one("#log-export-tree", DirectoryTree)
            tree.path = export_dir
            await pilot.pause(0.5)
            # place the cursor on the root (the target directory itself)
            tree.cursor_line = 0
            await pilot.pause(0.1)
            name_input = app.screen.query_one("#log-export-filename", Input)
            name_input.value = "my-logs"  # no .log suffix -> appended
            app.screen.query_one("#log-export-save", Button).press()
            await pilot.pause(0.3)

            saved = export_dir / "my-logs.log"
            assert saved.exists(), "export must write the .log file"
            content = saved.read_text(encoding="utf-8")
            assert "chat[probe]" in content
            # confirmation written back into the log buffer
            assert "my-logs.log" in str(pane._buffer[-1])  # pyright: ignore[reportPrivateUsage]
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()


async def test_reparse_failed_works_without_selection(tmp_path: Path) -> None:
    """'Re-analyze failed' must start re-analysis of every failed mail even
    when nothing is selected in the table — selection is only required for
    the single-mail re-analyze button, never for the bulk one."""
    from datetime import UTC
    from datetime import datetime as _dt

    from mailflow.domain import ProcessorNote, Urgency
    from mailflow_testkit.fakes import make_mail as make_test_mail

    service = await start_service_quiet(tmp_path)
    calls: list[str] = []

    original = service.process_mail

    async def _spy(mail: Any, *, force: bool = False) -> Any:
        calls.append(str(mail.message_id))
        return await original(mail, force=force)

    service.process_mail = _spy  # type: ignore[method-assign]

    # a record whose LLM analysis failed (processor note marked failed)
    from mailflow.domain import MailRecord

    mail = make_test_mail(message_id="failed-1", subject="解析失败邮件")
    record = MailRecord(
        record_id="failed-1",
        mail=mail,
        auto_urgency=Urgency.INFO,
        processor_notes=[
            ProcessorNote(
                processor_id="llm-importance",
                plugin_id="mailflow-core",
                status="failed",
                message="failed: rate limit",
                started_at=_dt.now(UTC),
                finished_at=_dt.now(UTC),
            )
        ],
    )
    await service.storage.save_mail(record)

    CommandRouter(service)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.2)
            app.query_one("#btn-refresh", Button).press()
            await pilot.pause(0.2)

            # do NOT select any row — the bulk re-analyze button must not
            # depend on selection
            pane = app.query_one(MailPane)
            pane._selected_id = None  # pyright: ignore[reportPrivateUsage]
            assert pane._selected_id is None  # pyright: ignore[reportPrivateUsage]
            app.query_one("#btn-reparse-failed", Button).press()
            completion = service.t("tui.history_reanalyzed", count=1)
            await _wait_until(
                pilot,
                lambda: completion in str(app.query_one("#mail-operation-status", Static).render()),
            )
            assert "failed-1" in calls, (
                "re-analyze failed must process failed mails without a selection; "
                f"process_mail calls: {calls}"
            )
            assert completion in str(app.query_one("#mail-operation-status", Static).render())
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()
