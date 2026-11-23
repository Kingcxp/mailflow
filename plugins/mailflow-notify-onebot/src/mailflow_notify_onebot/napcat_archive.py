"""Pinned NapCat release archive and its Linux container lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from mailflow.gateway import GatewaySetupError
from mailflow.gateway_resources import run_command, terminate_process

logger = logging.getLogger("mailflow.gateway.napcat")

VERSION = "v4.18.42"
REPOSITORY = "XingYunStar/NapCatQQ"
RELEASE_URL = f"https://github.com/{REPOSITORY}/releases/tag/{VERSION}"
DOWNLOAD_ROOT = f"https://github.com/{REPOSITORY}/releases/download/{VERSION}"
SHELL_URL = f"{DOWNLOAD_ROOT}/NapCat.Shell.zip"
SHELL_SHA256 = "02e5ff0f79916c1e04c81c1b4590056406fb4293f1f1300c955e71fd08b2829d"
_LINUX_ASSETS = {
    "amd64": "85e3fd02b58fd836a0177734b6ff77a896000bcfcf209b3b200d7e8b8452df7c",
    "arm64": "0d14594844e9135c760e710754a8778d98649de2ca837fce5a61b69b7d39954d",
}
_OWNER_LABEL = "io.mailflow.napcat.owner"


def linux_asset() -> tuple[str, str]:
    machine = platform.machine().lower()
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    if architecture not in _LINUX_ASSETS:
        raise GatewaySetupError("gateway.napcat_architecture", architecture=machine)
    return (
        f"{DOWNLOAD_ROOT}/napcat-docker-{VERSION}-{architecture}.tar",
        _LINUX_ASSETS[architecture],
    )


def verify_download(path: Path, expected: str) -> None:
    with path.open("rb") as handle:
        actual = hashlib.file_digest(handle, "sha256").hexdigest()
    if actual != expected:
        raise GatewaySetupError("gateway.napcat_checksum")


def docker_image(target: Path) -> str:
    try:
        image = str(json.loads((target / "docker-image.json").read_text("utf-8"))["image"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return image if re.fullmatch(r"sha256:[0-9a-f]{64}", image) else ""


def _linux_user() -> tuple[int, str]:
    if sys.platform == "win32":
        raise GatewaySetupError("gateway.napcat_docker_unsupported")
    import pwd

    uid = os.getuid()
    return uid, pwd.getpwuid(uid).pw_name


def _pending_group_access() -> bool:
    """An approved group change can be used without restarting MailFlow."""
    if sys.platform == "win32":
        return False
    import grp

    try:
        uid, username = _linux_user()
        group = grp.getgrnam("docker")
    except (KeyError, OSError):
        return False
    return (
        uid != 0 and group.gr_gid not in {*os.getgroups(), os.getgid()} and username in group.gr_mem
    )


def docker_command(command: list[str]) -> list[str]:
    if _pending_group_access() and (sg := shutil.which("sg")):
        return [sg, "docker", "-c", shlex.join(command)]
    return command


async def _docker_status() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, ""
    result = await run_command(
        docker_command(["docker", "info", "--format", "{{.OSType}}"]), timeout_seconds=20.0
    )
    if result.returncode or result.stdout.strip() != "linux":
        logger.debug("NapCat Docker unavailable: %s", result.stderr.strip())
        return False, (result.stderr or result.stdout).strip()
    return True, ""


async def require_docker(options: dict[str, Any] | None = None) -> None:
    ready, detail = await _docker_status()
    if ready:
        return
    if options is None:
        raise GatewaySetupError("gateway.napcat_docker_required", detail=detail)
    confirm = cast(Callable[[], Awaitable[bool]] | None, options.get("_confirm_docker_setup"))
    if not callable(confirm) and not options.get("install_dependencies", False):
        raise GatewaySetupError("gateway.napcat_docker_required", detail=detail)
    try:
        distribution = platform.freedesktop_os_release()
    except OSError as exc:
        raise GatewaySetupError("gateway.napcat_docker_unsupported") from exc
    families = {distribution.get("ID", ""), *distribution.get("ID_LIKE", "").split()}
    if (
        not families.intersection({"debian", "ubuntu"})
        or shutil.which("apt-get") is None
        or shutil.which("systemctl") is None
    ):
        raise GatewaySetupError("gateway.napcat_docker_unsupported")
    if os.environ.get("DOCKER_HOST", "") not in {
        "",
        "unix:///var/run/docker.sock",
    } or os.environ.get("DOCKER_CONTEXT", "") not in {"", "default"}:
        raise GatewaySetupError("gateway.napcat_docker_context")
    if shutil.which("docker") is not None:
        context = await run_command(["docker", "context", "show"], timeout_seconds=10.0)
        if context.returncode or context.stdout.strip() != "default":
            raise GatewaySetupError(
                "gateway.napcat_docker_context", detail=(context.stderr or detail).strip()
            )
    if callable(confirm) and not await confirm():
        raise asyncio.CancelledError
    await _prepare_docker(options)
    ready, detail = await _docker_status()
    if not ready:
        raise GatewaySetupError("gateway.napcat_docker_access", detail=detail)


async def _prepare_docker(options: dict[str, Any]) -> None:
    uid, username = _linux_user()
    password: str | None = None
    progress = options.get("_progress")
    try:
        if uid != 0:
            if shutil.which("sudo") is None:
                raise GatewaySetupError("gateway.napcat_docker_admin")
            cached = await run_command(["sudo", "-n", "true"], timeout_seconds=10.0)
            if cached.returncode:
                ask = options.get("_ask_sudo_password")
                if not callable(ask):
                    raise GatewaySetupError("gateway.napcat_docker_admin")
                password = str(await asyncio.to_thread(ask))

        async def execute(command: list[str], limit: float) -> subprocess.CompletedProcess[str]:
            if uid != 0:
                command = [
                    "sudo",
                    *(["-k", "-S", "-p", ""] if password is not None else ["-n"]),
                    *command,
                ]
            return await run_command(
                command,
                timeout_seconds=limit,
                input_text=password + "\n" if password is not None else None,
            )

        async def step(command: list[str], key: str, percent: float, limit: float) -> None:
            if progress is not None:
                progress.update(percent, f"gateway.napcat_docker_{key}", "installing")
            result = await execute(command, limit)
            if result.returncode:
                detail = "\n".join(
                    part.strip() for part in (result.stdout, result.stderr) if part.strip()
                )
                if key == "starting":
                    with contextlib.suppress(OSError, TimeoutError):
                        journal = await execute(
                            ["journalctl", "-u", "docker", "--no-pager", "-n", "30", "-o", "cat"],
                            10.0,
                        )
                        if journal.returncode == 0 and journal.stdout.strip():
                            detail += "\n\n" + journal.stdout.strip()
                if password:
                    detail = detail.replace(password, "***")
                logger.error("NapCat Docker %s failed: %s", key, detail)
                if "incorrect password" in detail.lower() or "not in the sudoers" in detail.lower():
                    raise GatewaySetupError("gateway.napcat_docker_admin", detail=detail)
                raise GatewaySetupError(f"gateway.napcat_docker_{key}_failed", detail=detail)

        service = await run_command(
            ["systemctl", "show", "--property=LoadState", "--value", "docker"],
            timeout_seconds=10.0,
        )
        if shutil.which("docker") is None or service.stdout.strip() == "not-found":
            apt = [
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "NEEDRESTART_MODE=l",
                "apt-get",
                "-o",
                "DPkg::Lock::Timeout=60",
            ]
            await step([*apt, "update"], "installing", 2.0, 300.0)
            await step(
                [*apt, "install", "-y", "--no-remove", "docker.io"],
                "installing",
                5.0,
                900.0,
            )
            # Debian 13 splits the client into a recommended package; minimal
            # systems may have disabled installation of recommendations.
            if shutil.which("docker") is None:
                await step(
                    [*apt, "install", "-y", "--no-remove", "docker-cli"],
                    "installing",
                    6.0,
                    300.0,
                )
        # Starting an active service is a no-op; do not restart other containers.
        await step(["systemctl", "enable", "--now", "docker"], "starting", 8.0, 90.0)
        if uid != 0:
            await step(["usermod", "-aG", "docker", username], "permissions", 9.0, 15.0)
    finally:
        password = None


async def image_available(image: str) -> bool:
    result = await run_command(
        docker_command(["docker", "image", "inspect", image]), timeout_seconds=20.0
    )
    return result.returncode == 0


def _archive_references(archive: Path) -> list[str]:
    """Read lookup references; Docker's store determines the runnable image ID."""
    with tarfile.open(archive) as image_archive:
        handle = image_archive.extractfile("manifest.json")
        if handle is None:
            raise GatewaySetupError("gateway.napcat_archive_invalid")
        with handle:
            manifest = json.load(handle)
        config = Path(manifest[0]["Config"]).name.removesuffix(".json")
        if not re.fullmatch(r"[0-9a-f]{64}", config):
            raise GatewaySetupError("gateway.napcat_archive_invalid")
        references = list(cast("list[str]", manifest[0].get("RepoTags") or []))
        # Classic stores use the config digest. Containerd may instead use
        # a manifest/index digest, also present in Docker's OCI save layout.
        with contextlib.suppress(KeyError):
            index = image_archive.extractfile("index.json")
            if index is not None:
                with index:
                    references.extend(str(item["digest"]) for item in json.load(index)["manifests"])
        references.append(f"sha256:{config}")
    return list(dict.fromkeys(references))


async def load_image(archive: Path) -> str:
    references = await asyncio.to_thread(_archive_references, archive)
    result = await run_command(
        docker_command(["docker", "load", "--input", str(archive)]), timeout_seconds=300.0
    )
    if result.returncode:
        detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        logger.error("NapCat image import failed: %s", detail)
        raise GatewaySetupError("gateway.napcat_docker_load_failed", detail=detail)
    failures: list[str] = []
    for reference in references:
        inspected = await run_command(
            docker_command(["docker", "image", "inspect", "--format", "{{.Id}}", reference]),
            timeout_seconds=20.0,
        )
        image = inspected.stdout.strip()
        if inspected.returncode == 0 and re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            # Persist and run the resolved content ID, never a mutable tag.
            return image
        failures.append(f"{reference}: {(inspected.stderr or inspected.stdout).strip()}")
    detail = "\n\n".join(failures)
    logger.error("NapCat imported image verification failed: %s", detail)
    raise GatewaySetupError("gateway.napcat_docker_verify_failed", detail=detail)


class DockerLaunch:
    """Own one foreground container without touching unrelated containers."""

    def __init__(self, target: Path) -> None:
        self.target = target.resolve()
        self.owner = uuid.uuid4().hex
        self.name = f"mailflow-napcat-{self.owner[:12]}"

    def command(self, image: str, uin: str, env: dict[str, Any]) -> list[str]:
        command = [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=host",
            "--memory=2560m",
            "--name",
            self.name,
            "--hostname",
            "mailflow-qq",
            "--label",
            f"{_OWNER_LABEL}={self.owner}",
        ]
        for local, remote in (
            ("config", "/app/napcat/config"),
            ("cache", "/app/napcat/cache"),
            ("profile/.config/QQ", "/app/.config/QQ"),
        ):
            directory = self.target / local
            directory.mkdir(parents=True, exist_ok=True)
            command += ["--mount", f"type=bind,src={directory},dst={remote}"]
        container_env = {
            **env,
            "NAPCAT_WORKDIR": "/app/napcat",
            "NAPCAT_UID": str(getattr(os, "getuid", lambda: 0)()),
            "NAPCAT_GID": str(getattr(os, "getgid", lambda: 0)()),
        }
        if uin.isdigit():
            container_env["ACCOUNT"] = uin
        for key, value in container_env.items():
            command += ["--env", f"{key}={value}"]
        return docker_command([*command, image])

    def terminate(self, process: subprocess.Popen[Any] | None) -> None:
        try:
            if process is not None:
                terminate_process(process, tree=True)
        finally:
            result = subprocess.run(
                docker_command(
                    [
                        "docker",
                        "ps",
                        "--all",
                        "--quiet",
                        "--filter",
                        f"label={_OWNER_LABEL}={self.owner}",
                    ]
                ),
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode:
                logger.error("NapCat container lookup failed: %s", result.stderr.strip())
                raise GatewaySetupError(
                    "gateway.napcat_docker_stop_failed",
                    detail=(result.stderr or result.stdout).strip(),
                )
            containers = result.stdout.split()
            if containers:
                removed = subprocess.run(
                    docker_command(["docker", "rm", "--force", *containers]),
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if removed.returncode:
                    logger.error("NapCat container cleanup failed: %s", removed.stderr.strip())
                    raise GatewaySetupError(
                        "gateway.napcat_docker_stop_failed",
                        detail=(removed.stderr or removed.stdout).strip(),
                    )
