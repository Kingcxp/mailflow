"""Persistent local TUI processes with authenticated terminal attachment."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mailflow.config import MailFlowConfig, load_config

from mailflow_tui.session_terminal import (
    FRAME_LIMIT,
    attach_terminal,
    decode_event,
    has_terminal,
    read_frame,
    send_frame,
    session_driver,
)

if TYPE_CHECKING:
    from mailflow_tui.app import MailFlowApp


class SessionError(Exception):
    def __init__(self, key: str, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


@dataclass
class SessionInfo:
    id: str
    name: str
    config: str | None
    cwd: str
    token: str
    created_at: str
    language: str = "en"
    local: bool = False
    pid: int = 0
    port: int = 0
    error: str = ""

    @property
    def path(self) -> Path:
        return session_directory() / f"{self.id}.json"

    @property
    def log(self) -> Path:
        return session_directory() / f"{self.id}.log"

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        try:
            with os.fdopen(
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(asdict(self), handle, ensure_ascii=False)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


def session_directory() -> Path:
    path = (
        Path(os.environ.get("MAILFLOW_SESSION_DIR", str(Path.home() / ".mailflow/tui-instances")))
        .expanduser()
        .resolve()
    )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _read_info(path: Path) -> SessionInfo:
    return SessionInfo(**json.loads(path.read_text(encoding="utf-8")))


def _remove_info(path: Path) -> None:
    path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_exit(pid: int) -> None:
    deadline = time.monotonic() + 10
    while _pid_alive(pid):
        if os.name != "nt":
            with contextlib.suppress(ChildProcessError):
                if os.waitpid(pid, cast(Any, os).WNOHANG)[0] == pid:
                    return
        if time.monotonic() >= deadline:
            raise SessionError("sessions.stop_timeout")
        time.sleep(0.05)


async def connect(
    info: SessionInfo, command: str, **options: Any
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict[str, Any]]:
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", info.port, limit=FRAME_LIMIT
            )
            send_frame(writer, {"token": info.token, "command": command, **options})
            await writer.drain()
            response = await read_frame(reader)
        if not response.get("ok"):
            raise SessionError(str(response.get("error") or "sessions.unavailable"), name=info.name)
        return reader, writer, response
    except BaseException:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        raise


async def request(info: SessionInfo, command: str) -> dict[str, Any]:
    _reader, writer, response = await connect(info, command)
    writer.close()
    await writer.wait_closed()
    return response


async def list_instances() -> list[tuple[SessionInfo, str]]:
    async def inspect(path: Path) -> tuple[SessionInfo, str] | None:
        try:
            info = _read_info(path)
        except (OSError, ValueError, TypeError):
            return None
        try:
            status = await request(info, "status")
            return info, str(status["state"])
        except (OSError, EOFError, ValueError, TimeoutError, SessionError):
            if _pid_alive(info.pid):
                return info, "unavailable"
            age = (datetime.now(UTC) - datetime.fromisoformat(info.created_at)).total_seconds()
            if age < 10 and not info.pid:
                return info, "starting"
            _remove_info(path)
            return None

    results = await asyncio.gather(*(inspect(path) for path in session_directory().glob("*.json")))
    return sorted(
        (item for item in results if item is not None), key=lambda item: item[0].created_at
    )


async def find_instance(name: str | None) -> SessionInfo:
    items = await list_instances()
    matches = [info for info, _state in items if name is None or name in {info.id, info.name}]
    if len(matches) == 1:
        return matches[0]
    raise SessionError("sessions.choose" if matches else "sessions.not_found", name=name or "")


def _data_key(info: SessionInfo) -> str:
    config = load_config(info.config) if info.config else MailFlowConfig()
    path = Path(config.storage.path).expanduser()
    return os.path.normcase(str((Path(info.cwd) / path).resolve()))


@contextmanager
def _instance_locks(info: SessionInfo) -> Generator[None]:
    # OS locks survive neither crashes nor reboots. Never delete their files while locked.
    with contextlib.ExitStack() as stack:
        for key in ("name:" + info.name, "data:" + _data_key(info)):
            path = session_directory() / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
            handle = stack.enter_context(path.open("a+b"))
            handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    windows = cast(Any, msvcrt)
                    windows.locking(handle.fileno(), windows.LK_NBLCK, 1)
                else:
                    import fcntl

                    posix = cast(Any, fcntl)
                    posix.flock(handle.fileno(), posix.LOCK_EX | posix.LOCK_NB)
            except OSError as exc:
                raise SessionError("sessions.in_use", name=info.name) from exc
        yield


def _new_info(config_path: str | None, name: str | None, local: bool) -> SessionInfo:
    from mailflow_tui.runner import ensure_default_config

    config_path = ensure_default_config(config_path)
    config = load_config(config_path) if config_path else MailFlowConfig()
    return SessionInfo(
        id=secrets.token_hex(4),
        name=name or (Path(config_path).stem if config_path else "main"),
        config=str(Path(config_path).resolve()) if config_path else None,
        cwd=str(Path.cwd()),
        token=secrets.token_urlsafe(32),
        created_at=datetime.now(UTC).isoformat(),
        language=config.general.language or config.i18n.language,
        local=local,
    )


def _spawn(info: SessionInfo) -> subprocess.Popen[bytes]:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        command = [sys.executable, "--_mailflow-tui-host", info.id]
    else:
        command = [sys.executable, "-X", "utf8", "-B", "-m", "mailflow_tui.sessions", info.id]
    windows_subprocess = cast(Any, subprocess)
    with os.fdopen(os.open(info.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as log:
        return subprocess.Popen(
            command,
            cwd=info.cwd,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=windows_subprocess.CREATE_NO_WINDOW
            | windows_subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0,
            start_new_session=os.name != "nt",
        )


async def _wait_ready(
    info: SessionInfo, process: subprocess.Popen[bytes] | None = None
) -> SessionInfo:
    try:
        async with asyncio.timeout(60):
            while True:
                current = _read_info(info.path)
                if current.error:
                    raise SessionError(current.error, name=info.name, log=str(info.log))
                if process is not None and process.poll() is not None:
                    break
                if current.port:
                    status = await request(current, "status")
                    if status["state"] in {"attached", "detached"}:
                        return current
                    if status["state"] == "stopping":
                        raise SessionError("sessions.unavailable", name=info.name)
                await asyncio.sleep(0.1)
    except (OSError, EOFError, ValueError, TimeoutError) as exc:
        raise SessionError("sessions.start_failed", name=info.name, log=str(info.log)) from exc
    raise SessionError("sessions.start_failed", name=info.name, log=str(info.log))


async def start_instance(
    config_path: str | None, *, name: str | None = None, local: bool = False
) -> SessionInfo:
    info = _new_info(config_path, name, local)
    for existing, _state in await list_instances():
        if _data_key(existing) == _data_key(info):
            if (name and existing.name != name) or (local and not existing.local):
                raise SessionError("sessions.in_use", name=existing.name)
            return await _wait_ready(existing)
        if existing.name == info.name:
            raise SessionError("sessions.in_use", name=info.name)
    info.save()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = await asyncio.to_thread(_spawn, info)
        return await _wait_ready(info, process)
    except BaseException:
        # Only this newly created process may be stopped after a failed launch.
        if process is not None and process.poll() is None:
            with contextlib.suppress(OSError, SessionError, EOFError, TimeoutError):
                await stop_instance(_read_info(info.path))
            with contextlib.suppress(subprocess.TimeoutExpired):
                await asyncio.to_thread(process.wait, timeout=5)
        if process is None or process.poll() is not None:
            _remove_info(info.path)
        raise


async def attach_instance(info: SessionInfo, *, takeover: bool = False) -> str:
    if not has_terminal():
        raise SessionError("sessions.terminal_required")
    reader, writer, _response = await connect(info, "attach", takeover=takeover)
    result = await attach_terminal(reader, writer)
    if result == "stopped":
        await asyncio.to_thread(_wait_for_exit, info.pid)
    return result


async def stop_instance(info: SessionInfo) -> None:
    reader, writer, _response = await connect(info, "stop")
    try:
        async with asyncio.timeout(30):
            await reader.read()
    finally:
        writer.close()
        await writer.wait_closed()
    await asyncio.to_thread(_wait_for_exit, info.pid)


class SessionHost:
    def __init__(self, info: SessionInfo) -> None:
        self.info = info
        self.app: MailFlowApp | None = None
        self.client: asyncio.StreamWriter | None = None
        self.server: asyncio.Server | None = None
        self.ready = False
        self.stopping = False
        self.task: asyncio.Task[Any] | None = None
        self.closed = asyncio.Event()

    def bind(self, app: MailFlowApp) -> None:
        self.app = app
        app.driver_class = session_driver(self)

    def mark_ready(self) -> None:
        self.ready = True

    async def start(self) -> None:
        self.task = asyncio.current_task()
        self.server = await asyncio.start_server(self._connect, "127.0.0.1", 0, limit=FRAME_LIMIT)
        self.info.pid = os.getpid()
        self.info.port = int(self.server.sockets[0].getsockname()[1])
        self.info.save()

    def write(self, data: str) -> None:
        client = self.client
        if client is None or client.is_closing():
            return
        if client.transport.get_write_buffer_size() > FRAME_LIMIT:
            self.detach()
            return
        send_frame(client, {"event": "output", "data": data})

    def detach(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            send_frame(client, {"event": "detached"})
            client.close()

    def stop(self) -> None:
        self.stopping = True
        if self.app is not None and self.app.is_running:
            self.app.exit()
        elif self.task is not None:
            self.task.cancel()

    async def _connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(5):
                hello = await read_frame(reader)
            if not secrets.compare_digest(str(hello.get("token", "")), self.info.token):
                send_frame(writer, {"ok": False, "error": "sessions.auth_failed"})
                return
            command = hello.get("command")
            if command == "attach":
                if not self.ready or self.stopping:
                    send_frame(writer, {"ok": False, "error": "sessions.unavailable"})
                    return
                if self.client is not None:
                    if not hello.get("takeover"):
                        send_frame(writer, {"ok": False, "error": "sessions.attached"})
                        return
                    self.detach()
                self.client = writer
            state = (
                "stopping"
                if self.stopping
                else "starting"
                if not self.ready
                else "attached"
                if self.client is not None
                else "detached"
            )
            send_frame(writer, {"ok": True, "id": self.info.id, "state": state})
            if command == "detach":
                self.detach()
            elif command == "stop":
                self.stop()
                await self.closed.wait()
            elif command == "attach":
                assert self.app is not None
                self.app.screen.refresh(repaint=True, layout=True)
                while self.client is writer and not writer.is_closing():
                    frame = await read_frame(reader)
                    if self.client is not writer:
                        break
                    event = decode_event(frame)
                    if event is not None:
                        self.app.post_message(event)
        except (EOFError, OSError, ValueError, KeyError, TypeError, TimeoutError):
            pass
        finally:
            if self.client is writer:
                self.client = None
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def close(self) -> None:
        self.stopping = True
        self.closed.set()
        if self.client is not None:
            send_frame(self.client, {"event": "stopped"})
            self.client.close()
            self.client = None
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


async def _serve(info: SessionInfo) -> None:
    from mailflow_tui.runner import run_local_host

    host = SessionHost(info)
    loop = asyncio.get_running_loop()
    if os.name != "nt":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, host.stop)
    try:
        with _instance_locks(info):
            await host.start()
            await run_local_host(info.config, with_server=info.local, session=host)
    except SessionError as exc:
        info.error = exc.key
        info.save()
        raise
    finally:
        await host.close()
        if not info.error:
            info.path.unlink(missing_ok=True)


def host_main(instance_id: str) -> None:
    info = _read_info(session_directory() / f"{instance_id}.json")
    os.chdir(info.cwd)
    with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
        asyncio.run(_serve(info))


if __name__ == "__main__":
    host_main(sys.argv[1])
