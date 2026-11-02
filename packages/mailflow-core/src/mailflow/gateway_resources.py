"""Resources shared by the local chat gateways.

Only a process launched by this owner may be reused or terminated. Port
numbers and directory names alone never establish ownership.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx

from mailflow.contracts import GatewayInstance
from mailflow.lifecycle import bounded

logger = logging.getLogger("mailflow.gateway")
LOGGED_IN = "__MAILFLOW_LOGGED_IN__"
_reapers: set[asyncio.Task[Any]] = set()


def _track_reaper(task: asyncio.Task[Any]) -> None:
    _reapers.add(task)
    task.add_done_callback(_reapers.discard)
    task.add_done_callback(
        lambda finished: finished.exception() if not finished.cancelled() else None
    )


def instance_token(instance_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in instance_id)
    safe = safe.strip("-_. ").lower()[:64] or "instance"
    if safe != instance_id:
        safe += "-" + hashlib.sha256(instance_id.encode()).hexdigest()[:12]
    return safe


def port_available(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def read_metadata(target: Path) -> dict[str, Any]:
    try:
        value: Any = json.loads((target / "mailflow.json").read_text(encoding="utf-8"))
        return dict(cast("dict[str, Any]", value)) if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def instance_directory(root: Path, provider: str, instance_id: str) -> Path:
    target = root / f"{provider}-{instance_token(instance_id)}"
    if target.exists():
        return target
    legacy_token = "".join(c if c.isalnum() or c in "-_." else "-" for c in instance_id).strip("-")
    legacy = root / f"{provider}-{legacy_token}"
    if legacy != target and legacy.is_dir():
        metadata = read_metadata(legacy)
        owner = metadata.get("instance_id")
        if owner is None or owner == instance_id:
            # Keep existing login data in place. The first legacy owner is
            # recorded so a second ID with the same old spelling is isolated.
            if owner is None:
                (legacy / "mailflow.json").write_text(
                    json.dumps({**metadata, "instance_id": instance_id}), encoding="utf-8"
                )
            return legacy
    return target


def reserved_ports(root: Path, *, exclude: tuple[Path, str] | None = None) -> set[int]:
    reserved: set[int] = set()
    for filename in ("mailflow.json", "instance.json"):
        for path in root.glob(f"*/{filename}"):
            try:
                values: Any = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(values, dict):
                reserved.update(
                    value
                    for key, value in cast("dict[str, Any]", values).items()
                    if key.endswith("_port")
                    and isinstance(value, int)
                    and (exclude is None or (path.parent, key) != exclude)
                )
    return reserved


def allocate_port(target: Path, name: str, preferred: int, *, explicit: bool = False) -> int:
    metadata = read_metadata(target)
    candidate = int(metadata.get(name) or preferred) if not explicit else preferred
    reserved = reserved_ports(target.parent, exclude=(target, name))
    for offset in range(1 if explicit else 1000):
        port = candidate + offset
        if port <= 65535 and port not in reserved and port_available(port):
            target.mkdir(parents=True, exist_ok=True)
            (target / "mailflow.json").write_text(
                json.dumps({**metadata, name: port}), encoding="utf-8"
            )
            return port
    raise RuntimeError(f"port {candidate} is in use; choose a different port")


def terminate_process(process: subprocess.Popen[Any], *, tree: bool = False) -> None:
    if process.poll() is not None and (not tree or os.name == "nt"):
        return
    if tree:
        if os.name == "nt":
            # Only the PID returned by our own launch may be targeted.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=2,
                check=False,
            )
        else:
            kill_group = cast(Callable[[int, int], None], getattr(os, "killpg", None))
            with contextlib.suppress(ProcessLookupError):
                kill_group(process.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
            with contextlib.suppress(ProcessLookupError):
                kill_group(process.pid, getattr(signal, "SIGKILL", 9))
        if process.poll() is not None:
            return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


async def launch_owned(
    launch: Callable[[], subprocess.Popen[Any]],
    *,
    terminate: Callable[[subprocess.Popen[Any]], None] = terminate_process,
) -> subprocess.Popen[Any]:
    """Reap a late Popen result if cancellation wins the launch race."""
    cancelled = threading.Event()

    def create() -> subprocess.Popen[Any]:
        process = launch()
        if cancelled.is_set():
            terminate(process)
        return process

    task = asyncio.create_task(asyncio.to_thread(create))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()

        # The launch may have finished just before the event was set.
        def reap(finished: asyncio.Task[subprocess.Popen[Any]]) -> None:
            if not finished.cancelled() and finished.exception() is None:
                process = finished.result()
                _track_reaper(asyncio.create_task(asyncio.to_thread(terminate, process)))

        task.add_done_callback(reap)
        raise


async def run_command(
    command: list[str],
    *,
    timeout_seconds: float,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    def terminate(process: subprocess.Popen[Any]) -> None:
        terminate_process(process, tree=True)

    process = await launch_owned(
        lambda: subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        ),
        terminate=terminate,
    )
    try:
        stdout, stderr = await bounded(
            asyncio.to_thread(process.communicate),
            timeout_seconds=timeout_seconds,
            label=f"gateway command {command[0]}",
        )
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout=(stdout or b"").decode("utf-8", "replace"),
            stderr=(stderr or b"").decode("utf-8", "replace"),
        )
    finally:
        if process.poll() is None:
            await asyncio.to_thread(terminate, process)


class GatewayFiles:
    """Remember newly created directories until login commits the instance."""

    def __init__(self) -> None:
        self._new_directories: dict[str, Path] = {}

    def prepare_directory(self, instance_id: str, target: Path) -> None:
        if not target.exists():
            self._new_directories[instance_id] = target.resolve()
        target.mkdir(parents=True, exist_ok=True)

    async def cleanup_failed(self, instance_id: str) -> None:
        target = self._new_directories.get(instance_id)
        if target is not None:
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(shutil.rmtree, target)
            self._new_directories.pop(instance_id, None)

    def commit_instance(self, instance_id: str) -> None:
        self._new_directories.pop(instance_id, None)


class LocalProcessGateway(GatewayFiles):
    provider = ""

    def __init__(
        self, directory: Callable[[str], Path], default_port: Callable[[str], int]
    ) -> None:
        super().__init__()
        self._directory = directory
        self._default_port = default_port
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._process_options: dict[str, dict[str, Any]] = {}

    def _port(self, instance_id: str) -> int:
        return int(
            read_metadata(self._directory(instance_id)).get("http_port")
            or self._default_port(instance_id)
        )

    def _endpoint(self, instance_id: str) -> str:
        return f"http://127.0.0.1:{self._port(instance_id)}"

    @staticmethod
    async def _wait_http_port(port: int, wait_seconds: float = 2.0) -> bool:
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"http://127.0.0.1:{port}/health")
                if response.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
        return False

    async def start_process(
        self,
        instance_id: str,
        options: dict[str, Any],
        command: list[str],
        *,
        timeout_seconds: float,
    ) -> GatewayInstance:
        process = self._processes.get(instance_id)
        persistent_options = {
            key: value for key, value in options.items() if not key.startswith("_")
        }
        if (
            process is not None
            and process.poll() is None
            and self._process_options.get(instance_id) != persistent_options
        ):
            await self.stop(instance_id)
            process = None
        if process is not None and process.poll() is None:
            return GatewayInstance(
                provider=self.provider,
                instance_id=instance_id,
                status="running",
                endpoint=self._endpoint(instance_id),
                extra={"port": self._port(instance_id), "reused": True},
            )
        target = self._directory(instance_id).resolve()
        preferred = int(options.get("port") or self._port(instance_id))
        if await self._wait_http_port(preferred, wait_seconds=0.2):
            raise RuntimeError(
                f"{self.provider} {instance_id}: port {preferred} is in use by an unmanaged process"
            )
        port = allocate_port(target, "http_port", preferred, explicit=bool(options.get("port")))
        endpoint = self._endpoint(instance_id)
        log_file = target / f"{self.provider}.log"
        env = {
            **os.environ,
            **dict(options.get("env") or {}),
            "GATEWAY_PORT": str(port),
            "MAILFLOW_BOT_URL": str(options.get("bot_url") or ""),
            "MAILFLOW_PROVIDER": self.provider,
            "MAILFLOW_INSTANCE": instance_id,
        }

        def launch() -> subprocess.Popen[Any]:
            with log_file.open("ab") as handle:
                return subprocess.Popen(
                    command, cwd=str(target), env=env, stdout=handle, stderr=subprocess.STDOUT
                )

        try:
            process = await launch_owned(launch)
            self._processes[instance_id] = process
            self._process_options[instance_id] = persistent_options
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"{self.provider} {instance_id} exited early (code {process.returncode}); see {log_file}{self._tail_log(log_file)}"
                    )
                if await self._wait_http_port(port, wait_seconds=0.5):
                    return GatewayInstance(
                        provider=self.provider,
                        instance_id=instance_id,
                        status="running",
                        endpoint=endpoint,
                        extra={"port": port, "pid": process.pid},
                    )
                await asyncio.sleep(0.1)
            raise RuntimeError(
                f"{self.provider} {instance_id} did not answer on {endpoint} in {timeout_seconds:g}s; see {log_file}{self._tail_log(log_file)}"
            )
        except BaseException:
            await self.stop(instance_id)
            raise

    async def stop(self, instance_id: str) -> None:
        process = self._processes.get(instance_id)
        if process is not None:
            await asyncio.to_thread(terminate_process, process)
            self._processes.pop(instance_id, None)
            self._process_options.pop(instance_id, None)

    async def status(self, instance_id: str) -> GatewayInstance:
        process = self._processes.get(instance_id)
        alive = process is not None and process.poll() is None
        healthy = alive and await self._wait_http_port(self._port(instance_id), wait_seconds=0.5)
        return GatewayInstance(
            provider=self.provider,
            instance_id=instance_id,
            status="running" if healthy else "starting" if alive else "stopped",
            endpoint=self._endpoint(instance_id),
            error="" if healthy else "bridge not answering",
        )

    async def qr(self, instance_id: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(f"{self._endpoint(instance_id)}/qr")
                response.raise_for_status()
                payload: Any = response.json()
            if payload.get("status") == "logged_in":
                return LOGGED_IN
            if payload.get("status") == "error":
                return f"ERROR: {payload.get('error') or 'bridge failed'}"
            return str(payload.get("qrcode") or "")
        except Exception as exc:
            logger.warning("%s %s QR failed: %s", self.provider, instance_id, exc)
            return f"ERROR: {self.provider} {instance_id}: {exc}"

    @staticmethod
    def _tail_log(log_file: Path) -> str:
        with contextlib.suppress(OSError):
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]
            return "\n  log: " + "\n  log: ".join(lines) if lines else ""
        return ""
