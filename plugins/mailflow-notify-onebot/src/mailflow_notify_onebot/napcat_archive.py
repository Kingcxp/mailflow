"""Pinned NapCat release archive and its Linux container lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path
from typing import Any

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


async def require_docker() -> None:
    if shutil.which("docker") is None:
        raise GatewaySetupError("gateway.napcat_docker_required")
    result = await run_command(["docker", "info", "--format", "{{.OSType}}"], timeout_seconds=20.0)
    if result.returncode or result.stdout.strip() != "linux":
        logger.error("NapCat Docker unavailable: %s", result.stderr.strip())
        raise GatewaySetupError("gateway.napcat_docker_required")


async def image_available(image: str) -> bool:
    result = await run_command(["docker", "image", "inspect", image], timeout_seconds=20.0)
    return result.returncode == 0


def _archive_image(archive: Path) -> str:
    # Use the content ID, not the mutable tag imported by `docker load`.
    with tarfile.open(archive) as image_archive:
        handle = image_archive.extractfile("manifest.json")
        if handle is None:
            raise GatewaySetupError("gateway.napcat_archive_invalid")
        with handle:
            manifest = json.load(handle)
        config = Path(manifest[0]["Config"]).name.removesuffix(".json")
    if not re.fullmatch(r"[0-9a-f]{64}", config):
        raise GatewaySetupError("gateway.napcat_archive_invalid")
    return f"sha256:{config}"


async def load_image(archive: Path) -> str:
    image = await asyncio.to_thread(_archive_image, archive)
    result = await run_command(["docker", "load", "--input", str(archive)], timeout_seconds=300.0)
    if result.returncode or not await image_available(image):
        logger.error("NapCat image import failed: %s", result.stderr.strip())
        raise GatewaySetupError("gateway.napcat_docker_load_failed")
    return image


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
        return [*command, image]

    def terminate(self, process: subprocess.Popen[Any] | None) -> None:
        try:
            if process is not None:
                terminate_process(process, tree=True)
        finally:
            result = subprocess.run(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"label={_OWNER_LABEL}={self.owner}",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode:
                logger.error("NapCat container lookup failed: %s", result.stderr.strip())
                raise GatewaySetupError("gateway.napcat_docker_stop_failed")
            containers = result.stdout.split()
            if containers:
                removed = subprocess.run(
                    ["docker", "rm", "--force", *containers],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if removed.returncode:
                    logger.error("NapCat container cleanup failed: %s", removed.stderr.strip())
                    raise GatewaySetupError("gateway.napcat_docker_stop_failed")
