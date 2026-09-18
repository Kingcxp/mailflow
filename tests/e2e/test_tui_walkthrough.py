"""A full walkthrough of the TUI: every tab, every Mail-tab control.

The point is user experience, not internals: an unhandled exception anywhere in
a handler fails the run (Textual re-raises it when the pilot exits), and the
assertions check what a person actually sees — rows, labels, status lines and
that controls return to a usable state after cancelling.

The LLM is stubbed so the walkthrough stays fast and deterministic; the real
model is exercised by the separate live measurements recorded in the build log.
"""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path
from typing import Any, cast

import pytest
from mailflow.commands import CommandRouter
from mailflow.contracts import LLMCompletion
from mailflow.plugins import PluginManager
from mailflow.service import start_service
from mailflow_storage_sqlite.plugin import plugin as storage_plugin
from mailflow_tui.app import ActionsPane, LLMPane, LogsPane, MailFlowApp, MailPane
from mailflow_tui.confirm import ConfirmModal
from mailflow_tui.profile import UserProfileModal
from test_tui import TUIPlugin, build_config
from textual.widgets import (
    Button,
    DataTable,
    Input,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TextArea,
)


class _StubRouter:
    """Answers every phase: intent, matching, seminar extraction and analysis."""

    async def chat(self, messages: Any, **kwargs: Any) -> LLMCompletion:
        system = str(messages[0].get("content", ""))
        user = str(messages[-1].get("content", ""))
        if system.startswith("You route one free-form"):
            # mirror the real routing precedence closely enough to exercise
            # the delete branch: removal wording decides, everything else
            # falls back to search
            intent = "delete" if "delete" in user.lower() else "search"
            return LLMCompletion(text=f'{{"intent":"{intent}"}}', model="stub")
        if "smart mail finder" in system:
            ref = user.split("candidate=", 1)[1].splitlines()[0] if "candidate=" in user else "m1"
            return LLMCompletion(text=f'[{{"id":"{ref}","relevance":90}}]', model="stub")
        if system.startswith("You are MailFlow's smart mail finder and"):
            ref = user.split("candidate=", 1)[1].splitlines()[0] if "candidate=" in user else "m1"
            return LLMCompletion(text=f'[{{"id":"{ref}","relevance":90}}]', model="stub")
        return LLMCompletion(
            text=(
                '{"summary":"Stub analysis","urgency":"important",'
                '"reason":"carries an action","reply_required":false,'
                '"suggested_reply":"","action_items":[],"notes":""}'
            ),
            model="stub",
        )


async def _wait_for(pilot: Any, predicate: Any, budget: float = 6.0) -> bool:
    """Poll a predicate; pilot pauses are not synchronization points."""
    deadline = asyncio.get_running_loop().time() + budget
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await pilot.pause(0.05)
    return bool(predicate())


@pytest.mark.asyncio
async def test_full_tui_walkthrough(tmp_path: Path) -> None:
    manager = PluginManager(build_config(tmp_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    service = await start_service(
        build_config(tmp_path / "walk.db"),
        config_path=tmp_path / "walk.toml",
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    CommandRouter(service)
    service.router = cast(Any, _StubRouter())
    # hold the single-mail re-analysis so its cancel affordance is observable,
    # then release it for the rest of the walkthrough
    gate = asyncio.Event()
    original_process = service.process_mail

    async def gated_process(mail: Any, *, force: bool = False) -> Any:
        if not gate.is_set():
            await gate.wait()
        return await original_process(mail, force=force)

    service.process_mail = gated_process  # type: ignore[method-assign]
    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            # -- the mail list fills in and the detail pane shows the stored mail
            for _ in range(200):
                if await service.count_mails() == 3:
                    break
                await pilot.pause(0.05)
            pane = app.query_one(MailPane)
            table = cast(DataTable[Any], app.query_one("#mail-table", DataTable))
            assert await _wait_for(pilot, lambda: table.row_count == 3), "mail table must fill"
            assert await _wait_for(
                pilot,
                lambda: bool(str(pane.query_one("#mail-body", Static).render()).strip()),
            ), "detail pane must show the body"
            summary = str(pane.query_one("#mail-summary", Static).render())
            assert "Summary" in summary or "摘要" in summary
            # empty status lines must not hold blank rows above the summary
            assert not pane.query_one("#mail-analysis-status", Static).display
            assert not pane.query_one("#mail-operation-status", Static).display

            # -- typing filters the list, clearing restores it
            search = app.query_one("#mail-search", Input)
            search.value = "ID card"
            assert await _wait_for(pilot, lambda: table.row_count == 1)
            search.value = ""
            assert await _wait_for(pilot, lambda: table.row_count == 3)

            # -- single-mail re-analyze: offers cancel, and cancelling restores
            # every control. The stub answers instantly, so this step holds the
            # mail on a gate long enough for the cancel affordance to be real.
            single = app.query_one("#btn-reparse", Button)
            single.press()
            assert await _wait_for(pilot, lambda: "Stop" in str(single.label)), (
                "button must offer cancel"
            )
            assert single.disabled is False
            single.press()
            gate.set()
            assert await _wait_for(pilot, lambda: "Re-analyze" in str(single.label)), (
                "button must restore"
            )
            assert not app.query_one("#btn-reparse-all", Button).disabled
            status = str(pane.query_one("#mail-operation-status", Static).render())
            assert "stopped" in status.lower()
            assert await _wait_for(
                pilot, lambda: not pane.query_one("#btn-reparse", Button).disabled
            )

            # -- bulk re-analyze: confirms first, cancels cleanly, and the run
            # always hands the controls back
            bulk = app.query_one("#btn-reparse-all", Button)
            bulk.press()
            assert await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmModal))
            app.screen.query_one("#confirm-cancel", Button).press()
            assert await _wait_for(pilot, lambda: not isinstance(app.screen, ConfirmModal))
            assert not bulk.disabled, "a cancelled dialog must leave the button usable"

            bulk.press()
            assert await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmModal))
            app.screen.query_one("#confirm-run", Button).press()
            assert await _wait_for(pilot, lambda: "Re-analyze" in str(bulk.label), budget=20.0)
            assert not app.query_one("#btn-reparse-failed", Button).disabled
            assert not single.disabled

            # -- clear expired mail: asks with a real count, cancels without deleting
            before = await service.count_mails()
            app.query_one("#btn-purge-expired", Button).press()
            assert await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmModal))
            app.screen.query_one("#confirm-cancel", Button).press()
            assert await _wait_for(pilot, lambda: not isinstance(app.screen, ConfirmModal))
            assert await service.count_mails() == before, "cancel must not delete anything"

            # -- mail preferences form saves what the model will read
            app.query_one("#btn-profile", Button).press()
            assert await _wait_for(pilot, lambda: isinstance(app.screen, UserProfileModal))
            form = cast(UserProfileModal, app.screen)
            form.query_one(
                "#profile-text", TextArea
            ).text = "Walkthrough: I care about course notices"
            form.query_one("#profile-save", Button).press()
            assert await _wait_for(pilot, lambda: not isinstance(app.screen, UserProfileModal))
            for _ in range(60):
                if "Walkthrough" in await service.user_profile():
                    break
                await pilot.pause(0.05)
            assert "Walkthrough" in await service.user_profile(), "preferences must be stored"

            # -- smart action: one instruction, matched rows only
            search.value = "the student ID invitation"
            app.query_one("#smart-action", Button).press()
            assert await _wait_for(
                pilot,
                # the pane's own result is the signal that the action finished
                lambda: pane._smart_action_result is not None,  # pyright: ignore[reportPrivateUsage]
                budget=15.0,
            )
            assert await _wait_for(pilot, lambda: table.row_count == 1)
            assert "ID card" in str(table.get_row_at(0)[1])

            # -- a delete instruction asks before removing, and cancel is safe
            before_delete = await service.count_mails()
            search.value = "delete the ads"
            app.query_one("#smart-action", Button).press()
            assert await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmModal))
            app.screen.query_one("#confirm-cancel", Button).press()
            assert await _wait_for(pilot, lambda: not isinstance(app.screen, ConfirmModal))
            assert await service.count_mails() == before_delete, "cancel must not delete anything"

            # -- every other tab mounts and renders without raising
            tabs = app.query_one(TabbedContent)
            for tab_id in (
                "tab-mailboxes",
                "tab-actions",
                "tab-llms",
                "tab-runtime",
                "tab-market",
                "tab-notifications",
                "tab-settings",
                "tab-logs",
            ):
                tabs.active = tab_id  # pyright: ignore[reportUnknownMemberType]
                assert await _wait_for(pilot, lambda: True, budget=1.0)
            assert app.is_running

            # -- the LLM chain reorders and the cursor follows
            tabs.active = "tab-llms"  # pyright: ignore[reportUnknownMemberType]
            assert await _wait_for(pilot, lambda: bool(app.query(LLMPane)))
            llm_pane = app.query_one(LLMPane)
            await pilot.pause(0.3)
            llm_table = cast(DataTable[Any], llm_pane.query_one("#llms-table", DataTable))
            llm_table.move_cursor(row=1, animate=False)  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause(0.1)
            app.query_one("#llm-up", Button).press()
            assert await _wait_for(pilot, lambda: llm_pane._selected == 0)  # pyright: ignore[reportPrivateUsage]

            # -- logs render and filter
            tabs.active = "tab-logs"  # pyright: ignore[reportUnknownMemberType]
            assert await _wait_for(pilot, lambda: bool(app.query(LogsPane)))
            logs = app.query_one(LogsPane)
            logs._log_queue.put("2026-09-17T10:00:00.000|ERROR|mailflow.service|walkthrough line")  # pyright: ignore[reportPrivateUsage]
            assert await _wait_for(pilot, lambda: bool(logs.query_one("#log-view", RichLog).lines))
            level = cast("Select[str]", logs.query_one("#log-level", Select))
            level.value = "DEBUG"
            await pilot.pause(0.2)

            # -- actions tab renders its own controls
            tabs.active = "tab-actions"  # pyright: ignore[reportUnknownMemberType]
            assert await _wait_for(pilot, lambda: bool(app.query(ActionsPane)))
            assert bool(app.query("#actions-edit")) and bool(app.query("#actions-review-seminars"))
            assert app.is_running
            app.exit()
            await pilot.pause()
    finally:
        await service.stop()
