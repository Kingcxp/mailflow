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

import asyncio
from typing import Any, ClassVar

from mailflow.domain import MailRecord
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Markdown, Static

from mailflow_tui.labels import urgency_label

# Brand accent used for the title bar (matches the app's $accent).
_ACCENT = "#7EA7F8"


class AskCorrectModal(ModalScreen[dict[str, Any] | None]):
    """Conversational analysis window: left chat, right mail info, bottom
    input. Closing discards the chat (persisted corrections remain)."""

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService, record: MailRecord) -> None:
        super().__init__()
        self._service = service
        self._record = record
        self._history: list[dict[str, str]] = []
        self._request_in_flight = False
        self._bindings.bind("escape", "close", self._t("tui.btn_close"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold {_ACCENT}]{escape(self._record.mail.subject)}[/bold {_ACCENT}]  "
            f"[dim]{escape(self._t('tui.ask_correct_ephemeral'))}[/dim]",
            id="ask-correct-title",
        )
        with Horizontal(id="ask-correct-body"):
            with Vertical(id="ask-correct-chat"):
                yield Static(self._t("tui.ask_correct_chat_label"), id="ask-correct-chat-label")
                with ScrollableContainer(id="ask-correct-scroll"):
                    yield Vertical(id="ask-correct-messages")
            with Vertical(id="ask-correct-info"):
                yield Static(self._t("tui.ask_correct_info_label"), id="ask-correct-info-label")
                yield Static("", id="ask-correct-analysis-status")
                yield Static("", id="ask-correct-urgency")
                yield Static("", id="ask-correct-summary")
                yield Static("", id="ask-correct-reason")
                yield Static("", id="ask-correct-original-body")
                yield Static("", id="ask-correct-notes")
        with Horizontal(id="ask-correct-input-row"):
            yield Input(placeholder=self._t("tui.ask_correct_placeholder"), id="ask-correct-input")
            yield Button(self._t("tui.ask_correct_send"), id="ask-correct-send", variant="primary")
            yield Button(self._t("tui.btn_close"), id="ask-correct-close", variant="default")
        yield Footer()

    async def on_mount(self) -> None:
        self._render_mail_info()
        await self._render_chat()
        self.query_one("#ask-correct-input", Input).focus()  # pyright: ignore[reportUnknownMemberType]

    def _render_mail_info(self) -> None:
        """Right pane: current analysis + original body (body never edited)."""
        record = self._record
        urgency = record.effective_urgency
        # The four contract colors are reused everywhere; the localized label
        # receives the same color while its stored enum value stays unchanged.
        label = urgency_label(self._service, urgency)
        self.query_one("#ask-correct-urgency", Static).update(  # pyright: ignore[reportUnknownMemberType]
            f"[bold]{self._t('tui.column_urgency')}:[/bold] "
            f"[bold {urgency.color}]■ {escape(label)}[/bold {urgency.color}]"
        )
        failed_notes = [note for note in record.processor_notes if note.status == "failed"]
        failure_detail = "; ".join(
            f"{note.processor_id}: {note.message}" for note in failed_notes[:2]
        )
        if failure_detail:
            status_key = (
                "tui.detail_analysis_failed"
                if record.analysis_is_fallback
                else "tui.detail_analysis_partial"
            )
            status = self._t(status_key, error=failure_detail)
        elif record.analysis_is_fallback:
            status = self._t("tui.detail_analysis_unavailable")
        else:
            status = ""
        self.query_one("#ask-correct-analysis-status", Static).update(  # pyright: ignore[reportUnknownMemberType]
            f"[red]{escape(status)}[/red]" if status else ""
        )
        summary = (
            self._t("tui.detail_analysis_unavailable")
            if record.analysis_is_fallback
            else record.summary or ""
        )
        reason = record.analysis.reason if record.analysis else ""
        if not reason and record.analysis_is_fallback:
            reason = self._t("tui.detail_reason_unavailable")
        self.query_one("#ask-correct-summary", Static).update(  # pyright: ignore[reportUnknownMemberType]
            f"[bold]{self._t('tui.detail_summary')}:[/bold] {escape(summary)}"
        )
        self.query_one("#ask-correct-reason", Static).update(  # pyright: ignore[reportUnknownMemberType]
            f"[bold]{self._t('tui.detail_reason')}:[/bold] {escape(reason or '-')}"
        )
        body = record.mail.body_text or record.mail.body_html or ""
        self.query_one("#ask-correct-original-body", Static).update(  # pyright: ignore[reportUnknownMemberType]
            f"[dim][bold]{self._t('tui.detail_body')}:[/bold][/dim] {escape(body[:4000])}"
        )
        attachments = [a.filename for a in record.mail.attachments if a.filename]
        notes = ""
        if attachments:
            notes = f"{self._t('tui.detail_attachments')}: {escape(', '.join(attachments[:5]))}"
        self.query_one("#ask-correct-notes", Static).update(  # pyright: ignore[reportUnknownMemberType]
            notes
        )

    async def _render_chat(self) -> None:
        container = self.query_one("#ask-correct-messages", Vertical)
        await container.remove_children()
        children: list[Any] = []
        for item in self._history:
            if item["role"] == "user":
                role = self._t("tui.ask_correct_you")
                classes = "ask-correct-user"
            else:
                role = self._t("tui.ask_correct_llm")
                classes = "ask-correct-assistant"
            children.extend(
                (
                    Static(
                        f"[bold]{escape(role)}[/bold]",
                        classes=f"ask-correct-role {classes}",
                    ),
                    Markdown(item["content"], classes=classes),
                )
            )
        if children:
            await container.mount(*children)
        self.query_one("#ask-correct-scroll", ScrollableContainer).scroll_end(animate=False)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ask-correct-input":
            await self._send()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ask-correct-send":
            await self._send()
        elif event.button.id == "ask-correct-close":
            self.action_close()

    def _set_request_state(self, in_flight: bool) -> None:
        """Keep one question in flight so the chat history stays ordered."""
        if not self.is_mounted:
            return
        input_box = self.query_one("#ask-correct-input", Input)
        send = self.query_one("#ask-correct-send", Button)
        input_box.disabled = in_flight
        send.disabled = in_flight
        send.label = self._t("tui.ask_correct_working" if in_flight else "tui.ask_correct_send")

    async def _send(self) -> None:
        if self._request_in_flight:
            return
        input_box = self.query_one("#ask-correct-input", Input)
        text = input_box.value.strip()
        if not text:
            return
        input_box.value = ""
        self._history.append({"role": "user", "content": text})
        await self._render_chat()
        self._request_in_flight = True
        self._set_request_state(True)
        self.run_worker(
            self._request_worker(list(self._history)),
            exclusive=True,
            group="ask-correct-request",
            exit_on_error=False,
        )

    async def _request_worker(self, messages: list[dict[str, str]]) -> None:
        """Run the service call outside the input event handler.

        The service owns the LLM/network work. The modal worker only applies
        the result if the screen is still mounted; closing the modal cancels
        its worker without adding a misleading failure message.
        """
        try:
            try:
                result = await self._service.chat_about_mail(self._record.record_id, messages)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.is_mounted:
                    self._history.append(
                        {
                            "role": "assistant",
                            "content": self._t("tui.ask_correct_request_failed"),
                        }
                    )
                    await self._render_chat()
                return
            if not self.is_mounted:
                return
            reply = str(result.get("reply") or "")
            corrections: dict[str, Any] = result.get("corrections") or {}
            if corrections:
                content = "\n\n".join(
                    part for part in (reply, self._t("tui.ask_correct_applied")) if part
                )
                self._history.append({"role": "assistant", "content": content})
                fresh = await self._service.get_mail(self._record.record_id)
                if fresh is not None:
                    self._record = fresh
            else:
                self._history.append({"role": "assistant", "content": reply})
            self._render_mail_info()
            await self._render_chat()
        finally:
            if self.is_mounted:
                self._request_in_flight = False
                self._set_request_state(False)

    def action_close(self) -> None:
        self.dismiss(None)
