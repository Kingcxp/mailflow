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
import secrets
import shutil
import subprocess
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


def _find_docker_compose() -> str | None:
    """A runnable ``docker compose`` (v2 plugin) or ``docker-compose``."""
    if shutil.which("docker"):
        result = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return "docker compose"
    if shutil.which("docker-compose"):
        return "docker-compose"
    return None


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
        if compose is None:
            raise GatewayNotInstalledError(
                "wechatpadpro needs Docker with compose (v2 plugin or docker-"
                "compose) — install Docker Desktop / engine first; MailFlow "
                "never installs system packages itself"
            )
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
            progress.update(10.0, "pulling wechatpadpro/mysql/redis images", "installing")
        result = await asyncio.to_thread(
            subprocess.run,
            [*compose.split(), "-f", str(compose_file), "pull"],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"wechatpadpro {instance_id}: image pull failed: {result.stderr.strip()[:400]}"
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
        await asyncio.to_thread(
            subprocess.run,
            [*compose.split(), "-f", str(compose_file), "up", "-d"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        deadline = asyncio.get_running_loop().time() + _READY_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if await self._wait_http(endpoint, wait_seconds=3.0):
                await self._ensure_bridge(instance_id, options)
                return GatewayInstance(
                    provider=self.provider,
                    instance_id=instance_id,
                    status="running",
                    endpoint=endpoint,
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
            [*compose.split(), "-f", str(compose_file), "stop"],
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
