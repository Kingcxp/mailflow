"""WeChatPadPro gateway provisioner: auto-deploy a WeChat Pad-protocol
gateway (WeChatPadPro + MySQL + Redis) with docker compose, drive the QR
login inside the TUI, and bridge incoming webhook messages to the local
``mailflow.bot_server`` command endpoint.

Install model:
- WeChatPadPro ships as a docker image that needs MySQL and Redis. The
  provisioner writes a per-instance compose project under
  ``<data>/gateways/wechatpadpro-<instance>/`` (compose.yml + .env with a
  generated ADMIN_KEY), then runs ``docker compose up -d``.
- Login: ``POST /admin/GenAuthKey`` (admin key) → auth key;
  ``POST /login/GetLoginQrCodeNewX`` → QR PNG + uuid;
  ``GET /login/CheckLoginStatus`` → login state polling.
- Incoming messages arrive as webhook POSTs (the provisioner registers
  the webhook endpoint with the gateway) and are forwarded to
  ``MAILFLOW_BOT_URL`` (``mailflow.bot_server``), the same chat-command
  path the napcat bridge uses.

The Pad protocol is a third-party protocol: WeChat risk control can warn
or ban the account — the same class of risk the openwechat (web/UOS)
path carries. Keep usage personal and low-volume.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import platform
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import httpx
from mailflow.contracts import GatewayInstance
from mailflow.gateway import GatewayNotInstalledError

logger = logging.getLogger("mailflow.gateway.wechatpadpro")

_QR_LOGGED_IN = "__MAILFLOW_LOGGED_IN__"
_API_PORT_BASE = 8100  # per-instance HTTP API port (host side)
_WEBHOOK_PORT_BASE = 18200  # per-instance webhook listener port
_READY_TIMEOUT = 120.0  # mysql+redis+app first boot is slow


def _data_root() -> Path:
    return Path("data") / "gateways"


def _instance_dir(instance_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in str(instance_id)).strip("-")
    return _data_root() / f"wechatpadpro-{safe}"


_DOCKER_DESKTOP_CLI = Path("C:/Program Files/Docker/Docker/resources/bin/docker.exe")


def _docker_exe() -> str | None:
    """A runnable docker CLI: PATH first, then the Docker Desktop default."""
    found = shutil.which("docker")
    if found:
        return found
    if _DOCKER_DESKTOP_CLI.exists():
        return str(_DOCKER_DESKTOP_CLI)
    return None


def _docker_daemon_up(docker: str, *, attempts: int = 2) -> bool:
    """The CLI alone is not enough: with Docker Desktop installed but not
    started, every compose command fails with a pipe error — and the
    auto-install silently waits forever. `docker info` answers only when
    the daemon is up. A starting engine answers intermittently (the CLI
    sometimes wins the pipe race), so one failure is not a verdict."""
    for attempt in range(attempts):
        try:
            result = subprocess.run([docker, "info"], capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass
        if attempt + 1 < attempts:
            time.sleep(2.0)
    return False


def _find_docker_compose(*, require_daemon: bool = True) -> str | None:
    """A usable docker: CLI + (when require_daemon) a RUNNING daemon.

    The daemon check matters: Docker Desktop installs fine but stays
    down until launched, and `docker compose pull/up` against a stopped
    daemon fails instantly with a pipe error — which reads to the user
    as 'progress stuck at 0%'."""
    docker = _docker_exe()
    if docker:
        result = subprocess.run(
            [docker, "compose", "version"], capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and (not require_daemon or _docker_daemon_up(docker)):
            return docker
    legacy = shutil.which("docker-compose")
    # Docker Desktop ships a docker-compose shim too: same daemon
    # requirement, so it must not bypass the daemon check
    if legacy and (not require_daemon or _docker_daemon_up(legacy)):
        return legacy
    return None


def _wait_daemon(docker: str, *, seconds: int, progress: Any) -> bool:
    """Poll `docker info` for ``seconds``; every probe is reported on the
    progress channel so waiting is never silent."""
    deadline = time.monotonic() + seconds
    step = 0
    while time.monotonic() < deadline:
        step += 1
        remaining = int(deadline - time.monotonic())
        progress(
            min(2.0 + step, 9.0),
            f"checking the docker daemon ({remaining}s left)…",
            "installing",
        )
        if _docker_daemon_up(docker, attempts=1):
            progress(9.0, "docker daemon is up", "installing")
            return True
        time.sleep(3.0)
    return False


def _journal_tail(unit: str, ask: Any) -> str:
    """Last lines of the unit's journal — the ONLY way to tell the user
    WHY a daemon failed to start (PVE LXC: nesting disabled; disk full;
    apparmor...). Best-effort: never raises."""
    try:
        code, output = _sudo_run(["journalctl", "-u", unit, "-n", "12", "--no-pager"], str(ask()))
        return output.strip()[-600:] if code == 0 else ""
    except Exception:
        return ""


# last failure diagnostics; the provision flow reads it right after the
# start attempt to build the user-facing error
_last_linux_failure: dict[str, str] = {"detail": ""}


def _start_linux_docker_service(ask: Any, progress: Any) -> bool:
    """Start the docker service via sudo systemctl (password asked through
    the guide's prompt). Returns True when the daemon came up."""
    if ask is None:
        progress(2.0, "docker service is stopped and no sudo prompt is available", "installing")
        return False
    sudo_retries_left = 1  # one wrong-password re-prompt, not per-unit
    for unit in ("docker", "docker.service"):
        progress(3.0, f"starting the docker service (sudo systemctl start {unit})…", "installing")
        code, output = _sudo_run(["systemctl", "start", unit], str(ask()))
        while (
            code != 0
            and sudo_retries_left > 0
            and (
                "incorrect password" in output.lower() or "authentication failure" in output.lower()
            )
        ):
            # wrong password: ask once more (a service failure must not
            # burn the retry — it would just re-fail and confuse)
            sudo_retries_left -= 1
            code, output = _sudo_run(["systemctl", "start", unit], str(ask()))
        if code == 0:
            # enabled = survives the next VM reboot without a re-setup
            with contextlib.suppress(Exception):
                _sudo_run(["systemctl", "enable", unit], str(ask()))
            docker = _docker_exe()
            if docker is None:
                return False
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                progress(
                    6.0,
                    f"waiting for the docker daemon ({int(deadline - time.monotonic())}s left)…",
                    "installing",
                )
                if _docker_daemon_up(docker):
                    return True
                time.sleep(2.0)
            # systemctl start 'succeeded' but the daemon died: the journal
            # explains (nesting disabled on PVE LXC, apparmor, disk full…)
            _last_linux_failure["detail"] = _journal_tail(unit, ask)
            return False
        lowered = output.lower()
        if "not found" in lowered or "does not exist" in lowered:
            continue
        _last_linux_failure["detail"] = (
            f"systemctl start {unit}: {output.strip()[:300]}\n{_journal_tail(unit, ask)}"
        )
        progress(
            3.0, f"systemctl start {unit} failed — see the error for the journal tail", "installing"
        )
    return False


def _start_docker_desktop(progress: Any) -> bool:
    """Launch Docker Desktop and wait for the daemon (up to ~2.5 min).
    Returns True when the daemon came up. ``progress`` is the installer's
    (percent, message, stage) callback."""
    exe = Path("C:/Program Files/Docker/Docker/Docker Desktop.exe")
    if not exe.exists():
        return False
    progress(3.0, "starting Docker Desktop (the daemon is not running)…", "installing")
    try:
        subprocess.Popen([str(exe)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    docker = _docker_exe()
    if docker is None:
        return False
    # WSL2 cold start (first boot after reboot) routinely takes 2-4 min:
    # 150s aborted too early and read as 'daemon is not running'
    deadline = time.monotonic() + 300
    step = 0
    while time.monotonic() < deadline:
        step += 1
        progress(
            min(3.0 + step, 9.0),
            f"waiting for the docker daemon ({int(deadline - time.monotonic())}s left)…",
            "installing",
        )
        if _docker_daemon_up(docker):
            return True
        # the GUI process dying (crash, EULA dialog closed) is fatal —
        # keep waiting only while it is alive
        probe = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Docker Desktop.exe"],
            capture_output=True,
            timeout=15,
        )
        # tasklist prints in the OEM codepage (GBK on zh-CN Windows):
        # text=True would decode utf-8 and crash the reader thread
        stdout_text = probe.stdout.decode("utf-8", errors="replace")
        if "Docker Desktop.exe" not in stdout_text:
            progress(
                9.0,
                "Docker Desktop exited during startup (check for update/EULA dialogs)",
                "installing",
            )
            return False
        time.sleep(3.0)
    return False


def _sudo_run(command: list[str], sudo_password: str) -> tuple[int, str]:
    """Run ``command`` through ``sudo -S`` (password on stdin); returns
    (returncode, combined output). Linux only — Windows uses its own
    install paths."""
    prefixed = ["sudo", "-S", "-p", "", *command]
    result = subprocess.run(
        prefixed,
        input=sudo_password + "\n",
        capture_output=True,
        text=True,
        timeout=1800,
    )
    return result.returncode, (result.stdout + result.stderr)


def _apt_available() -> bool:
    return shutil.which("apt-get") is not None


def install_docker_dependencies(ask_sudo_password: Any = None, progress: Any = None) -> str:
    """Install Docker Engine + compose when missing (Linux/Debian via apt;
    Windows via winget for Docker Desktop). Returns the compose command
    found/installed. Raises RuntimeError with the failing output.

    ``ask_sudo_password()`` is invoked lazily — only when an apt step
    actually needs sudo (Linux). Returns the password string. The host
    (TUI) supplies the prompt so the password is collected at the moment
    of need and never stored; one answer is reused for the remaining
    steps of this install."""
    sudo_password: str | None = None

    def _password() -> str:
        nonlocal sudo_password
        if sudo_password is None:
            if ask_sudo_password is None:
                raise RuntimeError(
                    "docker is missing and installing it needs sudo — "
                    "provide the password when prompted or install docker "
                    "manually, then retry"
                )
            sudo_password = str(ask_sudo_password())
        return sudo_password

    if _find_docker_compose() is not None:
        return _find_docker_compose() or ""

    system = platform.system()
    if system == "Windows":
        if shutil.which("winget"):
            if progress is not None:
                progress(
                    5.0, "installing Docker Desktop via winget (~500 MB download)", "downloading"
                )
            result = subprocess.run(
                [
                    "winget",
                    "install",
                    "--id",
                    "Docker.DockerDesktop",
                    "--accept-source-agreements",
                    "--accept-package-agreements",
                ],
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Docker Desktop winget install failed: {result.stderr.strip()[:400]}"
                )
            compose = _find_docker_compose()
            if compose is None:
                raise RuntimeError(
                    "Docker Desktop installed but not usable yet — start "
                    "Docker Desktop once, then retry the setup"
                )
            return compose
        raise RuntimeError(
            "Docker is missing and winget is unavailable — install Docker "
            "Desktop from https://www.docker.com/products/docker-desktop/"
        )

    # Linux: apt-only supported path
    if not _apt_available():
        raise RuntimeError(
            "docker is missing and this distro has no apt-get — install "
            "docker.io and docker-compose-plugin with the system package "
            "manager, then retry"
        )
    steps = [
        ["apt-get", "update"],
        [
            "apt-get",
            "install",
            "-y",
            "docker.io",
            "docker-compose-v2",
        ],
    ]
    total = len(steps)
    for index, step in enumerate(steps, start=1):
        if progress is not None:
            progress(
                5.0 + 40.0 * index / total,
                f"apt: {step[0]} {' '.join(step[1:])}",
                "installing",
            )
        code, output = _sudo_run(step, _password())
        if code != 0:
            # a wrong password surfaces as an apt failure: ask again once
            if "incorrect password" in output or "try again" in output.lower():
                sudo_password = None  # force re-prompt
                code, output = _sudo_run(step, _password())
            if code != 0 and "docker-compose-v2" in step:
                # the compose package name differs per distro/suite: plain
                # Debian has neither docker-compose-v2 nor -plugin — only
                # the classic docker-compose package
                for fallback in ("docker-compose-plugin", "docker-compose"):
                    code, output = _sudo_run(
                        ["apt-get", "install", "-y", fallback],
                        _password(),
                    )
                    if code == 0:
                        break
            if code != 0:
                raise RuntimeError(f"apt install failed: {output.strip()[:400]}")
    # fresh apt installs do NOT auto-start the daemon on minimal VMs
    # (typical PVE/Debian): start it ourselves — the sudo password is
    # already cached from the apt steps
    docker = _docker_exe()
    if (docker is None or not _docker_daemon_up(docker)) and not _start_linux_docker_service(
        ask_sudo_password, progress
    ):
        detail = _last_linux_failure.get("detail", "")
        raise RuntimeError(
            "docker packages installed but the daemon did not start"
            + (f":\n{detail}" if detail else " (check journalctl -u docker on the VM)")
        )
    compose = _find_docker_compose()
    if compose is None:
        raise RuntimeError(
            "docker packages installed but `docker compose` still not "
            "resolvable — start the docker service (systemctl start docker) "
            "and retry"
        )
    return compose


def _port_for(instance_id: str, base: int) -> int:
    digest = hashlib.sha1(instance_id.encode()).hexdigest()
    return base + int(digest[:4], 16) % 900


_COMPOSE_TEMPLATE = """\
services:
  wechatpadpro:
    image: wechatpadpro/wechatpadpro:latest
    container_name: mailflow-wpp-{safe}
    restart: unless-stopped
    ports:
      - "{api_port}:1238"
    environment:
      - DB_HOST=mysql
      - REDIS_HOST=redis
      - TZ=Asia/Shanghai
      - ADMIN_KEY={admin_key}
      - WEBHOOK_URL={webhook_url}
      - WEBHOOK_SECRET={webhook_secret}
      - REDIS_DB=1
      - MYSQL_CONNECT_STR=weixin:wppmailflow@tcp(mysql:3306)/weixin?charset=utf8mb4&parseTime=true&loc=Local
    depends_on:
      mysql:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - wpp-{safe}

  mysql:
    image: mysql:8.0
    container_name: mailflow-wpp-{safe}-mysql
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: {mysql_root}
      MYSQL_DATABASE: weixin
      MYSQL_USER: weixin
      MYSQL_PASSWORD: wppmailflow
    volumes:
      - mysql_data_{safe}:/var/lib/mysql
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost", "-uroot", "-p{mysql_root}"]
      interval: 5s
      timeout: 5s
      retries: 30
    networks:
      - wpp-{safe}

  redis:
    image: redis:6
    container_name: mailflow-wpp-{safe}-redis
    restart: unless-stopped
    command: redis-server --appendonly yes
    volumes:
      - redis_data_{safe}:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 30
    networks:
      - wpp-{safe}

networks:
  wpp-{safe}:
    driver: bridge

volumes:
  mysql_data_{safe}:
  redis_data_{safe}:
"""


class _WebhookBridge:
    """In-process HTTP listener the WeChatPadPro gateway POSTs message
    events to; each text message is forwarded to ``bot_url``
    (``mailflow.bot_server``) and the reply is ACKed (the gateway does
    not send it back — replies go out through the notifier's /Msg/SendTxt).
    """

    def __init__(self, instance_id: str, port: int, bot_url: str, secret: str) -> None:
        self.instance_id = instance_id
        self.port = port
        self.bot_url = bot_url
        self.secret = secret
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            parts = request_line.decode("utf-8", "replace").strip().split()
            # consume headers
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            body = await reader.read(1 << 20)
            status = 200
            if parts and parts[0] == "POST":
                try:
                    payload = json.loads(body.decode("utf-8", "replace") or "{}")
                    await self._forward(payload)
                except Exception as exc:
                    logger.warning(
                        "wechatpadpro %s webhook payload failed: %s",
                        self.instance_id,
                        exc,
                    )
                    status = 400
            await self._ack(writer, status)
        except Exception as exc:
            logger.warning("wechatpadpro %s webhook read failed: %s", self.instance_id, exc)
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _forward(self, payload: dict[str, Any]) -> None:
        data = cast_dict(payload.get("Data") or payload)
        if not data:
            return
        from_user = str(data.get("FromUserName") or "")
        content = str(data.get("Content") or "")
        push_type: Any = data.get("MsgType")
        # group messages arrive as "wxid:\ncontent" — strip the sender prefix
        if ":@" in from_user or (from_user.endswith("@chatroom") and ":\n" in content):
            _, _, content = content.partition(":\n")
        if not content or push_type not in (None, 1, "1"):
            return
        chat_type = "group" if from_user.endswith("@chatroom") else "private"
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                await client.post(
                    self.bot_url,
                    json={
                        "text": content,
                        "sender": str(data.get("SenderUserName") or from_user),
                        "chat_id": from_user,
                        "chat_type": chat_type,
                        "provider": "wechatpadpro",
                        "instance_id": self.instance_id,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "wechatpadpro %s dispatch to bot_server failed: %s",
                    self.instance_id,
                    exc,
                )

    async def _ack(self, writer: asyncio.StreamWriter, status: int) -> None:
        body = b'{"code": "200", "message": "success"}'
        writer.write(
            (
                f"HTTP/1.1 {status} OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + body
        )
        with contextlib.suppress(Exception):
            await writer.drain()


class WechatPadProProvisioner:
    provider = "wechatpadpro"

    def __init__(self) -> None:
        self._bridges: dict[str, _WebhookBridge] = {}

    # -- helpers -------------------------------------------------------------

    def _endpoint(self, instance_id: str) -> str:
        return f"http://127.0.0.1:{self._api_port(instance_id)}"

    @staticmethod
    def _api_port(instance_id: str) -> int:
        return _port_for(instance_id, _API_PORT_BASE)

    @staticmethod
    def _webhook_port(instance_id: str) -> int:
        return _port_for(instance_id, _WEBHOOK_PORT_BASE)

    def _webhook_url(self, instance_id: str) -> str:
        return f"http://host.docker.internal:{self._webhook_port(instance_id)}/webhook"

    # -- contract ------------------------------------------------------------

    async def detect(self) -> str:
        compose = _find_docker_compose()
        return f"docker: {compose}" if compose else "docker not available"

    async def is_available(self) -> bool:
        return _find_docker_compose() is not None

    async def install(self, instance_id: str, options: dict[str, Any]) -> None:
        compose = _find_docker_compose()
        progress = options.get("_progress")
        if compose is None:
            # either docker is missing entirely, or the CLI is present but
            # the daemon is down (Docker Desktop not started) — both must
            # be fixed before any pull can work
            ask = options.get("_ask_sudo_password")

            def _install_progress(pct: float, message: str, stage: str) -> None:
                if progress is not None:
                    progress.update(pct, message, stage)

            def _provision_docker() -> str:
                docker = _docker_exe()
                if docker is not None and not _docker_daemon_up(docker):
                    # daemon down (or still starting): the user may have
                    # launched Docker Desktop themselves a moment ago —
                    # give an ALREADY-RUNNING engine a grace window before
                    # spawning anything
                    if _wait_daemon(docker, seconds=60, progress=_install_progress):
                        return _find_docker_compose() or ""
                    # still down: Windows — launch Docker Desktop; Linux —
                    # systemctl start docker under sudo (password asked via
                    # the guide's prompt, same as apt)
                    if platform.system() == "Windows":
                        if _start_docker_desktop(_install_progress):
                            return _find_docker_compose() or ""
                    else:
                        if _start_linux_docker_service(ask, _install_progress):
                            return _find_docker_compose() or ""
                    raise RuntimeError(
                        "docker daemon did not come up (Windows: start "
                        "Docker Desktop; Linux: systemctl start docker) — "
                        "then retry the setup"
                    )
                return install_docker_dependencies(ask, _install_progress)

            compose = await asyncio.to_thread(_provision_docker)
        target = _instance_dir(instance_id)
        target.mkdir(parents=True, exist_ok=True)
        compose_file = target / "compose.yml"
        if compose_file.exists():
            logger.info("wechatpadpro %s: compose project already present", instance_id)
            return
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in instance_id)
        admin_key = str(options.get("admin_key") or secrets.token_hex(16))
        webhook_secret = secrets.token_hex(16)
        mysql_root = secrets.token_hex(8)
        bot_url = str(options.get("bot_url") or "")
        # persist state we need across restarts
        (target / "instance.json").write_text(
            json.dumps(
                {
                    "admin_key": admin_key,
                    "webhook_secret": webhook_secret,
                    "bot_url": bot_url,
                    "api_port": self._api_port(instance_id),
                    "webhook_port": self._webhook_port(instance_id),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        compose_body = _COMPOSE_TEMPLATE.format(
            safe=safe,
            api_port=self._api_port(instance_id),
            admin_key=admin_key,
            webhook_url=self._webhook_url(instance_id),
            webhook_secret=webhook_secret,
            mysql_root=mysql_root,
        )
        (target / "compose.yml").write_text(compose_body, encoding="utf-8")
        progress = options.get("_progress")
        if progress is not None:
            progress.update(10.0, "pulling wechatpadpro/mysql/redis images", "downloading")
        await self._pull_with_progress(compose, compose_file, progress)

    @staticmethod
    def _parse_pull_line(line: str) -> tuple[str, float, float] | None:
        """Extract (image, current_bytes, total_bytes) from a compose pull
        progress line; None when the line carries no byte counter."""
        match = re.search(r"\((\S+)\)[^\d]*([\d.]+)\s*([kKmMgG]i?B)/([\d.]+)\s*([kKmMgG]i?B)", line)
        if match is None:
            match = re.search(
                r"(?:^|\s)(\S+)\s+([\d.]+)\s*([kKmMgG]i?B)/([\d.]+)\s*([kKmMgG]i?B)", line
            )
        if match is None:
            return None

        def _bytes(value: str, unit: str) -> float:
            factor = {
                "kB": 1e3,
                "KB": 1e3,
                "MB": 1e6,
                "GB": 1e9,
                "kiB": 1024.0,
                "MiB": 1024.0**2,
                "GiB": 1024.0**3,
            }
            return float(value) * factor.get(unit, 1.0)

        total = _bytes(match.group(4), match.group(5))
        if total <= 0:
            return None
        return match.group(1), _bytes(match.group(2), match.group(3)), total

    async def _pull_with_progress(self, compose: str, compose_file: Path, progress: Any) -> None:
        """`docker compose pull` with live per-image byte progress.

        Compose's human output embeds counters ('325.4MB/890.1MB'); sum
        them across images onto the 10..95 band. Phase-only lines still
        update the label so the bar never looks stuck."""
        process = await asyncio.create_subprocess_exec(
            compose,
            "compose",
            "-f",
            str(compose_file),
            "pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        image_totals: dict[str, float] = {}
        image_current: dict[str, float] = {}
        error_tail = ""

        def _emit(line: str) -> None:
            if not line:
                return
            nonlocal error_tail
            parsed = self._parse_pull_line(line)
            if parsed is None and not any(
                k in line for k in ("Pulling", "Downloading", "Extracting", "Waiting", "Pulled")
            ):
                error_tail = (error_tail + "\n" + line)[-600:]
            if parsed is None:
                if any(k in line for k in ("Pulling", "Downloading", "Extracting", "Waiting")):
                    progress.update(progress.percent, f"pulling: {line[:70]}", "downloading")
                return
            image, current, total = parsed
            image_totals[image] = total
            image_current[image] = current
            grand_current = sum(image_current.values())
            grand_total = sum(image_totals.values())
            if grand_total > 0:
                pct = min(10.0 + 85.0 * (grand_current / grand_total), 95.0)
                progress.update(
                    pct,
                    f"pulling {image}: {grand_current / 1e6:.0f}/{grand_total / 1e6:.0f} MB",
                    "downloading",
                )

        stdout = process.stdout
        assert stdout is not None
        buf = b""
        while True:
            block: bytes = await stdout.read(4096)
            if not block:
                break
            buf += block
            while True:
                cuts = [i for i in (buf.find(b"\n"), buf.find(b"\r")) if i != -1]
                if not cuts:
                    break
                cut = min(cuts)
                line_bytes: bytes = buf[:cut]
                buf = buf[cut + 1 :]
                _emit(line_bytes.decode("utf-8", errors="replace").strip())
        if buf.strip():
            _emit(buf.decode("utf-8", errors="replace").strip())
        code = await process.wait()
        if code != 0:
            # surface compose's own text: parse-only silences the real
            # error otherwise
            raise RuntimeError(
                f"wechatpadpro: image pull failed (docker compose pull "
                f"exited {code}): {error_tail.strip()[:400]}"
            )

    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
        target = _instance_dir(instance_id)
        compose_file = target / "compose.yml"
        if not compose_file.exists():
            raise GatewayNotInstalledError(
                f"wechatpadpro {instance_id} is not installed (no compose "
                f"project); run the setup to install it"
            )
        compose = _find_docker_compose()
        if compose is None:
            raise GatewayNotInstalledError("docker compose disappeared since install")
        endpoint = self._endpoint(instance_id)
        # already up? (docker restarts containers with restart: unless-stopped)
        if await self._wait_http(endpoint, wait_seconds=2.0):
            logger.info("wechatpadpro %s: reusing running gateway", instance_id)
            await self._ensure_bridge(instance_id, options)
            return GatewayInstance(
                provider=self.provider,
                instance_id=instance_id,
                status="running",
                endpoint=endpoint,
                extra={"reused": True},
            )
        progress = options.get("_progress")
        if progress is not None:
            progress.update(96.0, "starting containers (docker compose up)", "starting")
        up = await asyncio.to_thread(
            subprocess.run,
            [compose, "compose", "-f", str(compose_file), "up", "-d"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if up.returncode != 0:
            raise RuntimeError(
                f"wechatpadpro {instance_id}: docker compose up failed: "
                f"{(up.stderr or up.stdout).strip()[:400]}"
            )
        deadline = asyncio.get_running_loop().time() + _READY_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if await self._wait_http(endpoint, wait_seconds=3.0):
                if progress is not None:
                    progress.update(99.0, f"gateway answering on {endpoint}", "starting")
                await self._ensure_bridge(instance_id, options)
                return GatewayInstance(
                    provider=self.provider,
                    instance_id=instance_id,
                    status="running",
                    endpoint=endpoint,
                )
            if progress is not None:
                progress.update(
                    97.0, f"waiting for the gateway to answer on {endpoint}…", "starting"
                )
            await asyncio.sleep(3.0)
        raise RuntimeError(
            f"wechatpadpro {instance_id} did not answer on {endpoint} in "
            f"{_READY_TIMEOUT:.0f}s; check `docker compose logs` under {target}"
        )

    async def stop(self, instance_id: str) -> None:
        bridge = self._bridges.pop(instance_id, None)
        if bridge is not None:
            await bridge.stop()
        target = _instance_dir(instance_id)
        compose_file = target / "compose.yml"
        if not compose_file.exists():
            return
        compose = _find_docker_compose()
        if compose is None:
            return
        await asyncio.to_thread(
            subprocess.run,
            [compose, "compose", "-f", str(compose_file), "stop"],
            capture_output=True,
            text=True,
            timeout=300,
        )

    async def status(self, instance_id: str) -> GatewayInstance:
        endpoint = self._endpoint(instance_id)
        running = await self._wait_http(endpoint, wait_seconds=2.0)
        if running:
            return GatewayInstance(
                provider=self.provider,
                instance_id=instance_id,
                status="running",
                endpoint=endpoint,
            )
        return GatewayInstance(
            provider=self.provider,
            instance_id=instance_id,
            status="stopped",
            error="gateway not answering",
        )

    async def qr(self, instance_id: str) -> str:
        """Login state: base64 QR png, logged-in sentinel, or ERROR: …"""
        meta = self._meta(instance_id)
        if meta is None:
            return "ERROR: instance not installed"
        endpoint = self._endpoint(instance_id)
        key = await self._auth_key(instance_id, meta)
        if not key:
            return "ERROR: could not obtain auth key from admin API"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                if meta.get("login_key") is None:
                    response = await client.post(
                        f"{endpoint}/login/GetLoginQrCodeNewX",
                        params={"key": key},
                        json={"Check": False, "Proxy": ""},
                    )
                    payload = cast_dict(response.json())
                    data = cast_dict(payload.get("data") or {})
                    uuid = str(data.get("uuid") or "")
                    if not uuid:
                        return f"ERROR: qr request rejected: {str(payload)[:120]}"
                    meta["login_key"] = key
                    meta["uuid"] = uuid
                    self._save_meta(instance_id, meta)
                    qrcode = str(data.get("qrcode") or "")
                    if qrcode.startswith("data:image"):
                        return qrcode.split(",", 1)[1]
                    return str(data.get("qrcodeUrl") or "")
                # poll: CheckLoginStatus
                response = await client.get(
                    f"{endpoint}/login/CheckLoginStatus",
                    params={"key": meta["login_key"], "uuid": meta["uuid"]},
                )
                payload = cast_dict(response.json())
                code = payload.get("code")
                if code == 200:
                    # scanned and confirmed: login succeeded
                    self._save_meta(instance_id, {**meta, "logged_in": True})
                    return _QR_LOGGED_IN
                if code == -3:
                    return (
                        "ERROR: WeChat requires a verification code — use the "
                        "verification-code login in the gateway web UI"
                    )
                if code == 300:
                    # expired: restart the QR flow on the next poll
                    self._save_meta(
                        instance_id,
                        {k: v for k, v in meta.items() if k not in ("login_key", "uuid")},
                    )
                    return ""
                return ""
        except Exception as exc:
            logger.warning("wechatpadpro %s /qr failed: %s", instance_id, exc)
            return ""

    async def ensure_bridge(self, instance_id: str, options: dict[str, Any]) -> Any:
        return await self._ensure_bridge(instance_id, options)

    # -- internals -----------------------------------------------------------

    def _meta(self, instance_id: str) -> dict[str, Any] | None:
        meta_file = _instance_dir(instance_id) / "instance.json"
        if not meta_file.exists():
            return None
        try:
            value: Any = json.loads(meta_file.read_text(encoding="utf-8"))
            return cast_dict(value)
        except Exception:
            return None

    def _save_meta(self, instance_id: str, meta: dict[str, Any]) -> None:
        meta_file = _instance_dir(instance_id) / "instance.json"
        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    async def _auth_key(self, instance_id: str, meta: dict[str, Any]) -> str:
        """The per-instance auth key: generated once via the admin API."""
        if meta.get("auth_api_key"):
            return str(meta["auth_api_key"])
        endpoint = self._endpoint(instance_id)
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{endpoint}/admin/GenAuthKey1",
                params={"key": meta["admin_key"]},
                json={"Count": 1, "Days": 3650},
            )
            payload = cast_dict(response.json())
        data = cast_dict(payload.get("data"))
        key = ""
        entries: list[Any] = list(data.get("list") or data.get("keys") or [])
        if entries:
            key = str(cast_dict(entries[0]).get("Key") or "")
        elif data:
            key = str(data.get("Key") or data.get("key") or "")
        if key:
            self._save_meta(instance_id, {**meta, "auth_api_key": key})
        return key

    async def _ensure_bridge(self, instance_id: str, options: dict[str, Any]) -> None:
        existing = self._bridges.get(instance_id)
        meta = self._meta(instance_id) or {}
        bot_url = str(options.get("bot_url") or meta.get("bot_url") or "")
        if not bot_url:
            logger.warning(
                "wechatpadpro %s: no bot_url in options — webhook bridge "
                "cannot be created; re-save the notifier so the chat "
                "endpoint is persisted",
                instance_id,
            )
            return
        if existing is not None and existing.bot_url == bot_url:
            return
        if existing is not None:
            await existing.stop()
        bridge = _WebhookBridge(
            instance_id,
            self._webhook_port(instance_id),
            bot_url,
            str(meta.get("webhook_secret") or ""),
        )
        await bridge.start()
        self._bridges[instance_id] = bridge

    @staticmethod
    async def _wait_http(endpoint: str, wait_seconds: float) -> bool:
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(endpoint)
                if response.status_code < 500:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False


def cast_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        typed: dict[str, Any] = cast("dict[str, Any]", value)
        return dict(typed)
    return {}
