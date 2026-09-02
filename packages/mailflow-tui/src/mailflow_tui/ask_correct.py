"""Ask & Correct: an LLM chat over one analysed mail.

Opened from the Mail tab's Ask & Correct button. The left pane is a live
conversation with the LLM about this mail; the right pane shows the current
analysis (urgency, summary, reason, original body). The user can question the
urgency or ask for details; the LLM replies conversationally and may apply
corrections (urgency / summary / reason — never the body), which are written
back to the stored record and reflected in the right pane immediately.

The conversation is intentionally ephemeral: closing the modal discards it
(a reminder is shown in the header). Any corrections the LLM applied are
persisted, matching the old Reject flow's "feedback becomes a guideline"
behaviour — the user's stated preference is recorded into the feedback
guidelines so future analyses tune the same way.
"""

from __future__ import annotations

from typing import Any, ClassVar

from mailflow.domain import MailRecord
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class AskCorrectModal(ModalScreen[dict[str, Any] | None]):
    """Conversational analysis window: left chat, right mail info, bottom
    input. Closing discards the chat (persisted corrections remain)."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "close", "Close")]

    def __init__(self, service: MailFlowService, record: MailRecord) -> None:
        super().__init__()
        self._service = service
        self._record = record
        self._history: list[dict[str, str]] = []

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold]{escape(self._record.mail.subject)}[/bold] — "
            f"{escape(self._t('tui.ask_correct_ephemeral'))}",
            id="ask-correct-title",
        )
        with Horizontal(id="ask-correct-body"):
            with Vertical(id="ask-correct-chat"):  # noqa: SIM117 — nested layout required
                with ScrollableContainer(id="ask-correct-scroll"):
                    yield Static("", id="ask-correct-messages")
            with Vertical(id="ask-correct-info"):
                yield Static("", id="ask-correct-urgency")
                yield Static("", id="ask-correct-summary")
                yield Static("", id="ask-correct-reason")
                yield Static("", id="ask-correct-original-body")
                yield Static("", id="ask-correct-notes")
        with Horizontal(id="ask-correct-input-row"):
            yield Input(placeholder=self._t("tui.ask_correct_placeholder"), id="ask-correct-input")
            yield Button(self._t("tui.ask_correct_send"), id="ask-correct-send", variant="primary")

    async def on_mount(self) -> None:
        self._render_mail_info()
        self._render_chat()
        self.query_one("#ask-correct-input", Input).focus()  # pyright: ignore[reportUnknownMemberType]

    def _render_mail_info(self) -> None:
        """Right pane: current analysis + original body (body never edited)."""
        record = self._record
        urgency = record.effective_urgency.value
        self.query_one("#ask-correct-urgency", Static).update(  # pyright: ignore[reportUnknownMemberType]
            f"[bold]{self._t('tui.column_urgency')}:[/bold] {escape(urgency)}"
        )
        summary = record.summary or ""
        self.query_one("#ask-correct-summary", Static).update(  # pyright: ignore[reportUnknownMemberType]
            f"[bold]{self._t('tui.detail_summary')}:[/bold] {escape(summary)}"
        )
        reason = record.analysis.reason if record.analysis else ""
        self.query_one("#ask-correct-reason", Static).update(  # pyright: ignore[reportUnknownMemberType]
            f"[bold]{self._t('tui.detail_reason')}:[/bold] {escape(reason or '-')}"
        )
        body = record.mail.body_text or record.mail.body_html or ""
        self.query_one("#ask-correct-original-body", Static).update(  # pyright: ignore[reportUnknownMemberType]
            f"[bold]{self._t('tui.detail_body')}:[/bold] {escape(body[:4000])}"
        )
        attachments = [a.filename for a in record.mail.attachments if a.filename]
        notes = ""
        if attachments:
            notes = f"{self._t('tui.detail_attachments')}: {escape(', '.join(attachments[:5]))}"
        self.query_one("#ask-correct-notes", Static).update(  # pyright: ignore[reportUnknownMemberType]
            notes
        )

    def _render_chat(self) -> None:
        node = self.query_one("#ask-correct-messages", Static)
        lines: list[str] = []
        for item in self._history:
            role = (
                self._t("tui.ask_correct_you")
                if item["role"] == "user"
                else self._t("tui.ask_correct_llm")
            )
            lines.append(f"[bold]{role}[/bold] {escape(item['content'])}")
        node.update("\n\n".join(lines))  # pyright: ignore[reportUnknownMemberType]
        self.query_one("#ask-correct-scroll", ScrollableContainer).scroll_end(  # pyright: ignore[reportUnknownMemberType]
            animate=False
        )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ask-correct-input":
            await self._send()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ask-correct-send":
            await self._send()

    async def _send(self) -> None:
        input_box = self.query_one("#ask-correct-input", Input)
        text = input_box.value.strip()
        if not text:
            return
        input_box.value = ""
        self._history.append({"role": "user", "content": text})
        self._render_chat()
        result = await self._service.chat_about_mail(self._record.record_id, self._history)
        reply = str(result.get("reply") or "")
        corrections: dict[str, Any] = result.get("corrections") or {}
        if corrections:
            changed = self._service.t("tui.ask_correct_applied")
            self._history.append(
                {"role": "assistant", "content": f"{reply}\n\n[green]{changed}[/green]"}
            )
            fresh = await self._service.get_mail(self._record.record_id)
            if fresh is not None:
                self._record = fresh
        else:
            self._history.append({"role": "assistant", "content": reply})
        self._render_mail_info()
        self._render_chat()

    def action_close(self) -> None:
        self.dismiss(None)
