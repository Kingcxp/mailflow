"""NapCat deployment, existing installation compatibility and owned resources."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Any, cast

import httpx
import mailflow_notify_onebot.gateway as gw_mod
import mailflow_notify_onebot.napcat_archive as archive_mod
import pytest
from mailflow_notify_onebot.gateway import (
    NapCatProvisioner,
    _instance_dir,  # pyright: ignore[reportPrivateUsage]
)

# the autouse _linux_env fixture stubs the runtime-lib preflight to []
# for install tests; keep the real implementation for the preflight test
_REAL_MISSING_LIBS = gw_mod._missing_qq_runtime_libs  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def _linux_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    # simulate the Linux host path (AppImage flow) regardless of the OS
    monkeypatch.setattr(gw_mod, "_IS_WINDOWS", False)

    # the CI/test host is not a QQ desktop image: pretend the runtime
    # libraries exist so the tests exercise the install/launch logic
    def _no_missing_libs() -> list[str]:
        return []

    monkeypatch.setattr(gw_mod, "_missing_qq_runtime_libs", _no_missing_libs)


@pytest.mark.asyncio
async def test_linux_start_launches_appimage_with_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    instance = _instance_dir("A-Bot-NapCat-5baf4a")
    instance.mkdir(parents=True, exist_ok=True)
    appimage = instance / "QQ-50969_NapCat-v4.18.19-amd64.AppImage"
    appimage.write_bytes(b"#!fake-appimage")
    appimage.chmod(0o755)

    captured: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> Any:
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")

        class FakeProc:
            pid = 4242
            returncode: int | None = None

            def poll(self) -> int | None:
                return None

            def terminate(self) -> None:
                pass

            def kill(self) -> None:
                pass

            def wait(self, timeout: float | None = None) -> int | None:
                return None

        return FakeProc()

    monkeypatch.setattr(gw_mod.subprocess, "Popen", fake_popen)
    # the terminate path calls pgrep/kill after the wait times out; fake
    # subprocess.run so the test does not touch the real process table.
    # Also make the readiness wait fail fast without a real HTTP server.
    monkeypatch.setattr(
        NapCatProvisioner,
        "_wait_http_port",
        lambda self, port, wait_seconds=2.0: _async_false(),  # pyright: ignore[reportUnknownLambdaType,reportUnknownArgumentType]
    )
    # memory check must pass; shrink the readiness window so the test
    # does not sit through the full 30s timeout
    monkeypatch.setattr(gw_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(gw_mod, "_available_memory_mb", lambda: 4096.0)
    monkeypatch.setattr(gw_mod, "_READY_TIMEOUT", 3.0)

    prov = NapCatProvisioner()
    # fake a node runtime so the preflight passes
    try:
        await prov.start("A-Bot-NapCat-5baf4a", {"port": "39999"})
    except RuntimeError as exc:
        # start will time out (no real gateway); the launch itself is
        # what we assert on
        assert "did not answer" in str(exc) or "started" in str(exc)
    command = captured["command"]
    assert command[0] == "xvfb-run"
    # the AppImage path must be absolute — a relative path would
    # double-prefix against the child's cwd and fail with 'not found'
    appimage_arg = command[2]
    assert Path(appimage_arg).is_absolute()
    assert appimage_arg.endswith(".AppImage")
    # extract-and-run bypasses FUSE, which containers/VMs cannot modprobe
    assert "--appimage-extract-and-run" in command
    assert captured["cwd"] == str(instance.resolve())
    profile = instance.resolve() / "profile"
    assert Path(captured["env"]["HOME"]) == profile
    assert Path(captured["env"]["USERPROFILE"]) == profile


def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args[0] if args else [], 0, stdout="", stderr="")


async def _async_false() -> bool:
    return False


def _available_command(name: str) -> str:
    return f"/bin/{name}"


def _missing_command(name: str) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["windows", "x86_64", "aarch64"])
async def test_default_archive_installs_without_url_and_rejects_corruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, host: str
) -> None:
    from mailflow.gateway import GatewaySetupError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gw_mod, "_IS_WINDOWS", host == "windows")
    monkeypatch.setattr(archive_mod.platform, "machine", lambda: host)
    monkeypatch.setattr(archive_mod.shutil, "which", _available_command)
    image = "sha256:" + "a" * 64
    payload = io.BytesIO()
    if host == "windows":
        with zipfile.ZipFile(payload, "w") as shell:
            shell.writestr("napcat.mjs", "// fake Shell entry")
            shell.writestr("config/onebot11.json", "default gateway configuration")
    else:
        manifest = json.dumps([{"Config": "blobs/sha256/" + "a" * 64}]).encode()
        with tarfile.open(fileobj=payload, mode="w") as image_archive:
            member = tarfile.TarInfo("manifest.json")
            member.size = len(manifest)
            image_archive.addfile(member, io.BytesIO(manifest))
    contents = payload.getvalue()
    digest = hashlib.sha256(contents).hexdigest()
    monkeypatch.setattr(gw_mod, "SHELL_SHA256", digest)
    monkeypatch.setattr(archive_mod, "_LINUX_ASSETS", {"amd64": digest, "arm64": digest})
    downloads: list[str] = []
    loaded = False

    async def docker(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal loaded
        if command[1] == "load":
            loaded = True
        exists = command[1:3] != ["image", "inspect"] or loaded
        return subprocess.CompletedProcess(command, 0 if exists else 1, "linux\n", "")

    def download(url: str, destination: Path, progress: Any = None, **kwargs: Any) -> None:
        downloads.append(url)
        destination.write_bytes(contents)

    monkeypatch.setattr(archive_mod, "run_command", docker)
    monkeypatch.setattr(NapCatProvisioner, "_download", staticmethod(download))
    provisioner = NapCatProvisioner()
    target = _instance_dir("archive")
    config = target / "config" / "onebot11.json"
    config.parent.mkdir(parents=True)
    config.write_text("saved gateway configuration", encoding="utf-8")
    login = target / "profile" / "saved-login"
    login.parent.mkdir(parents=True)
    login.write_text("keep", encoding="utf-8")
    await provisioner.install("archive", {})
    assert config.read_text("utf-8") == "saved gateway configuration"
    assert login.read_text("utf-8") == "keep"
    if host == "windows":
        assert downloads == [archive_mod.SHELL_URL]
        assert (target / "napcat.mjs").read_text("utf-8") == "// fake Shell entry"
    else:
        architecture = "amd64" if host == "x86_64" else "arm64"
        assert downloads == [
            f"{archive_mod.DOWNLOAD_ROOT}/napcat-docker-v4.18.42-{architecture}.tar"
        ]
        assert archive_mod.docker_image(target) == image
        assert (
            json.loads((target / "docker-image.json").read_text("utf-8"))["source"]
            == archive_mod.RELEASE_URL
        )

    await provisioner.install("archive", {})
    assert len(downloads) == 1 and login.read_text("utf-8") == "keep"
    assert not list(target.glob("*.zip")) and not list(target.glob("*.tar"))

    if host == "windows":
        (target / "napcat.mjs").write_text("broken entry", encoding="utf-8")
    await provisioner.install("archive", {"_reinstall": True})
    assert len(downloads) == 2
    assert config.read_text("utf-8") == "saved gateway configuration"
    assert login.read_text("utf-8") == "keep"
    if host == "windows":
        assert (target / "napcat.mjs").read_text("utf-8") == "// fake Shell entry"

    if host != "windows":
        loaded = False  # A pruned Docker image is restored without deleting login data.
        await provisioner.install("archive", {})
        assert len(downloads) == 3 and login.read_text("utf-8") == "keep"
        monkeypatch.setattr(archive_mod.shutil, "which", _missing_command)
        with pytest.raises(GatewaySetupError) as missing:
            await provisioner.install("no-docker", {})
        assert missing.value.code == "gateway.napcat_docker_required"
        assert len(downloads) == 3
        monkeypatch.setattr(archive_mod.shutil, "which", _available_command)

    contents = b"corrupt archive"
    with pytest.raises(GatewaySetupError) as failed_repair:
        await provisioner.install("archive", {"_reinstall": True})
    assert failed_repair.value.code == "gateway.napcat_checksum"
    assert config.read_text("utf-8") == "saved gateway configuration"
    assert login.read_text("utf-8") == "keep"
    if host == "windows":
        assert (target / "napcat.mjs").read_text("utf-8") == "// fake Shell entry"
    else:
        assert archive_mod.docker_image(target) == image
    with pytest.raises(GatewaySetupError) as corrupt:
        await provisioner.install("corrupt", {})
    assert corrupt.value.code == "gateway.napcat_checksum"
    bad_target = _instance_dir("corrupt")
    assert not archive_mod.docker_image(bad_target) and not (bad_target / "napcat.mjs").exists()
    assert not list(bad_target.glob("*.part")) and not list(bad_target.glob("*.zip"))
    assert not list(bad_target.glob("*.tar"))


@pytest.mark.asyncio
async def test_archive_container_stop_and_cancel_keep_login_and_other_containers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(archive_mod.shutil, "which", _available_command)
    monkeypatch.setattr(gw_mod, "_available_memory_mb", lambda: 4096.0)
    image = "sha256:" + "a" * 64
    containers = {"unrelated-bot": "other-owner"}
    commands: list[list[str]] = []
    waiting = asyncio.Event()
    cancel_boot = False

    async def docker(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "linux\n", "")

    class Process:
        pid = 4242
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

    def launch(command: list[str], **kwargs: Any) -> Any:
        commands.append(command)
        name = command[command.index("--name") + 1]
        owner = command[command.index("--label") + 1].split("=", 1)[1]
        containers[name] = owner
        assert kwargs["env"].get("HOME") != str(_instance_dir("normal") / "profile")
        return Process()

    def subprocess_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[1] == "ps":
            owner = command[-1].split("=", 2)[-1]
            owned = "\n".join(name for name, token in containers.items() if token == owner)
            return subprocess.CompletedProcess(command, 0, owned, "")
        assert command[1:3] == ["rm", "--force"]
        containers.pop(command[-1])
        return subprocess.CompletedProcess(command, 0, "", "")

    def terminate(process: Process, **kwargs: Any) -> None:
        process.returncode = 0

    async def bridge(instance_id: str, options: dict[str, Any]) -> Any:
        class Bridge:
            url = "http://127.0.0.1:18321/onebot/event"

        return Bridge()

    async def port_ready(port: int, wait_seconds: float = 2.0) -> bool:
        if port in {39999, 39998}:
            return False
        if cancel_boot:
            waiting.set()
            await asyncio.Event().wait()
        return True

    monkeypatch.setattr(archive_mod, "run_command", docker)
    monkeypatch.setattr(archive_mod, "terminate_process", terminate)
    monkeypatch.setattr(gw_mod.subprocess, "Popen", launch)
    monkeypatch.setattr(gw_mod.subprocess, "run", subprocess_run)
    provisioner = NapCatProvisioner()
    monkeypatch.setattr(provisioner, "ensure_bridge", bridge)
    monkeypatch.setattr(provisioner, "_wait_http_port", port_ready)
    for index, instance_id in enumerate(("normal", "cancelled")):
        target = _instance_dir(instance_id)
        target.mkdir(parents=True)
        (target / "docker-image.json").write_text(json.dumps({"image": image}), encoding="utf-8")
        login = target / "profile" / ".config" / "QQ" / "saved-login"
        login.parent.mkdir(parents=True)
        login.write_text("keep", encoding="utf-8")
        if instance_id == "normal":
            result = await provisioner.start(instance_id, {"port": 39999 - index})
            assert result.status == "starting"
            await provisioner.stop(instance_id)
            await provisioner.stop(instance_id)
        else:
            cancel_boot = True
            task = asyncio.create_task(provisioner.start(instance_id, {"port": 39999 - index}))
            await asyncio.wait_for(waiting.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert login.read_text("utf-8") == "keep"
        config = json.loads((target / "config" / "onebot11.json").read_text("utf-8"))
        assert config["network"]["httpServers"][0]["host"] == "127.0.0.1"
        assert config["network"]["httpClients"][0]["url"].endswith(":18321/onebot/event")
        assert containers == {"unrelated-bot": "other-owner"}
    assert len(commands) == 2
    for command in commands:
        assert command[0:2] == ["docker", "run"] and command[-1] == image
        assert "--network=host" in command and "--pull=never" in command
        assert "NAPCAT_WORKDIR=/app/napcat" in command
        assert any("dst=/app/.config/QQ" in arg for arg in command)


@pytest.mark.asyncio
async def test_explicit_source_and_existing_payload_remain_usable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Custom AppImages, older Shell layouts and saved login data still work."""
    from mailflow.errors import error_text
    from mailflow.i18n import I18n

    monkeypatch.chdir(tmp_path)
    instance = _instance_dir("A-Bot-NapCat-5baf4a")  # pyright: ignore[reportPrivateUsage]
    downloads: dict[str, Path] = {}

    def fake_download(url: str, destination: Path, progress: Any = None, **kw: Any) -> None:
        downloads[url] = destination
        destination.write_bytes(b"#!fake-appimage")

    monkeypatch.setattr(NapCatProvisioner, "_download", staticmethod(fake_download))
    prov = NapCatProvisioner()

    # the runtime preflight checks xvfb-run/fusermount with shutil.which
    def _fake_which(name: str) -> str:
        return f"/usr/bin/{name}"

    monkeypatch.setattr(gw_mod.shutil, "which", _fake_which)

    def interrupted(url: str, destination: Path, progress: Any = None, **kw: Any) -> None:
        destination.write_bytes(b"incomplete")
        raise ConnectionError("download interrupted")

    monkeypatch.setattr(NapCatProvisioner, "_download", staticmethod(interrupted))
    with pytest.raises(ConnectionError):
        await prov.install(
            "A-Bot-NapCat-5baf4a", {"napcat_appimage_url": "https://example.test/napcat.AppImage"}
        )
    assert not list(instance.glob("*.AppImage")) and not list(instance.glob("*.part"))
    monkeypatch.setattr(NapCatProvisioner, "_download", staticmethod(fake_download))
    await prov.install(
        "A-Bot-NapCat-5baf4a", {"napcat_appimage_url": "https://example.test/napcat.AppImage"}
    )
    assert list(downloads) == ["https://example.test/napcat.AppImage"]
    appimage = instance / "NapCat.AppImage"
    assert appimage.exists()
    await prov.install("A-Bot-NapCat-5baf4a", {})
    assert len(downloads) == 1 and appimage.read_bytes() == b"#!fake-appimage"

    # Older Windows Shell layouts are still usable; no new package is needed.
    monkeypatch.setattr(gw_mod, "_IS_WINDOWS", True)
    legacy_entry = instance / "main.js"
    legacy_entry.write_text("// existing installation", encoding="utf-8")
    await prov.install("A-Bot-NapCat-5baf4a", {})
    assert len(downloads) == 1
    assert prov._find_entry(instance, "A-Bot-NapCat-5baf4a") == legacy_entry.resolve()  # pyright: ignore[reportPrivateUsage]

    from mailflow.gateway import GatewayNotInstalledError

    with pytest.raises(GatewayNotInstalledError) as missing:
        await prov.start("missing-installation", {})
    for language in ("en", "zh-CN"):
        translator = I18n(language)
        assert error_text(missing.value, translator.t) == translator.t(
            "gateway.napcat_missing", directory=str(_instance_dir("missing-installation").resolve())
        )
    assert len(downloads) == 1 and legacy_entry.read_text("utf-8") == "// existing installation"


@pytest.mark.asyncio
async def test_qr_fresh_keeps_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A recently updated QR keeps returning its payload (not logged in)."""
    monkeypatch.chdir(tmp_path)
    instance = _instance_dir("A-Bot-NapCat-5baf4a")  # pyright: ignore[reportPrivateUsage]
    (instance / "cache").mkdir(parents=True, exist_ok=True)
    qr_file = instance / "cache" / "qrcode.png"
    qr_file.write_bytes(b"y" * 200)

    prov = NapCatProvisioner()

    # HTTP probe fails (no server) -> falls through to QR payload
    def _dead_endpoint(iid: str) -> str:
        return "http://127.0.0.1:1"

    monkeypatch.setattr(prov, "_endpoint", _dead_endpoint)

    result = await prov.qr("A-Bot-NapCat-5baf4a")
    assert result != "__MAILFLOW_LOGGED_IN__"
    assert result  # the QR payload


@pytest.mark.asyncio
async def test_start_refuses_stale_process_on_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unmanaged listener must neither be adopted nor terminated."""
    monkeypatch.chdir(tmp_path)
    instance = _instance_dir("A-Bot-NapCat-5baf4a")  # pyright: ignore[reportPrivateUsage]
    instance.mkdir(parents=True, exist_ok=True)
    (instance / "QQ-50969_NapCat-v4.18.19-amd64.AppImage").write_bytes(b"x")

    monkeypatch.setattr(gw_mod, "_available_memory_mb", lambda: 4096.0)
    # the port answers HTTP but no process is in _processes -> stale
    monkeypatch.setattr(
        NapCatProvisioner,
        "_wait_http_port",
        lambda self, port, wait_seconds=2.0: _async_true(),  # pyright: ignore[reportUnknownLambdaType,reportUnknownArgumentType]
    )
    prov = NapCatProvisioner()
    with pytest.raises(RuntimeError, match="still in use"):
        await prov.start("A-Bot-NapCat-5baf4a", {"port": "39999"})


@pytest.mark.asyncio
async def test_qr_clears_cached_uin_when_logged_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After logout the login probe no longer sees a session: the cached
    QQ number must be dropped so state never shows a phantom login."""
    monkeypatch.chdir(tmp_path)
    instance = _instance_dir("A-Bot-NapCat-5baf4a")  # pyright: ignore[reportPrivateUsage]
    (instance / "cache").mkdir(parents=True, exist_ok=True)
    (instance / "cache" / "qrcode.png").write_bytes(b"y" * 200)

    prov = NapCatProvisioner()
    prov._uins["A-Bot-NapCat-5baf4a"] = "404291187"  # pyright: ignore[reportPrivateUsage]

    # HTTP success with a rejected business result and a stale cached identity
    # must not report a successful login.
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"retcode": 100, "data": {"user_id": 404291187}})
    )

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return client_type(**kwargs, transport=transport)

    monkeypatch.setattr(gw_mod.httpx, "AsyncClient", client)

    result = await prov.qr("A-Bot-NapCat-5baf4a")
    assert result != "__MAILFLOW_LOGGED_IN__"
    assert result  # QR payload returned
    assert "A-Bot-NapCat-5baf4a" not in prov._uins  # pyright: ignore[reportPrivateUsage]


async def _async_true() -> bool:
    return True


@pytest.mark.asyncio
async def test_ensure_bridge_syncs_per_account_on_healthy_poll() -> None:
    """The per-account config sync must run on EVERY healthy poll — not
    only at bridge creation. At creation time no QQ session exists (the
    user has not logged in yet), so the QQ number is only learnable after
    login; a sync that skips the existing-bridge path would never repair a
    stale per-account config and chat commands would stay dead forever."""
    import asyncio

    prov = NapCatProvisioner()

    syncs: list[str] = []

    async def _fake_sync(instance_id: str) -> bool:
        syncs.append(instance_id)
        return False

    # a REAL listening socket stands in for the live bridge: the
    # dead-port self-check must reach it, so the sync path is exercised
    import asyncio as _asyncio

    listener = await _asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    live_port = listener.sockets[0].getsockname()[1]  # pyright: ignore[reportUnknownMemberType]

    class _LiveBridge:
        port = live_port
        running = True
        _bot_url = "http://127.0.0.1:18789/bot/message"
        _token = ""

        def matches_connection(self, bot_url: str, token: str) -> bool:
            return self._bot_url == bot_url and self._token == token

    prov._bridges["napcat-1"] = cast(  # pyright: ignore[reportPrivateUsage]
        Any, _LiveBridge()
    )
    prov._sync_per_account_config = _fake_sync  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    # the sync returns False, but a fresh bridge still triggers the bounce
    # restart — mock it so the test never touches the real process table
    restarts: list[str] = []

    async def _fake_restart(iid: str, options: dict[str, Any]) -> None:
        restarts.append(iid)

    prov._restart_for_config = _fake_restart  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]

    try:
        await prov.ensure_bridge("napcat-1", {"bot_url": "http://127.0.0.1:18789/bot/message"})
        await asyncio.sleep(0.05)
        assert syncs == ["napcat-1"], (
            "healthy-poll path must run the per-account sync even when the bridge already exists"
        )
        # a healthy poll with an UNCHANGED config must not bounce the
        # child every cycle (that would restart NapCat every 60s); the
        # bounce belongs to bridge *creation* and repaired configs
        assert restarts == []
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_missing_qq_runtime_libs_detects_absent_gtk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preflight reports a package when its shared library is absent
    from the ldconfig cache, so minimal containers get the apt command
    instead of a cryptic 'major.node: cannot open shared object file'."""

    def _fake_ldconfig(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args[0] if args else [],
            0,
            stdout="\n".join(
                [
                    "\tlibnss3.so => /lib/x86_64-linux-gnu/libnss3.so",
                    "\tlibgbm.so.1 => /lib/x86_64-linux-gnu/libgbm.so.1",
                    "\tlibasound.so.2 => /lib/x86_64-linux-gnu/libasound.so.2",
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(gw_mod, "_missing_qq_runtime_libs", _REAL_MISSING_LIBS)
    monkeypatch.setattr(gw_mod.subprocess, "run", _fake_ldconfig)
    missing = gw_mod._missing_qq_runtime_libs()  # pyright: ignore[reportPrivateUsage]
    assert "libgtk-3-0" in missing  # absent -> reported
    assert "libnss3" not in missing  # present -> not reported
