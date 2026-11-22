"""A view of the service-owned console, using the shared chat command router."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from mailflow.console import ConsoleMessage
from mailflow.domain import StyleSpan
from mailflow.errors import translated_error
from mailflow.permissions import current_context, permission_scope
from rich.cells import cell_len
from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Offset
from textual.message import Message
from textual.widgets import Button, OptionList, Select, Static, TabbedContent

from mailflow_tui.confirm import ConfirmModal
from mailflow_tui.labels import error_message
from mailflow_tui.pickers import PathPicker
from mailflow_tui.widgets import PromptInput, SafeSelect, TextArea


class ConsoleContextMenu(OptionList):
    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "dismiss_menu", show=False)]
    DEFAULT_CSS = """
    ConsoleContextMenu {
        layer: overlay;
        position: absolute;
        width: 28;
        height: auto;
        border: round $ctp-surface1;
        padding: 0;
        background: $ctp-mantle;
        color: $ctp-text;
        display: none;
    }
    ConsoleContextMenu:focus {
        border: round $ctp-blue;
    }
    ConsoleContextMenu > .option-list--option-highlighted {
        color: $ctp-text;
        background: $ctp-surface0;
        text-style: bold;
    }
    ConsoleContextMenu > .option-list--option-hover {
        color: $ctp-text;
        background: $ctp-surface1;
    }
    """

    def action_dismiss_menu(self) -> None:
        self.post_message(self.DismissRequested())

    class DismissRequested(Message):
        pass


class ConsoleCard(Vertical, can_focus=True):
    BINDINGS: ClassVar[list[Any]] = [
        Binding("enter,m,shift+f10", "menu", show=False),
        Binding("up", "adjacent(-1)", show=False),
        Binding("down", "adjacent(1)", show=False),
    ]
    DEFAULT_CSS = """
    ConsoleCard { height: auto; margin: 0 0 1 0; padding: 0 1; border-left: solid $ctp-teal; }
    ConsoleCard.user { border-left: solid $ctp-blue; }
    ConsoleCard.notification { border-left: solid $ctp-peach; }
    ConsoleCard.system { border-left: solid $ctp-overlay0; }
    ConsoleCard.failed { border-left: solid $ctp-red; }
    ConsoleCard:focus { background: $ctp-surface0; }
    ConsoleCard Static { height: auto; }
    ConsoleCard .console-body { text-align: left; }
    ConsoleCard .console-caption { color: $ctp-subtext0; }
    ConsoleCard .console-objects { max-height: 3; }
    """

    class MenuRequested(Message):
        def __init__(self, card: ConsoleCard, x: int, y: int) -> None:
            super().__init__()
            self.card, self.x, self.y = card, x, y

    def __init__(self, service: Any, message: ConsoleMessage) -> None:
        super().__init__(id="console-msg-" + message.id, classes=message.role)
        self.service, self.message = service, message

    @staticmethod
    def _styled_text(spans: list[StyleSpan]) -> Text:
        colors = {
            "cyan": "$ctp-blue",
            "green": "$ctp-green",
            "yellow": "$ctp-yellow",
            "red": "$ctp-red",
            "magenta": "$ctp-mauve",
        }
        output = Text()
        previous_heading = False
        for span in spans:
            value = span.text
            if previous_heading and value.startswith("\n") and not value.startswith("\n\n"):
                value = "\n" + value
            attributes: list[str] = []
            color = ""
            for token in span.style.split():
                if token in colors:
                    color = colors[token]
                elif token == "dim":
                    color = "$ctp-subtext0"
                elif token in {"bold", "italic", "underline", "strike", "reverse"}:
                    attributes.append(token)
            style = " ".join((*attributes, color))
            output.append(value, style=style or None)
            previous_heading = "bold" in attributes and "cyan" in span.style.split()
        return output

    def compose(self) -> ComposeResult:
        yield Static(classes="console-caption")
        yield Static(markup=False, classes="console-body")
        yield Static(markup=False, classes="console-caption console-objects")

    def on_mount(self) -> None:
        self.call_after_refresh(lambda: self.update_message(self.message))

    def update_message(self, message: ConsoleMessage) -> None:
        self.message = message
        if not self.is_mounted:
            return
        color = {"user": "$ctp-blue", "notification": "$ctp-peach", "system": "$ctp-subtext0"}.get(
            message.role, "$ctp-teal"
        )
        header = Text(self.service.t("console.role_" + message.role), style="bold " + color)
        header.append(
            "  "
            + message.at.astimezone(ZoneInfo(self.service.config.general.timezone)).strftime(
                "%H:%M"
            ),
            style="$ctp-subtext0",
        )
        if message.state != "done":
            header.append(
                " · " + self.service.t("console.state_" + message.state),
                style="$ctp-red" if message.state == "failed" else "$ctp-yellow",
            )
        self.query_one(Static).update(header)
        body = self.query_one(".console-body", Static)
        renderable = (
            Markdown(message.text, justify="left")
            if message.markdown
            else self._styled_text(message.spans)
            if message.spans
            else Text(message.text)
        )
        body.update(renderable)
        details: list[str] = []
        if message.role == "user" and message.reply_to:
            details.append(self.service.t("console.quoted"))
        if message.references:
            titles = [
                reference.get("title") or self.service.t("console.source_" + reference["kind"])
                for reference in message.references[:3]
            ]
            details.append(self.service.t("console.related_items") + ": " + " · ".join(titles))
        if message.files:
            filenames = [str(file["filename"]) for file in message.files[:3]]
            if len(message.files) > len(filenames):
                filenames.append("…")
            details.append(self.service.t("console.attachments") + ": " + " · ".join(filenames))
        caption = self.query_one(".console-objects", Static)
        caption.update("\n".join(details))
        caption.display = bool(details)
        self.set_class(message.state == "failed", "failed")

    def action_menu(self) -> None:
        self.post_message(self.MenuRequested(self, self.region.x + 1, self.region.y + 1))

    def action_adjacent(self, offset: int) -> None:
        cards = list(self.parent.query(ConsoleCard)) if self.parent is not None else []
        index = cards.index(self) + offset
        if 0 <= index < len(cards):
            cards[index].focus()

    def on_click(self, event: events.Click) -> None:
        self.focus()
        if event.button == 3:
            event.stop()
            event.prevent_default()
            self.post_message(self.MenuRequested(self, event.screen_x, event.screen_y))


class ConsoleTimeline(VerticalScroll):
    class PositionChanged(Message):
        pass

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self.post_message(self.PositionChanged())


@dataclass
class ConversationView:
    rows: dict[str, ConsoleMessage] = field(default_factory=dict)
    runtime_id: str = ""
    cursor: int = 0
    has_more: bool = False
    busy: bool = False
    text: str = ""
    quote: ConsoleMessage | None = None
    scroll: float = 0
    follow: bool = True
    unread: int = 0
    pending: tuple[str, str, str] | None = None


class ConsolePane(Vertical):
    DEFAULT_CSS = """
    ConsolePane { height: 1fr; padding: 0 1; layers: base overlay; }
    ConsolePane .console-toolbar { height: 3; align-vertical: middle; }
    ConsolePane #console-session { width: 1fr; height: auto; color: $ctp-subtext0; }
    ConsolePane #console-channel { display: none; width: 24; height: 3; }
    ConsolePane Button { height: 3; width: auto; min-width: 8; margin: 0 1 0 0; }
    ConsolePane #console-status { height: auto; max-height: 3; color: $ctp-subtext0; }
    ConsolePane #console-enable { display: none; }
    ConsolePane #console-timeline { height: 1fr; padding: 1 0 0 0; border-top: solid $ctp-surface1; }
    ConsolePane #console-empty { height: auto; color: $ctp-subtext0; }
    ConsolePane #console-older { height: 1; min-height: 1; width: 100%; }
    ConsolePane #console-new-row { height: auto; align-horizontal: right; }
    ConsolePane #console-new-row, ConsolePane #console-quote-row { display: none; }
    ConsolePane #console-new { height: 1; min-height: 1; }
    ConsolePane #console-quote-row { height: auto; max-height: 3; border-left: solid $ctp-lavender; }
    ConsolePane #console-quote { width: 1fr; height: auto; color: $ctp-lavender; }
    ConsolePane #console-unquote { height: 1; min-height: 1; }
    ConsolePane #console-input { margin-top: 0; }
    ConsolePane #console-send-row { height: 3; align-vertical: middle; }
    ConsolePane #console-keys { width: 1fr; height: auto; color: $ctp-subtext0; }
    """

    def __init__(self, service: Any) -> None:
        super().__init__()
        self.service = service
        self._identity = current_context()
        self._channel = ""
        self._views: dict[str, ConversationView] = {"": ConversationView()}
        self._cards: dict[str, ConsoleCard] = {}
        self._channels: list[dict[str, Any]] = []
        self._prefix = "/"
        self._last_upload = ""
        self._lock = asyncio.Lock()
        self._unsubscribers: list[Callable[[], None]] = []
        self._loading = self._dirty = self._sending = self._operating = False
        self._rendering = False
        self._channel_valid = True
        self._context_menu_choices: list[tuple[str, str]] = []
        self._context_menu_message: ConsoleMessage | None = None
        self._context_menu_focus: Any | None = None

    @property
    def view(self) -> ConversationView:
        return self._views.setdefault(self._channel, ConversationView())

    def compose(self) -> ComposeResult:
        with Horizontal(classes="console-toolbar"):
            yield Static(id="console-session", markup=False)
            yield SafeSelect([], id="console-channel", allow_blank=True)
            yield Button(id="console-enable", variant="success", disabled=True)
            yield Button(id="console-menu", variant="primary")
        yield Static(id="console-status", markup=False)
        with ConsoleTimeline(id="console-timeline"):
            yield Button(id="console-older", disabled=True)
            yield Static(id="console-empty", markup=False)
        with Horizontal(id="console-new-row"):
            yield Button(id="console-new", variant="primary")
        with Horizontal(id="console-quote-row"):
            yield Static(id="console-quote", markup=False)
            yield Button(id="console-unquote")
        yield PromptInput(id="console-input")
        with Horizontal(id="console-send-row"):
            yield Button(id="console-send", variant="primary", disabled=True)
            yield Button(id="console-stop", variant="error", disabled=True)
            yield Static(id="console-keys", markup=False)
        yield ConsoleContextMenu(id="console-context-menu")

    def on_mount(self) -> None:
        for event in (
            "console.changed",
            "config.changed",
            "remote.connection.changed",
            "plugin.enabled",
            "plugin.disabled",
            "plugin.installed",
            "plugin.uninstalled",
            "backup.restored",
        ):
            self._unsubscribers.append(self.service.on(event, self._on_event))
        self.call_after_refresh(self._initialize)

    def _initialize(self) -> None:
        self.relabel()
        self.request_refresh()

    def on_unmount(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

    def relabel(self) -> None:
        if not self.is_mounted:
            return
        t = self.service.t
        self.query_one("#console-session", Static).update(t("console.session_only"))
        self.query_one("#console-empty", Static).update(t("console.welcome", prefix=self._prefix))
        self.query_one("#console-keys", Static).update(t("console.keys"))
        self.query_one("#console-input", PromptInput).placeholder = t(
            "console.input_placeholder", prefix=self._prefix
        )
        for name in ("menu", "send", "stop", "enable", "older", "unquote"):
            self.query_one("#console-" + name, Button).label = t("console." + name)
        for card in self._cards.values():
            card.update_message(card.message)
        self._sync_controls()

    async def _on_event(self, event: str, **payload: Any) -> None:
        if event == "console.changed" and payload.get("channel") != self._channel:
            return
        if event == "remote.connection.changed" and payload.get("state") != "connected":
            return
        self.request_refresh()

    def request_refresh(self) -> None:
        self._dirty = True
        if not self._loading and self.is_mounted:
            self._loading = True
            self.run_worker(self._refresh_loop(), group="console-feed", exit_on_error=False)

    def activate(self) -> None:
        view = self.view

        def restore() -> None:
            if self.view is view and self.is_mounted:
                timeline = self.query_one(ConsoleTimeline)
                if view.follow:
                    timeline.scroll_end(animate=False, immediate=True)
                else:
                    timeline.scroll_to(y=view.scroll, animate=False, immediate=True)

        self.call_after_refresh(restore)
        self.request_refresh()

    async def _refresh_loop(self) -> None:
        try:
            while self._dirty and self.is_mounted:
                self._dirty = False
                await asyncio.sleep(0.08)
                await self.refresh_messages()
        finally:
            self._loading = False

    async def refresh_messages(self, *, older: bool = False) -> None:
        async with self._lock:
            channel, view = self._channel, self.view
            before = min((r.sequence for r in view.rows.values()), default=0) if older else 0
            try:
                with permission_scope(self._identity):
                    data = await self.service.console_messages(
                        channel, before=before, after=0 if older else view.cursor
                    )
                    if view.runtime_id and view.runtime_id != data["runtime_id"]:
                        data = await self.service.console_messages(channel)
                if channel != self._channel or not self.is_mounted:
                    return
                timeline = self.query_one(ConsoleTimeline)
                following = (
                    timeline.is_vertical_scroll_end
                    if view.runtime_id and self.is_on_screen
                    else view.follow
                )
                if view.runtime_id != data["runtime_id"] or data["cleared"] >= min(
                    (r.sequence for r in view.rows.values()), default=data["cleared"] + 1
                ):
                    view.rows.clear()
                    view.quote = None
                    view.pending = None
                    await timeline.remove_children(ConsoleCard)
                    self._cards.clear()
                    view.has_more = False
                    following = True
                initial = not view.runtime_id or not view.cursor
                view.runtime_id = data["runtime_id"]
                if not older:
                    view.cursor = data["cursor"]
                view.busy = data["busy"]
                self._channel_valid = True
                self._channels = data["channels"]
                self._prefix = data["command_prefix"]
                status = next(
                    (c["reason"] for c in self._channels if c["id"] == data["channel"]), ""
                )
                if not data["subscribed"]:
                    status = self.service.t("console.unsubscribed")
                self.query_one("#console-status", Static).update(status)
                if not channel:
                    self._views.pop("", None)
                    self._channel = data["channel"]
                    self._views[self._channel] = view
                selector = self.query_one("#console-channel", Select)
                with selector.prevent(Select.Changed):
                    selector.set_options([(c["id"], c["id"]) for c in self._channels])
                    selector.value = self._channel
                selector.display = len(self._channels) > 1
                if initial or older:
                    view.has_more = data["has_more"]
                oldest = data["oldest"]
                for key in [key for key, row in view.rows.items() if row.sequence < oldest]:
                    view.rows.pop(key)
                    card = self._cards.pop(key, None)
                    if card is not None:
                        await card.remove()
                messages = [ConsoleMessage.model_validate(row) for row in data["messages"]]
                previous = max((r.sequence for r in view.rows.values()), default=0)
                if not following and not older:
                    view.unread += sum(
                        r.id not in view.rows and r.sequence > previous for r in messages
                    )
                view.rows.update({r.id: r for r in messages})
                await self._render_messages(
                    messages, following=following and not older, history=older
                )
                self.query_one("#console-empty", Static).update(
                    self.service.t("console.welcome", prefix=self._prefix)
                )
                self.query_one(PromptInput).placeholder = self.service.t(
                    "console.input_placeholder", prefix=self._prefix
                )
                self._sync_controls()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if channel == self._channel and self.is_mounted:
                    if getattr(exc, "code", "") == "console.channel_missing":
                        self._channel_valid = False
                        with permission_scope(self._identity):
                            available = await self.service.console_messages()
                        self._channels = available["channels"]
                        selector = self.query_one("#console-channel", Select)
                        with selector.prevent(Select.Changed):
                            selector.set_options([(c["id"], c["id"]) for c in self._channels])
                            selector.value = Select.NULL
                        selector.display = True
                        self._sync_controls()
                    self.query_one("#console-status", Static).update(
                        Text(error_message(self.service, exc), style="$ctp-red")
                    )

    async def _render_messages(
        self, rows: list[ConsoleMessage], *, following: bool, history: bool = False
    ) -> None:
        timeline, view = self.query_one(ConsoleTimeline), self.view
        anchor = next(
            (
                card
                for card in sorted(self._cards.values(), key=lambda c: c.message.sequence)
                if card.region.bottom > timeline.content_region.y
            ),
            None,
        )
        anchor_y = anchor.region.y if anchor is not None else 0
        old_scroll, old_height = timeline.scroll_y, timeline.virtual_size.height
        self._rendering = True
        try:
            new = sorted(
                (row for row in rows if row.id not in self._cards), key=lambda row: row.sequence
            )
            # Older pages precede the existing cards; ordinary changes update in place.
            for row in new:
                card = ConsoleCard(self.service, row)
                later = sorted(
                    (c for c in self._cards.values() if c.message.sequence > row.sequence),
                    key=lambda c: c.message.sequence,
                )
                await timeline.mount(card, before=later[0] if later else None)
                self._cards[row.id] = card
            for row in rows:
                if row.id in self._cards:
                    self._cards[row.id].update_message(row)
            self.query_one("#console-empty").display = not view.rows
            self.query_one("#console-older").display = view.has_more

            def restore() -> None:
                if not self.is_mounted or self.view is not view:
                    return
                if not self.is_on_screen:
                    self._rendering = False
                    self._sync_controls()
                    return
                if following:
                    timeline.scroll_end(animate=False, immediate=True)
                    view.unread = 0
                elif anchor is not None and anchor.is_mounted:
                    timeline.scroll_to(
                        y=old_scroll + anchor.region.y - anchor_y, animate=False, immediate=True
                    )
                elif history:
                    timeline.scroll_to(
                        y=old_scroll + timeline.virtual_size.height - old_height,
                        animate=False,
                        immediate=True,
                    )
                self._rendering = False
                view.scroll, view.follow = timeline.scroll_y, following
                self._sync_controls()

            self.call_after_refresh(restore)
        except BaseException:
            self._rendering = False
            raise

    def _sync_controls(self) -> None:
        if not self.is_mounted:
            return
        view = self.view
        self.query_one("#console-session").display = (
            self.size.width >= 90 or len(self._channels) <= 1
        )
        configured = next((c for c in self._channels if c["id"] == self._channel), None)
        enable = self.query_one("#console-enable", Button)
        enable.display = configured is not None and (
            not configured["configured"] or not configured["enabled"]
        )
        enable.disabled = self._operating
        self.query_one("#console-channel", Select).disabled = self._sending or self._operating
        self.query_one("#console-send", Button).disabled = (
            not self._channel_valid
            or self._sending
            or view.busy
            or not self.query_one(PromptInput).value.strip()
        )
        self.query_one("#console-stop", Button).disabled = not view.busy or self._operating
        self.query_one("#console-older", Button).disabled = not view.has_more
        self.query_one("#console-new-row").display = bool(view.unread)
        self.query_one("#console-new", Button).label = self.service.t(
            "console.new_messages", count=view.unread
        )
        self.query_one("#console-quote-row").display = view.quote is not None
        if view.quote is not None:
            self.query_one("#console-quote", Static).update(
                self.service.t(
                    "console.quote_preview", text=" ".join(view.quote.text.split())[:180]
                )
            )

    def on_resize(self, _event: events.Resize) -> None:
        self._sync_controls()

    def on_console_timeline_position_changed(self, event: ConsoleTimeline.PositionChanged) -> None:
        event.stop()
        if self.is_mounted and not self._rendering and self.is_on_screen:
            timeline = self.query_one(ConsoleTimeline)
            self.view.scroll = timeline.scroll_y
            self.view.follow = timeline.is_vertical_scroll_end
            if self.view.follow:
                self.view.unread = 0
            self._sync_controls()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "console-input":
            event.stop()
            self.view.text = self.query_one(PromptInput).value
            self._sync_controls()

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        event.stop()
        self._send()

    def _send(self) -> None:
        if (
            not self._channel_valid
            or self._sending
            or self.view.busy
            or not self.query_one(PromptInput).value.strip()
        ):
            return
        self._sending = True
        text = self.query_one(PromptInput).value
        self.view.text = text
        self.view.follow, self.view.unread = True, 0
        self.query_one(ConsoleTimeline).scroll_end(animate=False, immediate=True)
        quote_id = self.view.quote.id if self.view.quote else ""
        self._sync_controls()
        self.run_worker(
            self._submit(self._channel, self.view, text, quote_id),
            group="console-send",
            exit_on_error=False,
        )

    async def _submit(self, channel: str, view: ConversationView, text: str, quote_id: str) -> None:
        if view.pending is None or view.pending[:2] != (text, quote_id):
            view.pending = (text, quote_id, uuid4().hex)
        try:
            with permission_scope(self._identity):
                await self.service.console_send(
                    text, channel=channel, request_id=view.pending[2], quote_id=quote_id
                )
            view.pending = None
            unchanged = (
                self.query_one(PromptInput).value == text
                if self.view is view
                else view.text == text
            )
            if unchanged:
                view.text, view.quote = "", None
                if self.view is view:
                    self.query_one(PromptInput).value = ""
            if self.view is view:
                await self.refresh_messages()
        except Exception as exc:
            if self.is_mounted:
                self.query_one("#console-status", Static).update(
                    Text(error_message(self.service, exc), style="$ctp-red")
                )
        finally:
            self._sending = False
            self._sync_controls()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if (
            event.select.id != "console-channel"
            or event.value is Select.NULL
            or str(event.value) == self._channel
        ):
            return
        event.stop()
        async with self._lock:
            self.view.text = self.query_one(PromptInput).value
            self.view.scroll = self.query_one(ConsoleTimeline).scroll_y
            self.view.follow = self.query_one(ConsoleTimeline).is_vertical_scroll_end
            self._channel = str(event.value)
            self._channel_valid = True
            await self.query_one(ConsoleTimeline).remove_children(ConsoleCard)
            self._cards.clear()
            self.query_one(PromptInput).value = self.view.text
            await self._render_messages(
                list(self.view.rows.values()), following=self.view.follow
            )
            if not self.view.follow:
                self.call_after_refresh(
                    self.query_one(ConsoleTimeline).scroll_to, y=self.view.scroll, animate=False
                )
        self.request_refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        name = event.button.id
        if name == "console-send":
            self._send()
        elif name == "console-new":
            self.query_one(ConsoleTimeline).scroll_end(animate=False)
            self.view.unread = 0
            self._sync_controls()
        elif name == "console-unquote":
            self.view.quote = None
            self._sync_controls()
            self.query_one(PromptInput).focus()
        elif name == "console-older":
            self.run_worker(
                self.refresh_messages(older=True), group="console-feed", exit_on_error=False
            )
        elif name == "console-menu":
            self._menu()
        elif name in {"console-stop", "console-enable"} and not self._operating:
            self.run_worker(
                self._control("cancel" if name == "console-stop" else "enable"),
                group="console-control",
                exit_on_error=False,
            )

    async def _control(self, operation: str) -> None:
        if self._operating:
            return
        self._operating = True
        self._sync_controls()
        try:
            with permission_scope(self._identity):
                await getattr(self.service, "console_" + operation)(self._channel)
            await self.refresh_messages()
        except Exception as exc:
            self.query_one("#console-status", Static).update(
                Text(error_message(self.service, exc), style="$ctp-red")
            )
        finally:
            self._operating = False
            self._sync_controls()

    def _menu(self) -> None:
        choices = [
            (self.service.t("console." + name), name)
            for name in ("help", "refresh", "upload", "notification_settings", "clear")
        ]
        if not getattr(self.service, "remote", False):
            choices.insert(2, (self.service.t("console.model_settings"), "model_settings"))
        button = self.query_one("#console-menu", Button)
        self._open_context_menu(choices, button.region.x, button.region.bottom)

    def _open_context_menu(
        self,
        choices: list[tuple[str, str]],
        x: int,
        y: int,
        *,
        message: ConsoleMessage | None = None,
    ) -> None:
        if not choices:
            return
        menu = self.query_one(ConsoleContextMenu)
        menu.clear_options()
        menu.add_options(label for label, _ in choices)
        self._context_menu_choices = choices
        self._context_menu_message = message
        self._context_menu_focus = self.app.focused
        width = min(max(22, max(cell_len(label) for label, _ in choices) + 4), self.size.width)
        height = min(len(choices) + 2, 14, self.size.height)
        menu.styles.width = width
        menu.styles.height = height
        menu.styles.offset = Offset(
            min(max(0, x - self.region.x), max(0, self.size.width - width)),
            min(max(0, y - self.region.y), max(0, self.size.height - height)),
        )
        menu.highlighted = 0
        menu.display = True
        menu.focus()

    def _close_context_menu(self, *, restore_focus: bool) -> None:
        if not self.is_mounted:
            return
        menu = self.query_one(ConsoleContextMenu)
        menu.display = False
        self._context_menu_choices = []
        self._context_menu_message = None
        focus, self._context_menu_focus = self._context_menu_focus, None
        if restore_focus and focus is not None and getattr(focus, "is_mounted", False):
            focus.focus()

    def on_click(self, event: events.Click) -> None:
        menu = self.query_one(ConsoleContextMenu)
        if menu.display and event.screen_offset not in menu.region:
            self._close_context_menu(restore_focus=False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        menu = self.query_one(ConsoleContextMenu)
        if event.option_list is not menu or not 0 <= event.option_index < len(
            self._context_menu_choices
        ):
            return
        event.stop()
        choice = self._context_menu_choices[event.option_index][1]
        message = self._context_menu_message
        self._close_context_menu(restore_focus=True)
        if message is None:
            self.run_worker(
                self._run_conversation_action(choice),
                group="console-menu",
                exclusive=True,
                exit_on_error=False,
            )
        else:
            self.run_worker(
                self._run_message_action(message, choice),
                group="console-menu",
                exclusive=True,
                exit_on_error=False,
            )

    def on_console_context_menu_dismiss_requested(
        self, _event: ConsoleContextMenu.DismissRequested
    ) -> None:
        self._close_context_menu(restore_focus=True)

    async def _run_conversation_action(self, choice: str) -> None:
        if choice == "help":
            try:
                with permission_scope(self._identity):
                    await self.service.console_send(
                        self._prefix + "mailflow help",
                        channel=self._channel,
                        request_id=uuid4().hex,
                    )
                await self.refresh_messages()
            except Exception as exc:
                self.query_one("#console-status", Static).update(
                    Text(error_message(self.service, exc), style="$ctp-red")
                )
        elif choice == "refresh":
            self.request_refresh()
        elif choice == "notification_settings":
            self.app.query_one(TabbedContent).active = "tab-notifications"
        elif choice == "model_settings":
            self.app.query_one(TabbedContent).active = "tab-mailboxes"
        elif choice == "upload":
            await self._upload()
        elif choice == "clear":
            if self.view.busy:
                self.query_one("#console-status", Static).update(
                    self.service.t("console.stop_before_clear")
                )
                return
            confirmed = await cast(Any, self.app).push_screen_wait(
                ConfirmModal(
                    self.service,
                    title=self.service.t("console.clear"),
                    body=self.service.t("console.clear_explanation"),
                    confirm_label=self.service.t("console.clear"),
                    variant="error",
                    focus_confirm=False,
                )
            )
            if confirmed:
                await self._control("clear")

    def on_console_card_menu_requested(self, event: ConsoleCard.MenuRequested) -> None:
        event.stop()
        self._message_menu(event.card.message, event.x, event.y)

    def _message_menu(self, message: ConsoleMessage, x: int, y: int) -> None:
        if message.channel != self._channel:
            return
        choices = [
            (self.service.t("console.quote"), "quote"),
            (self.service.t("console.copy"), "copy"),
        ]
        choices.extend(
            (
                self.service.t(
                    "console.open_reference",
                    title=r.get("title") or self.service.t("console.source_" + r["kind"]),
                ),
                "ref:" + str(i),
            )
            for i, r in enumerate(message.references)
        )
        choices.extend(
            (self.service.t("console.save_file", name=f["filename"]), "file:" + str(i))
            for i, f in enumerate(message.files)
        )
        self._open_context_menu(choices, x, y, message=message)

    async def _run_message_action(self, message: ConsoleMessage, choice: str) -> None:
        if choice == "quote":
            self.view.quote = message
            self._sync_controls()
            self.query_one(PromptInput).focus()
        elif choice == "copy":
            self.app.copy_to_clipboard(message.text)
            self.notify(self.service.t("console.copied"))
        elif choice and choice.startswith("ref:"):
            reference = message.references[int(choice[4:])]
            if reference["kind"] == "mail":
                cast(Any, self.app).open_mail(reference["id"])
            elif reference["kind"] == "action":
                cast(Any, self.app).open_action(reference["id"])
        elif choice and choice.startswith("file:"):
            await self._download(message, message.files[int(choice[5:])])

    async def _upload(self) -> None:
        channel = self._channel
        path = await cast(Any, self.app).push_screen_wait(
            PathPicker(self.service, self._last_upload)
        )
        if not path:
            return
        self._last_upload = path
        try:

            def read() -> bytes:
                source = Path(path).expanduser()
                if source.stat().st_size > 4_000_000:
                    raise translated_error(ValueError, self.service.t, "experience.file_too_large")
                return source.read_bytes()

            data = await asyncio.to_thread(read)
            with permission_scope(self._identity):
                await self.service.console_upload(
                    channel,
                    {
                        "filename": Path(path).name,
                        "content_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                )
            await self.refresh_messages()
        except Exception as exc:
            self.query_one("#console-status", Static).update(
                Text(error_message(self.service, exc), style="$ctp-red")
            )

    async def _download(self, message: ConsoleMessage, file: dict[str, Any]) -> None:
        name = str(file["filename"]).replace("\\", "/").rsplit("/", 1)[-1]
        path = await cast(Any, self.app).push_screen_wait(
            PathPicker(self.service, str(Path.home() / name))
        )
        if not path:
            return
        try:
            destination = Path(path).expanduser()
            if await asyncio.to_thread(destination.exists):
                confirmed = await cast(Any, self.app).push_screen_wait(
                    ConfirmModal(
                        self.service,
                        title=self.service.t("console.save_file", name=name),
                        body=self.service.t("console.overwrite", path=str(destination)),
                        confirm_label=self.service.t("tui.btn_save"),
                        focus_confirm=False,
                    )
                )
                if not confirmed:
                    return
            with permission_scope(self._identity):
                content = await self.service.console_file(message.channel, message.id, file["id"])
            await asyncio.to_thread(destination.write_bytes, base64.b64decode(content["data"]))
            self.notify(self.service.t("console.saved_file", path=str(destination)))
        except Exception as exc:
            self.query_one("#console-status", Static).update(
                Text(error_message(self.service, exc), style="$ctp-red")
            )
