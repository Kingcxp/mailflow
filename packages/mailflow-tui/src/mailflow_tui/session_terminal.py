"""Carry Textual output and input over a local session connection."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
from typing import TYPE_CHECKING, Any, cast

from textual import events
from textual.app import App
from textual.driver import Driver
from textual.geometry import Size
from textual.message import Message

if TYPE_CHECKING:
    from mailflow_tui.sessions import SessionHost

FRAME_LIMIT = 8 * 1024 * 1024
_MOUSE_EVENTS = {
    cls.__name__: cls
    for cls in (
        events.MouseDown,
        events.MouseUp,
        events.MouseMove,
        events.MouseScrollUp,
        events.MouseScrollDown,
    )
}


def send_frame(writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
    writer.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    line = await reader.readline()
    if not line:
        raise EOFError
    frame: Any = json.loads(line)
    if not isinstance(frame, dict):
        raise ValueError("invalid session frame")
    return cast(dict[str, Any], frame)


def encode_event(message: Message) -> dict[str, Any] | None:
    if isinstance(message, events.Key):
        return {"event": "Key", "key": message.key, "character": message.character}
    if isinstance(message, events.Paste):
        return {"event": "Paste", "text": message.text}
    if isinstance(message, events.Resize):
        return {"event": "Resize", "size": [message.size.width, message.size.height]}
    if isinstance(message, events.MouseEvent) and type(message).__name__ in _MOUSE_EVENTS:
        return {
            "event": type(message).__name__,
            **{
                key: getattr(message, key)
                for key in ("x", "y", "delta_x", "delta_y", "button", "shift", "meta", "ctrl")
            },
        }
    if isinstance(message, events.AppFocus | events.AppBlur):
        return {"event": type(message).__name__}
    return None


def decode_event(frame: dict[str, Any]) -> events.Event | None:
    kind = frame.get("event")
    if kind == "Key":
        return events.Key(str(frame["key"]), frame.get("character"))
    if kind == "Paste":
        return events.Paste(str(frame["text"]))
    if kind == "Resize":
        width, height = frame["size"]
        size = Size(max(1, min(1000, int(width))), max(1, min(500, int(height))))
        return events.Resize(size, size)
    if kind in _MOUSE_EVENTS:
        return _MOUSE_EVENTS[kind](
            None,
            x=float(frame["x"]),
            y=float(frame["y"]),
            delta_x=int(frame["delta_x"]),
            delta_y=int(frame["delta_y"]),
            button=int(frame["button"]),
            shift=bool(frame["shift"]),
            meta=bool(frame["meta"]),
            ctrl=bool(frame["ctrl"]),
        )
    if kind == "AppFocus":
        return events.AppFocus()
    if kind == "AppBlur":
        return events.AppBlur()
    return None


def session_driver(host: SessionHost) -> type[Driver]:
    class SessionDriver(Driver):
        def write(self, data: str) -> None:
            if not self._loop.is_closed():
                self._loop.call_soon_threadsafe(host.write, data)

        def start_application_mode(self) -> None:
            self._size = self._size or (100, 30)
            size = Size(*self._size)
            self._app.post_message(events.Resize(size, size))

        def disable_input(self) -> None:
            pass

        def stop_application_mode(self) -> None:
            pass

    return SessionDriver


async def attach_terminal(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
    """Reuse Textual's native terminal drivers, including Windows console input."""

    class TerminalEndpoint(App[None]):
        async def _post_message(self, message: Message) -> bool:
            if writer.is_closing():
                return False
            frame = encode_event(message)
            if frame is not None:
                send_frame(writer, frame)
            return True

    endpoint = TerminalEndpoint()
    driver = endpoint.driver_class(endpoint)
    try:
        driver.start_application_mode()
        size = shutil.get_terminal_size((100, 30))
        send_frame(writer, {"event": "Resize", "size": list(size)})
        while True:
            frame = await read_frame(reader)
            if frame.get("event") == "output":
                driver.write(str(frame["data"]))
            else:
                return str(frame.get("event") or "disconnected")
    except (EOFError, ConnectionError):
        return "disconnected"
    finally:
        # Restore raw mode, cursor, mouse reporting and the original terminal screen.
        # Closing this connection never stops the host or its mail/gateway resources.
        try:
            driver.stop_application_mode()
        finally:
            driver.close()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


def has_terminal() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())
