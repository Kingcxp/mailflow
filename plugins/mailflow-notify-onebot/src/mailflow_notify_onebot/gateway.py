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
# Linux QQ (NTQQ) deb; headless containers run it under xvfb. The URL is a
# known rolling build; override with options.qq_deb_url if it moves.
_QQ_DEB_URL = "https://dldir1.qq.com/qqfile/qq/QQNT/Linux/QQ_3.2.15_240902_x86_64_01.deb"
_QQ_INSTALL_DIR = Path("/opt/QQ")
_WEBUI_PORT = 6099
_QR_LOGGED_IN = "__MAILFLOW_LOGGED_IN__"
_BASE_PORT = 3000
_READY_TIMEOUT = 30.0

_latest_version: str | None = None


def _release_url(version: str) -> str:
    return f"https://github.com/NapNeko/NapCatQQ/releases/download/v{version}/{_NAPCAT_ASSET}"


def _data_root() -> Path:
    """Gateway data root; mirrors the storage db directory layout."""
    # resolve from the current working directory like the rest of the app
    return Path("data") / "gateways"


def _available_memory_mb() -> float | None:
    """Free system memory in MB (Linux /proc/meminfo); None when unknown."""
    import os

    if os.name != "nt":
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024.0
        except OSError:
            return None
    return None


def _path_exists(path: Path) -> bool:
    """Sync existence check (Path.exists in async functions trips ASYNC240)."""
    return path.exists()


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


def _detect_qq() -> str | None:
    """Path to a local QQ client (NapCat injects into it).

    Windows: registry UninstallString like the official launcher.bat, or
    the QQ process path. Linux: qq / linuxqq on PATH. None when no QQ
    client is found — NapCat cannot work without one.
    """
    import os
    import shutil

    if os.name == "nt":
        try:
            # winreg is Windows-only; import dynamically so mypy on Linux
            # (where the module has no stub) does not fail
            import importlib

            winreg = importlib.import_module("winreg")

            keys = [
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
            ]
            for key in keys:
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                        value, _ = winreg.QueryValueEx(handle, "UninstallString")
                    path = str(value)
                    # UninstallString points into the QQ install dir
                    import re as _re

                    m = _re.search(r"(?i)([A-Za-z]:\\[^\\]*)", path)
                    if m:
                        candidate = Path(m.group(1)) / "QQ.exe"
                        if candidate.exists():
                            return str(candidate)
                except OSError:
                    continue
        except Exception:
            pass
        # fall back to the running QQ process
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-Process QQ -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Path)",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            path = (result.stdout or "").strip()
            if path and Path(path).exists():
                return path
        except Exception:
            pass
        return None
    # POSIX: look for the Linux QQ binaries
    return shutil.which("qq") or shutil.which("linuxqq")


class NapCatProvisioner:
    """OneBot v11 gateway backed by a local NapCat process."""

    backend_id = "napcat"

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._webui_ports: dict[str, int] = {}

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
        import os

        node = _find_node()
        if node is None:
            raise RuntimeError(
                "NapCat needs Node.js >= 18; install Node first (or set options.node_path)"
            )
        target = _instance_dir(instance_id)
        # installed means MailFlow's own copy (data/gateways) is in place;
        # Linux additionally mirrors it into QQ's app dir during install.
        # data/gateways is the source of truth so make clean-gateways
        # forces a full reinstall (QQ alone must not count as installed).
        if await asyncio.to_thread(
            lambda: _path_exists(target / "napcat.mjs") or _path_exists(target / "main")
        ):
            logger.info("napcat %s already installed at %s", instance_id, target)
            return
        # a leftover directory without the real entry point means a broken
        # or partial install: wipe it so the fresh download starts clean
        # (covers the case where data/gateways was half-removed)
        if await asyncio.to_thread(lambda: _path_exists(target)):
            logger.warning(
                "napcat %s: %s exists but is incomplete — removing it for a clean reinstall",
                instance_id,
                target,
            )
            await asyncio.to_thread(shutil.rmtree, target, True)
        target.mkdir(parents=True, exist_ok=True)
        await self._install_napcat_package(instance_id, options, target)
        if os.name != "nt":
            # Linux headless: verify the runtime (xvfb + QQ deb), mirror
            # NapCat into QQ's resources/app/napcat and patch the app
            # entry point (official BootWay03 flow).
            await self._install_linux_qq(instance_id, options, target)

    async def _install_napcat_package(
        self, instance_id: str, options: dict[str, Any], target: Path
    ) -> None:
        """Download + unpack the NapCat.Shell package into ``target``."""
        requested = str(options.get("napcat_version") or "").strip()
        if requested == "latest":
            version = _latest_napcat_version()
        else:
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

    async def _install_linux_qq(
        self, instance_id: str, options: dict[str, Any], target: Path
    ) -> None:
        """Verify the Linux runtime (xvfb + QQ deb) and mirror the already
        downloaded NapCat package into QQ's app dir.

        MailFlow never installs system packages itself: missing runtime
        pieces are reported with the exact install command so the operator
        can provision them (apt in a container, distro packages elsewhere).
        """
        import shutil as _sh

        missing: list[str] = []
        if _sh.which("xvfb-run") is None:
            missing.append("xvfb-run (apt install xvfb xauth)")
        if not _path_exists(_QQ_INSTALL_DIR / "qq"):
            missing.append(f"Linux QQ at {_QQ_INSTALL_DIR / 'qq'} (install the QQ deb)")
        if missing:
            raise RuntimeError(
                "NapCat on Linux needs these missing pieces — install them "
                "first, then retry:\n  - "
                + "\n  - ".join(missing)
                + "\n(example: apt-get install -y xvfb xauth, then dpkg -i QQ_*.deb)"
            )
        # mirror the freshly unpacked package into QQ's app dir and patch
        # the app entry point; QQ's tree is root-owned, so guard the whole
        # block with actionable permission guidance
        qq_app = _QQ_INSTALL_DIR / "resources" / "app"
        napcat_dir = qq_app / "napcat"
        try:
            if not _path_exists(napcat_dir / "napcat.mjs"):
                qq_app.mkdir(parents=True, exist_ok=True)
                if napcat_dir.exists():
                    shutil.rmtree(napcat_dir, ignore_errors=True)
                shutil.copytree(target, napcat_dir, dirs_exist_ok=True)
                logger.info("napcat %s: NapCat mirrored to %s", instance_id, napcat_dir)
            # write the loader that imports napcat.mjs when --no-sandbox is passed
            loader = qq_app / "loadNapCat.cjs"
            loader.write_text(
                'const path = require("path");\n'
                "const CurrentPath = path.dirname(__filename);\n"
                'const hasNapcatParam = process.argv.includes("--no-sandbox");\n'
                "if (hasNapcatParam) {\n"
                '  (async () => { await import("file://" + path.join(CurrentPath, "./napcat/napcat.mjs")); })();\n'
                "} else {\n"
                '  require("./application/app_launcher/index.js");\n'
                "}\n",
                encoding="utf-8",
            )
            # patch package.json main -> loadNapCat.cjs (BootWay03)
            pkg = qq_app / "package.json"
            if _path_exists(pkg):
                text = pkg.read_text(encoding="utf-8", errors="replace")
                if "loadNapCat.cjs" not in text:
                    import re as _re

                    patched = _re.sub(
                        r'"main"\s*:\s*"[^"]*"',
                        '"main": "./loadNapCat.cjs"',
                        text,
                        count=1,
                    )
                    pkg.write_text(patched, encoding="utf-8")
                    logger.info("napcat %s: patched QQ package.json main", instance_id)
        except PermissionError as exc:
            raise RuntimeError(
                f"cannot write to {qq_app} (permission denied). NapCat must "
                "live inside QQ's app directory, which is owned by root. "
                "Fix with one of:\n"
                "  - run MailFlow as root:  sudo <your mailflow command>\n"
                "  - grant your user write access:  "
                "sudo chown -R $USER /opt/QQ/resources/app\n"
                "  - or make the tree writable:  "
                "sudo chmod -R a+rw /opt/QQ/resources/app"
            ) from exc
        logger.info("napcat %s: Linux QQ + NapCat ready", instance_id)

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
        NapCat.Shell/main.js — try them in order. Returns an ABSOLUTE
        path: node resolves a relative script against the child's cwd,
        which would double the data/gateways prefix.
        """
        base = target.resolve()
        candidates = [
            base / "napcat.mjs",
            base / "loadNapCat.js",
            base / "main.js",
            base / "NapCat.Shell" / "main.js",
            base / "bin" / "main.js",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise RuntimeError(
            f"napcat {instance_id}: no entry point found under {target} — "
            "the install is incomplete or the gateway directory was "
            "partially removed; delete data/gateways/napcat-* and retry "
            "the setup to reinstall"
        )

    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
        import os

        node = _find_node()
        if node is None:
            raise RuntimeError("NapCat needs Node.js >= 18; install Node first")
        if os.name == "nt":
            qq = _detect_qq()
            if qq is None:
                raise RuntimeError(
                    "NapCat needs a local QQ client to log in (it injects "
                    "into QQ's process), but none was found on this machine. "
                    "Install QQ for your platform first."
                )
        elif not _path_exists(_QQ_INSTALL_DIR / "qq"):
            raise RuntimeError(
                "NapCat is not installed on this Linux host: run the "
                "auto-install first (it installs Linux QQ + xvfb + NapCat)."
            )
        else:
            # NapCat launches the full QQ NT client (Electron): it needs
            # roughly 1.5-2 GB of RAM. Refuse to start on hosts without
            # enough free memory instead of letting the OOM killer take
            # down the whole VM.
            mem_mb = _available_memory_mb()
            if mem_mb is not None and mem_mb < 1500:
                raise RuntimeError(
                    f"NapCat needs ~1.5-2 GB of free RAM (it runs the full "
                    f"QQ client), but this host has only {mem_mb:.0f} MB "
                    "available. Free memory or pick a lighter platform "
                    "(e.g. WeChaty) on this machine."
                )
        # the runnable NapCat lives in QQ's app dir on Linux (installed by
        # the BootWay03 flow) and in data/gateways on Windows
        run_target = (
            _QQ_INSTALL_DIR / "resources" / "app" / "napcat"
            if os.name != "nt"
            else _instance_dir(instance_id)
        )
        if not _path_exists(run_target / "napcat.mjs"):
            raise RuntimeError(
                f"napcat {instance_id} is not installed (no napcat.mjs under "
                f"{run_target}); run the setup again to install it"
            )
        target = run_target
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
        entry: Path | None = await asyncio.to_thread(self._find_entry, target, instance_id)
        assert entry is not None
        env = dict(options.get("env") or {})
        env.setdefault("NAPCAT_UID", instance_id)
        env.setdefault("NAPCAT_PORT", str(port))
        # NapCat writes its login QR to <workdir>/cache/qrcode.png; pin the
        # workdir to the instance dir so qr() can find and refresh it
        env.setdefault("NAPCAT_WORKDIR", str(target))
        log_file = target / "napcat.log"

        def _launch() -> subprocess.Popen[Any]:
            with open(log_file, "ab") as handle:
                if os.name == "nt":
                    # Windows with a QQ client: inject like the official
                    # launcher.bat (NapCatWinBootMain.exe starts QQ with
                    # the hook), the only mode where NapCat's worker runs.
                    assert qq is not None
                    boot_main = target / "NapCatWinBootMain.exe"
                    hook = target / "NapCatWinBootHook.dll"
                    env.setdefault("NAPCAT_PATCH_PACKAGE", str(target / "qqnt.json"))
                    env.setdefault("NAPCAT_LOAD_PATH", str(target / "loadNapCat.js"))
                    env.setdefault("NAPCAT_INJECT_PATH", str(hook))
                    env.setdefault("NAPCAT_LAUNCHER_PATH", str(boot_main))
                    env.setdefault("NAPCAT_MAIN_PATH", str(entry))
                    command = [str(boot_main), qq, str(hook)]
                    cwd = str(target)
                else:
                    # Linux headless: run the patched QQ under xvfb with
                    # --no-sandbox; loadNapCat.cjs imports napcat.mjs.
                    command = ["xvfb-run", "-a", str(_QQ_INSTALL_DIR / "qq"), "--no-sandbox"]
                    cwd = str(_QQ_INSTALL_DIR)
                return subprocess.Popen(
                    command,
                    cwd=cwd,
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
        webui_port = int(options.get("webui_port") or _WEBUI_PORT)
        # NapCat auto-increments the WebUI port when it is taken; scan a
        # small window instead of hard-failing on the configured one
        ready_port = webui_port
        ready = False
        for candidate in range(webui_port, webui_port + 5):
            if await self._wait_http_port(candidate, wait_seconds=4.0):
                ready_port = candidate
                ready = True
                break
        endpoint = self._endpoint(instance_id)
        if not ready:
            log_tail = self._tail_log(log_file)
            self._terminate(instance_id)
            raise RuntimeError(
                f"napcat {instance_id} started but its WebUI did not answer on "
                f"http://127.0.0.1:{webui_port} (scanned +0..+4) in "
                f"{_READY_TIMEOUT:.0f}s; see {log_file}{log_tail}"
            )
        self._webui_ports[instance_id] = ready_port
        logger.info(
            "napcat %s: WebUI up on :%d (OneBot HTTP on :%d after the QQ session logs in)",
            instance_id,
            ready_port,
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
        """Login state for the guide.

        NapCat writes its login QR to ``<workdir>/cache/qrcode.png`` (see
        NAPCAT_WORKDIR) and refreshes it automatically when it expires.
        Login is decided by the OneBot ``get_login_info`` probe (a user_id
        means logged in), never by the presence/absence of a QR file.
        Returns the base64 PNG (data URL stripped), the logged-in sentinel,
        or '' when the QR file does not exist yet."""
        target = _instance_dir(instance_id)
        qr_file = target / "cache" / "qrcode.png"
        # logged in? -> sentinel; a failed probe just means not yet ready
        logged_in = False
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                login = await client.post(f"{self._endpoint(instance_id)}/get_login_info", json={})
                login.raise_for_status()
                login_data: Any = login.json().get("data") or {}
                logged_in = bool(login_data.get("user_id"))
        except Exception:
            logged_in = False
        if logged_in:
            return _QR_LOGGED_IN
        # not logged in: the QR png (refreshed by NapCat on expiry)
        try:
            raw = qr_file.read_bytes()
        except OSError:
            # diagnose why the QR is missing: log the expected path and a
            # snippet of the NapCat log so the guide can show the reason
            tail = self._tail_log(_instance_dir(instance_id) / "napcat.log", lines=5)
            logger.info(
                "napcat %s: QR not ready yet (%s missing)%s",
                instance_id,
                qr_file,
                tail,
            )
            return ""
        if len(raw) < 100:
            logger.info("napcat %s: QR file exists but is empty/short (%d bytes)", instance_id, len(raw))
            return ""
        import base64 as _b64

        return _b64.b64encode(raw).decode("ascii")


__all__ = ["NapCatProvisioner", "_find_node"]
