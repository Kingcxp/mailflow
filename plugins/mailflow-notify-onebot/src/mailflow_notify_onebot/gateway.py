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
import os
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
# module-level so tests can flip the host platform
_IS_WINDOWS = os.name == "nt"

_NAPCAT_VERSION = "4.18.19"
_NAPCAT_ASSET = "NapCat.Shell.zip"
_NAPCAT_API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
_NAPCAT_SIZE_MB = 29.5  # approximate; the log is informational
# Linux headless: the official AppImage bundles QQ NT + NapCat in one
# file (only xvfb + fuse needed at runtime), so no QQ deb install or
# mirroring into /opt/QQ is required. The Shell zip stays the Windows
# path. Override with options.napcat_appimage_url if it moves.
_NAPCAT_APPIMAGE_API = "https://api.github.com/repos/NapNeko/NapCatAppImageBuild/releases/latest"
_NAPCAT_APPIMAGE_ASSET = "QQ-50969_NapCat-v4.18.19-amd64.AppImage"
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

    if not _IS_WINDOWS:
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


def _installed_marker(target: Path) -> bool:
    """True when ``target`` holds a usable NapCat install for this host.

    Windows: the Shell package entry point. Linux: any bundled AppImage
    (the asset name changes with each QQ/NapCat release, so glob rather
    than pin the exact filename).
    """

    if not _IS_WINDOWS:
        return any(p.suffix == ".AppImage" for p in target.glob("*.AppImage"))
    return _path_exists(target / "napcat.mjs") or _path_exists(target / "main")


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
    import shutil

    if _IS_WINDOWS:
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

        node = _find_node()
        if node is None:
            raise RuntimeError(
                "NapCat needs Node.js >= 18; install Node first (or set options.node_path)"
            )
        target = _instance_dir(instance_id)
        # installed means MailFlow's own copy (data/gateways) is in place:
        # the Shell zip entry point on Windows, or a bundled AppImage on
        # Linux. data/gateways is the source of truth so make clean-gateways
        # forces a full reinstall.
        if await asyncio.to_thread(_installed_marker, target):
            logger.info("napcat %s already installed at %s", instance_id, target)
            return
        # a leftover directory without the real entry point means a broken
        # or partial install: wipe it so the fresh download starts clean
        # (covers the case where data/gateways was half-removed, and old
        # QQ-deb installs that predate the AppImage flow)
        if await asyncio.to_thread(lambda: _path_exists(target)):
            logger.warning(
                "napcat %s: %s exists but is incomplete — removing it for a clean reinstall",
                instance_id,
                target,
            )
            await asyncio.to_thread(shutil.rmtree, target, True)
        target.mkdir(parents=True, exist_ok=True)
        if not _IS_WINDOWS:
            # Linux headless: download the official AppImage (bundles
            # QQ NT + NapCat); no QQ deb, no /opt/QQ mirroring
            await self._install_linux_appimage(instance_id, options, target)
        else:
            await self._install_napcat_package(instance_id, options, target)

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
        progress = options.get("_progress")
        logger.info("napcat %s: downloading %s (%.1f MB)", instance_id, url, _NAPCAT_SIZE_MB)
        await asyncio.to_thread(self._download, url, archive, progress)
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

    async def _install_linux_appimage(
        self, instance_id: str, options: dict[str, Any], target: Path
    ) -> None:
        """Download the official NapCat AppImage (bundles QQ NT + NapCat).

        MailFlow never installs system packages itself: missing runtime
        pieces (xvfb, fuse) are reported with the exact install command
        so the operator can provision them.
        """
        import shutil as _sh

        missing: list[str] = []
        if _sh.which("xvfb-run") is None:
            missing.append("xvfb-run (apt install xvfb xauth)")
        if missing:
            raise RuntimeError(
                "NapCat AppImage on Linux needs these missing pieces — "
                "install them first, then retry:\n  - "
                + "\n  - ".join(missing)
                + "\n(example: apt-get install -y xvfb xauth)"
            )
        if _sh.which("fusermount") is None:
            # AppImages normally need FUSE to mount; containers/VMs often
            # cannot modprobe fuse, so we launch with
            # --appimage-extract-and-run which unpacks to a temp dir and
            # needs no FUSE at all
            logger.warning(
                "napcat %s: fusermount not found — the AppImage will run "
                "in extract mode (no FUSE needed)",
                instance_id,
            )
        # resolve the latest amd64 AppImage asset name from the release
        # API; when the API is unreachable we fall back to the pinned
        # known release below
        requested = str(options.get("napcat_appimage_url") or "").strip()
        url = requested
        asset = _NAPCAT_APPIMAGE_ASSET
        if not url:
            try:
                import json as _json
                import urllib.request as _ur

                def _lookup() -> tuple[str, str]:
                    req = _ur.Request(_NAPCAT_APPIMAGE_API, headers={"User-Agent": "mailflow"})
                    with _ur.urlopen(req, timeout=30) as resp:
                        data = _json.loads(resp.read().decode("utf-8"))
                    for a in data.get("assets", []):
                        name: str = a["name"]
                        if "amd64" in name and name.endswith(".AppImage"):
                            return str(a["browser_download_url"]), name
                    return "", _NAPCAT_APPIMAGE_ASSET

                resolved = await asyncio.to_thread(_lookup)
                url, asset = resolved
            except Exception as exc:
                logger.warning("napcat %s: AppImage release lookup failed (%s)", instance_id, exc)
        if not url:
            # pinned fallback (same asset the API resolves for this tag);
            # never hard-fail on a transient network/rate-limit issue
            url = (
                "https://github.com/NapNeko/NapCatAppImageBuild/releases/download/"
                f"v{_NAPCAT_VERSION}/{_NAPCAT_APPIMAGE_ASSET}"
            )
            logger.warning(
                "napcat %s: release API unreachable — falling back to the pinned AppImage %s",
                instance_id,
                _NAPCAT_APPIMAGE_ASSET,
            )
        appimage = target / asset
        progress = options.get("_progress")
        if progress is not None:
            progress.update(0.0, f"downloading {asset} (~190 MB)", "downloading")
        logger.info("napcat %s: downloading AppImage %s", instance_id, url)
        await asyncio.to_thread(self._download, url, appimage, progress)
        await asyncio.to_thread(appimage.chmod, 0o755)
        logger.info(
            "napcat %s: AppImage ready (%d MB)",
            instance_id,
            appimage.stat().st_size // 1024 // 1024,
        )
        logger.info("napcat %s: Linux QQ + NapCat ready", instance_id)

    @staticmethod
    def _download(
        url: str,
        destination: Path,
        progress: Any | None = None,
        *,
        stage: str = "downloading",
    ) -> None:
        """Download ``url`` to ``destination``.

        ``progress`` is an InstallProgress-like object with an ``update``
        method (percent, message); the total size comes from the
        Content-Length header when available, otherwise the bar advances
        by bytes seen.
        """
        request = urllib.request.Request(url, headers={"User-Agent": "mailflow"})
        try:
            with (
                urllib.request.urlopen(request, timeout=120) as response,
                open(destination, "wb") as handle,
            ):
                total = int(response.headers.get("Content-Length") or 0)
                seen = 0
                chunk = 64 * 1024
                while True:
                    block = response.read(chunk)
                    if not block:
                        break
                    handle.write(block)
                    seen += len(block)
                    if progress is not None:
                        what = destination.name
                        if total:
                            pct = 100.0 * seen / total
                            mb = seen / (1024 * 1024)
                            progress.update(
                                pct,
                                f"{stage}: {what} — {mb:.0f} / {total / (1024 * 1024):.0f} MB",
                            )
                        else:
                            progress.update(
                                0.0, f"{stage}: {what} — {seen / (1024 * 1024):.0f} MB", stage
                            )
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

        node = _find_node()
        if node is None:
            raise RuntimeError("NapCat needs Node.js >= 18; install Node first")
        qq: str | None = None
        if _IS_WINDOWS:
            qq = _detect_qq()
            if qq is None:
                raise RuntimeError(
                    "NapCat needs a local QQ client to log in (it injects "
                    "into QQ's process), but none was found on this machine. "
                    "Install QQ for your platform first."
                )
        elif not any(
            p.suffix == ".AppImage" for p in _instance_dir(instance_id).glob("*.AppImage")
        ):
            raise RuntimeError(
                "NapCat is not installed on this Linux host: run the "
                "auto-install first (it downloads the official NapCat "
                "AppImage with QQ bundled)."
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
        # the AppImage (Linux) and the Shell package (Windows) both live in
        # the instance dir; on Linux the AppImage holds QQ + NapCat and
        # writes its cache/QR next to itself
        run_target = _instance_dir(instance_id)
        if not _IS_WINDOWS:
            # the AppImage asset name changes with each release: glob it
            appimages = list(run_target.glob("*.AppImage"))
            if not appimages:
                raise RuntimeError(
                    f"napcat {instance_id} is not installed (no .AppImage "
                    f"under {run_target}); run the setup again to install it"
                )
            # absolute path: the child's cwd is the instance dir, so a
            # relative data/gateways/... path would double-prefix and
            # 'not found' at launch
            entry_path = appimages[0].resolve()
        else:
            entry_path = run_target / "napcat.mjs"
            if not _path_exists(entry_path):
                raise RuntimeError(
                    f"napcat {instance_id} is not installed (missing "
                    f"{entry_path}); run the setup again to install it"
                )
            entry_path = entry_path.resolve()
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
        # v4.5.3+ loads ./config/onebot11.json as the default OneBot
        # config; the per-account file is onebot11_<qq>.json. Use the
        # default name (we do not know the QQ number before login) with
        # the full server shape NapCat expects (name + enable required).
        payload = {
            "network": {
                "httpServers": [
                    {
                        "name": "mailflow-http",
                        "enable": True,
                        "port": port,
                        "host": "127.0.0.1",
                        "enableCors": False,
                        "enableWebsocket": False,
                        "messagePostFormat": "array",
                        "token": "",
                        "debug": False,
                    }
                ],
                "httpClients": [],
                "websocketServers": [],
                "websocketClients": [],
            },
            "musicSignUrl": "",
            "enableLocalFile2Url": False,
            "parseMultMsg": False,
        }
        # Write OneBot config to instance dir AND per-user NapCat config dir,
        # ensuring the HTTP server starts after login regardless of platform.
        # On Windows the boot loader targets the instance dir; on Linux the
        # AppImage reads from ~/.config/QQ/NapCat/config/.
        _write_configs = [config_dir]
        if not _IS_WINDOWS:
            import os as _os
            home = _os.environ.get("HOME") or str(Path.home())
            _write_configs.append(Path(home) / ".config" / "QQ" / "NapCat" / "config")
        for _target in _write_configs:
            _target.mkdir(parents=True, exist_ok=True)
            (_target / "onebot11.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
            logger.info("napcat %s: OneBot HTTP config written to %s", instance_id, _target)
        # Linux runs the bundled AppImage directly; only the Windows Shell
        # package needs its node entry point located inside the tree
        if not _IS_WINDOWS:
            entry = entry_path
        else:
            entry = await asyncio.to_thread(self._find_entry, target, instance_id)
        # instance data dir: logs + the QR cache live here (on Linux this
        # differs from the QQ app dir where the package is mirrored)
        data_dir = _instance_dir(instance_id)
        data_dir.mkdir(parents=True, exist_ok=True)
        env = dict(options.get("env") or {})
        env.setdefault("NAPCAT_UID", instance_id)
        env.setdefault("NAPCAT_PORT", str(port))
        # NapCat writes its login QR to <workdir>/cache/qrcode.png; pin the
        # workdir to the instance data dir so qr() can find and refresh it
        env.setdefault("NAPCAT_WORKDIR", str(data_dir))
        log_file = data_dir / "napcat.log"

        def _launch() -> subprocess.Popen[Any]:
            with open(log_file, "ab") as handle:
                if _IS_WINDOWS:
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
                    cwd = str(target.resolve())
                else:
                    # Linux headless: run the AppImage (QQ + NapCat
                    # bundled) under xvfb. The AppImage is self-contained;
                    # its cache and QR land next to the file (cwd).
                    command = [
                        "xvfb-run",
                        "-a",
                        str(entry_path),
                        "--appimage-extract-and-run",
                        "--no-sandbox",
                        # cap the Electron/QQ heap so a low-RAM container
                        # does not get OOM-killed (QQ wants ~1.5-2 GB)
                        "--max-old-space-size=1024",
                    ]
                    cwd = str(run_target.resolve())
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
        # small window. The WebUI may require a token or come up late on
        # slow hosts, so the QR file appearing is also a valid ready
        # signal — the process is alive and the login flow started.
        qr_file = _instance_dir(instance_id) / "cache" / "qrcode.png"
        ready_port = webui_port
        ready = False
        deadline = asyncio.get_running_loop().time() + _READY_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            for candidate in range(webui_port, webui_port + 5):
                if await self._wait_http_port(candidate, wait_seconds=2.0):
                    ready_port = candidate
                    ready = True
                    break
            if ready:
                break
            if _path_exists(qr_file) and qr_file.stat().st_size > 100:
                # QR out = login flow started; WebUI may still be gated
                ready = True
                ready_port = 0
                break
            # the launcher died (missing xvfb, bad QQ binary, missing
            # libs, OOM): report immediately with the exit code
            proc: subprocess.Popen[Any] | None = self._processes.get(instance_id)
            if proc is not None and proc.poll() is not None:
                log_tail = self._tail_log(log_file)
                self._processes.pop(instance_id, None)
                raise RuntimeError(
                    f"napcat {instance_id} exited early (code "
                    f"{proc.returncode}); see {log_file}{log_tail}"
                )
            await asyncio.sleep(2.0)
        endpoint = self._endpoint(instance_id)
        if not ready:
            log_tail = self._tail_log(log_file)
            self._terminate(instance_id)
            raise RuntimeError(
                f"napcat {instance_id} started but neither its WebUI "
                f"(http://127.0.0.1:{webui_port}, scanned +0..+4) nor the "
                f"QR file ({qr_file}) appeared in {_READY_TIMEOUT:.0f}s; "
                f"see {log_file}{log_tail}"
            )
        self._webui_ports[instance_id] = ready_port
        logger.info(
            "napcat %s: ready (WebUI :%d, OneBot HTTP on :%d after login)",
            instance_id,
            ready_port or webui_port,
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
        # NapCat spawns QQ (xvfb-run + dbus-run-session on Linux, boot exe
        # on Windows): kill the whole tree so no orphan QQ process remains.
        # pkill -P only hits direct children, so walk the tree recursively.
        try:
            if _IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    timeout=10,
                )
            else:
                visited: set[int] = set()

                def _kill_tree(pid: int, sig: str) -> None:
                    if pid in visited:
                        return
                    visited.add(pid)
                    out = subprocess.run(
                        ["pgrep", "-P", str(pid)],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    ).stdout
                    for child in out.split():
                        _kill_tree(int(child), sig)
                    subprocess.run(
                        ["kill", "-" + sig, str(pid)],
                        capture_output=True,
                        timeout=10,
                    )

                _kill_tree(process.pid, "TERM")
                # give the tree a moment, then force what remains
                try:
                    process.wait(timeout=3)
                except Exception:
                    _kill_tree(process.pid, "KILL")
        except Exception:
            pass

    async def stop(self, instance_id: str) -> None:
        # terminate can block up to 5s waiting for the process tree; run it
        # off the event loop so the TUI never freezes on cancel
        await asyncio.to_thread(self._terminate, instance_id)

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
        qr_file = _instance_dir(instance_id) / "cache" / "qrcode.png"
        # logged in? -> sentinel; a failed probe just means not yet ready.
        # Probe the configured OneBot port first, then the WebUI port
        # (NapCat also answers get_login_info there once a session is up).
        # Log the probe target + status on change so a misconfigured port
        # is visible in the guide instead of silently waiting forever.
        logged_in = False
        endpoints = [self._endpoint(instance_id)]
        webui = self._webui_ports.get(instance_id)
        webui_url = f"http://127.0.0.1:{webui}" if webui else ""
        if webui_url and webui_url not in endpoints:
            endpoints.append(webui_url)
        probe = ""
        async with httpx.AsyncClient(timeout=8.0) as client:
            for endpoint in endpoints:
                try:
                    login = await client.post(f"{endpoint}/get_login_info", json={})
                    login.raise_for_status()
                    login_data: Any = login.json().get("data") or {}
                    # accept any id field NapCat/OneBot variants emit
                    # (user_id / uin / account); an empty dict = not yet
                    logged_in = bool(
                        login_data.get("user_id")
                        or login_data.get("uin")
                        or login_data.get("account")
                    )
                    probe = f"{endpoint} HTTP {login.status_code}"
                    if logged_in:
                        break
                except Exception as exc:
                    probe = f"{endpoint} ERR {type(exc).__name__}"
        if probe != getattr(self, "_last_probe", None):
            self._last_probe = probe
            logger.info(
                "napcat %s: login probe -> %s (%s)",
                instance_id,
                "logged in" if logged_in else "waiting",
                probe,
            )
            # also log which local ports answer get_login_info at all,
            # so a misconfigured NapCat HTTP server is diagnosable
            if not logged_in:
                ports: list[int] = []
                base = self._port_for(instance_id)
                for candidate in list(range(base, base + 4)) + list(
                    range(_WEBUI_PORT, _WEBUI_PORT + 4)
                ):
                    try:
                        async with httpx.AsyncClient(timeout=1.5) as client:
                            resp = await client.post(
                                f"http://127.0.0.1:{candidate}/get_login_info", json={}
                            )
                        if resp.status_code < 500:
                            ports.append(candidate)
                    except Exception:
                        continue
                if ports:
                    logger.info(
                        "napcat %s: get_login_info answered on ports %s",
                        instance_id,
                        ports,
                    )
        if logged_in:
            return _QR_LOGGED_IN
        # NOTE: no stable-QR heuristic here — while waiting for a scan
        # the QR file is equally stable, so it would falsely report
        # 'logged in' before the user scanned. Login is only confirmed
        # by the get_login_info probe above, or manually by the user via
        # the guide's 'I'm logged in' button.
        # not logged in: the QR png (refreshed by NapCat on expiry)
        try:
            raw = qr_file.read_bytes()
        except OSError:
            # diagnose why the QR is missing and hand it to the guide via
            # the ERROR: prefix so the user sees the actual reason
            tail = self._tail_log(_instance_dir(instance_id) / "napcat.log", lines=10)
            logger.info(
                "napcat %s: QR not ready yet (%s missing)%s",
                instance_id,
                qr_file,
                tail,
            )
            return f"ERROR: QR file {qr_file} not created yet{tail}"
        if len(raw) < 100:
            return (
                f"ERROR: QR file exists but is empty/short ({len(raw)} bytes) — "
                "the QQ login screen may not have rendered"
            )
        import base64 as _b64

        return _b64.b64encode(raw).decode("ascii")


__all__ = ["NapCatProvisioner", "_find_node"]
