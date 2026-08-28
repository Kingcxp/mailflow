"""NapCat gateway provisioner: auto-install, launch and supervise a NapCat
QQ bot (OneBot v11 HTTP) for the Bots tab.

Install model:
- NapCat is a Node.js app distributed as a release zip (GitHub). We pin a
  version, download the zip into ``<data>/gateways/napcat-<instance>/``,
  unpack it, and run it with the system ``node``.
- Each instance gets its own directory and its own HTTP port (base 3000 +
  instance offset), so "Add" always starts an independent bot session.
- The QR login is driven through the OneBot HTTP API (``get_qrcode`` +
  ``get_login_info`` polling); the TUI renders the payload.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import httpx
from mailflow.contracts import GatewayInstance

logger = logging.getLogger("mailflow.gateway.napcat")

# Default NapCat release: pinned to a known-good version so installs never
# depend on the GitHub API (anonymous rate limit is 60 req/hr and can be
# exhausted by retries/multiple sessions). Set options.napcat_version to a
# specific tag, or to "latest" to resolve from the GitHub API at install
# time.
_NAPCAT_VERSION = "4.18.19"
_NAPCAT_ASSET = "NapCat.Shell.zip"
_NAPCAT_API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
_NAPCAT_SIZE_MB = 29.5  # approximate; the log is informational
_BASE_PORT = 3000
_READY_TIMEOUT = 30.0

_latest_version: str | None = None


def _release_url(version: str) -> str:
    return f"https://github.com/NapNeko/NapCatQQ/releases/download/v{version}/{_NAPCAT_ASSET}"


def _data_root() -> Path:
    """Gateway data root; mirrors the storage db directory layout."""
    # resolve from the current working directory like the rest of the app
    return Path("data") / "gateways"


def _safe_token(instance_id: str) -> str:
    """Instance id -> filesystem/port-safe token.

    Instance ids are user-chosen and may contain spaces, parentheses or
    other characters that break directory names and integer parsing; map
    every non [A-Za-z0-9_-] char to '-', then append a short hash of the
    original so two ids that collapse to the same token stay distinct.
    """
    import hashlib

    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "-", instance_id).strip("-") or "gw"
    digest = hashlib.sha1(instance_id.encode("utf-8")).hexdigest()[:6]
    return f"{cleaned}-{digest}"


def _latest_napcat_version() -> str:
    """Latest NapCat release tag (cached per process); raises with a clear
    message when the GitHub API is unreachable or rate-limited."""
    global _latest_version
    if _latest_version:
        return _latest_version
    request = urllib.request.Request(
        _NAPCAT_API, headers={"User-Agent": "mailflow", "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"could not look up the latest NapCat release ({exc}); "
            f"the default pinned version {_NAPCAT_VERSION} is used instead "
            "— remove options.napcat_version or pin a specific tag"
        ) from exc
    tag = str(payload.get("tag_name", "")).lstrip("v")
    if not tag:
        raise RuntimeError("NapCat release lookup returned no version tag")
    _latest_version = tag
    return tag


def _instance_dir(instance_id: str) -> Path:
    return _data_root() / f"napcat-{_safe_token(instance_id)}"


def _find_node() -> str | None:
    """Path to a usable ``node`` binary (NapCat needs Node >= 18)."""
    node = shutil.which("node")
    if node is None:
        return None
    try:
        result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    version = (result.stdout or "").strip().lstrip("v")
    try:
        major = int(version.split(".")[0])
    except ValueError:
        return None
    return node if major >= 18 else None


class NapCatProvisioner:
    """OneBot v11 gateway backed by a local NapCat process."""

    backend_id = "napcat"

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen[Any]] = {}

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _port_for(instance_id: str) -> int:
        # deterministic per instance id: stable across restarts
        try:
            suffix = int(_safe_token(instance_id).split("-")[-2])
        except (ValueError, IndexError):
            suffix = 0
        return _BASE_PORT + (suffix % 100)

    def _endpoint(self, instance_id: str) -> str:
        return f"http://127.0.0.1:{self._port_for(instance_id)}"

    async def _wait_http(self, instance_id: str, wait_seconds: float = _READY_TIMEOUT) -> bool:
        url = self._endpoint(instance_id)
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    response = await client.get(url)
                if response.status_code < 500:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.0)
        return False

    # -- GatewayProvisioner ----------------------------------------------------

    async def detect(self) -> str:
        node = _find_node()
        if node is None:
            return "node not found (NapCat needs Node.js >= 18)"
        installed = any(_instance_dir(p.name) for p in _data_root().glob("napcat-*"))
        running = await self._any_running()
        parts = [f"node {node}"]
        parts.append("installed" if installed else "not installed")
        parts.append("running" if running else "not running")
        return "; ".join(parts)

    async def _any_running(self) -> bool:
        # the directory name is the sanitized token (not the original id);
        # probe each instance's HTTP port by re-deriving it from the token
        for directory in _data_root().glob("napcat-*"):
            if not directory.is_dir():
                continue
            token = directory.name[len("napcat-") :]
            try:
                suffix = int(token.split("-")[-2])
            except (ValueError, IndexError):
                continue
            port = _BASE_PORT + (suffix % 100)
            if await self._wait_http_port(port, wait_seconds=2.0):
                return True
        return False

    @staticmethod
    async def _wait_http_port(port: int, wait_seconds: float = 2.0) -> bool:
        url = f"http://127.0.0.1:{port}"
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(url)
                if response.status_code < 500:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.3)
        return False

    async def install(self, instance_id: str, options: dict[str, Any]) -> None:
        node = _find_node()
        if node is None:
            raise RuntimeError(
                "NapCat needs Node.js >= 18; install Node first (or set options.node_path)"
            )
        target = _instance_dir(instance_id)
        if (target / "package.json").exists() or (target / "main").exists():
            logger.info("napcat %s already installed at %s", instance_id, target)
            return
        target.mkdir(parents=True, exist_ok=True)
        requested = str(options.get("napcat_version") or "").strip()
        if requested == "latest":
            version = _latest_napcat_version()
        else:
            # pinned default (or an explicit tag): no GitHub API call, so
            # installs cannot hit the anonymous rate limit
            version = requested or _NAPCAT_VERSION
        url = _release_url(version)
        archive = target / "napcat.zip"
        logger.info("napcat %s: downloading %s (%.1f MB)", instance_id, url, _NAPCAT_SIZE_MB)
        await asyncio.to_thread(self._download, url, archive)
        logger.info(
            "napcat %s: downloaded %d bytes; unpacking…",
            instance_id,
            archive.stat().st_size if archive.exists() else 0,
        )
        try:
            with zipfile.ZipFile(archive) as zf:
                entries = zf.namelist()
                zf.extractall(target)
        finally:
            archive.unlink(missing_ok=True)
        logger.info("napcat %s: installed %d files at %s", instance_id, len(entries), target)

    @staticmethod
    def _download(url: str, destination: Path) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "mailflow"})
        try:
            with (
                urllib.request.urlopen(request, timeout=120) as response,
                open(destination, "wb") as handle,
            ):
                shutil.copyfileobj(response, handle)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"download failed: HTTP {exc.code} {exc.reason} for {url}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"download failed: {exc.reason} for {url}") from exc
        except OSError as exc:
            raise RuntimeError(f"download failed: {exc} for {url}") from exc

    @staticmethod
    def _tail_log(log_file: Path, lines: int = 8) -> str:
        """Last N log lines, formatted for error messages ('' when empty)."""
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = content[-lines:]
            return "\n  log: " + "\n  log: ".join(tail) if tail else ""
        except OSError:
            return ""

    @staticmethod
    def _find_entry(target: Path, instance_id: str) -> Path:
        """Locate the NapCat entry point inside the unpacked tree.

        NapCat.Shell ships `napcat.mjs` at the root (with package.json
        next to it); older layouts used main.js or a nested
        NapCat.Shell/main.js — try them in order.
        """
        candidates = [
            target / "napcat.mjs",
            target / "loadNapCat.js",
            target / "main.js",
            target / "NapCat.Shell" / "main.js",
            target / "bin" / "main.js",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise RuntimeError(f"napcat {instance_id}: no entry point found under {target}")

    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
        node = _find_node()
        if node is None:
            raise RuntimeError("NapCat needs Node.js >= 18; install Node first")
        target = _instance_dir(instance_id)
        if not target.exists():
            raise RuntimeError(f"napcat {instance_id} is not installed")
        port = int(options.get("port") or self._port_for(instance_id))
        # already running on this port (another instance or a self-hosted
        # NapCat)? reuse it instead of starting a conflicting process
        if await self._wait_http_port(port, wait_seconds=1.0):
            logger.info("napcat %s: reusing already-running gateway on :%d", instance_id, port)
            return GatewayInstance(
                provider="napcat",
                instance_id=instance_id,
                status="running",
                endpoint=f"http://127.0.0.1:{port}",
                extra={"port": port, "reused": True},
            )
        # config for NapCat: HTTP server on the instance port
        config_dir = target / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        http_config = config_dir / "onebot11_http.json"
        http_config.write_text(
            json.dumps(
                {
                    "network": {
                        "httpServers": [
                            {
                                "enable": True,
                                "port": port,
                                "host": "127.0.0.1",
                                "enableCors": False,
                                "enableWebsocket": False,
                                "token": "",
                                "debug": False,
                            }
                        ]
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        # launch: NapCat shell entry. Run as a standalone node process
        # (NAPCAT_FORCE_NODE_PROCESS) so no QQ injection is required to
        # start; stdout/stderr go to a per-instance log file so startup
        # failures are diagnosable instead of invisible.
        entry: Path | None = await asyncio.to_thread(self._find_entry, target, instance_id)
        assert entry is not None
        env = dict(options.get("env") or {})
        env.setdefault("NAPCAT_UID", instance_id)
        env.setdefault("NAPCAT_PORT", str(port))
        env.setdefault("NAPCAT_FORCE_NODE_PROCESS", "1")
        log_file = target / "napcat.log"

        def _launch() -> subprocess.Popen[Any]:
            with open(log_file, "ab") as handle:
                # the child inherits the fd; closing the parent copy is fine
                return subprocess.Popen(
                    [node, str(entry)],
                    cwd=str(entry.parent),
                    env={**__import__("os").environ, **env},
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )

        try:
            process = await asyncio.to_thread(_launch)
        except OSError as exc:
            raise RuntimeError(f"failed to launch napcat {instance_id}: {exc}") from exc
        self._processes[instance_id] = process
        # the OneBot HTTP API only listens after the QQ session logs in;
        # wait for the process being alive + the WebUI port (6099) as the
        # readiness signal, and report the login requirement clearly
        webui_port = int(options.get("webui_port") or 6099)
        ready = await self._wait_http_port(webui_port, wait_seconds=_READY_TIMEOUT)
        endpoint = self._endpoint(instance_id)
        if not ready:
            log_tail = self._tail_log(log_file)
            self._terminate(instance_id)
            raise RuntimeError(
                f"napcat {instance_id} started but its WebUI did not answer on "
                f"http://127.0.0.1:{webui_port} in {_READY_TIMEOUT:.0f}s; "
                f"see {log_file}{log_tail}"
            )
        logger.info(
            "napcat %s: WebUI up on :%d (OneBot HTTP on :%d after the QQ session logs in)",
            instance_id,
            webui_port,
            port,
        )
        return GatewayInstance(
            provider="napcat",
            instance_id=instance_id,
            status="running",
            endpoint=endpoint,
            extra={"port": port, "pid": process.pid},
        )

    def _terminate(self, instance_id: str) -> None:
        process = self._processes.pop(instance_id, None)
        if process is None:
            return
        if process.poll() is None:
            with __import__("contextlib").suppress(Exception):
                process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                with __import__("contextlib").suppress(Exception):
                    process.kill()

    async def stop(self, instance_id: str) -> None:
        self._terminate(instance_id)

    async def status(self, instance_id: str) -> GatewayInstance:
        endpoint = self._endpoint(instance_id)
        running = await self._wait_http(instance_id, wait_seconds=2.0)
        if running:
            return GatewayInstance(
                provider="napcat",
                instance_id=instance_id,
                status="running",
                endpoint=endpoint,
            )
        process = self._processes.get(instance_id)
        if process is not None and process.poll() is None:
            return GatewayInstance(
                provider="napcat",
                instance_id=instance_id,
                status="starting",
                error="HTTP not answering yet",
                endpoint=endpoint,
            )
        return GatewayInstance(
            provider="napcat",
            instance_id=instance_id,
            status="stopped",
            error="process not running",
        )

    async def qr(self, instance_id: str) -> str:
        """Ask the running NapCat for its login QR (base64 PNG)."""
        endpoint = self._endpoint(instance_id)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(f"{endpoint}/get_qrcode", json={})
                response.raise_for_status()
                payload: Any = response.json().get("data") or {}
                image = payload.get("qrcode") or payload.get("image") or ""
                return str(image)
        except Exception as exc:
            logger.warning("napcat %s get_qrcode failed: %s", instance_id, exc)
            return ""


__all__ = ["NapCatProvisioner", "_find_node"]
