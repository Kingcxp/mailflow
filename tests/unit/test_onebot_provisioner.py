"""NapCat provisioner tests: AppImage entry resolution and launch."""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request as _ur
from pathlib import Path
from typing import Any, cast

import mailflow_notify_onebot.gateway as gw_mod
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
    monkeypatch.setattr(gw_mod, "_find_node", lambda: "node")
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


def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args[0] if args else [], 0, stdout="", stderr="")


async def _async_false() -> bool:
    return False


@pytest.mark.asyncio
async def test_appimage_install_falls_back_when_api_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Release API failure must not abort the install: it falls back to
    the pinned AppImage URL instead of raising."""
    monkeypatch.chdir(tmp_path)
    instance = _instance_dir("A-Bot-NapCat-5baf4a")  # pyright: ignore[reportPrivateUsage]
    instance.mkdir(parents=True, exist_ok=True)

    # API lookup raises (network unreachable) and the fallback download
    # writes the pinned asset
    def boom(*args: Any, **kwargs: Any) -> Any:  # pyright: ignore[reportUnknownParameterType]
        raise urllib.error.URLError("network unreachable")

    monkeypatch.setattr(_ur, "urlopen", boom)

    downloads: dict[str, Path] = {}

    def fake_download(url: str, destination: Path, progress: Any = None, **kw: Any) -> None:
        downloads[url] = destination
        destination.write_bytes(b"#!fake-appimage")

    monkeypatch.setattr(NapCatProvisioner, "_download", staticmethod(fake_download))

    # the runtime preflight checks xvfb-run/fusermount with shutil.which
    def _fake_which(name: str) -> str:
        return f"/usr/bin/{name}"

    monkeypatch.setattr(gw_mod.shutil, "which", _fake_which)

    prov = NapCatProvisioner()
    await prov._install_linux_appimage(  # pyright: ignore[reportPrivateUsage]
        "A-Bot-NapCat-5baf4a", {}, instance
    )

    # exactly one download, from the pinned release URL (no raise)
    assert len(downloads) == 1
    url = next(iter(downloads))
    assert url.startswith(
        "https://github.com/NapNeko/NapCatAppImageBuild/releases/download/v4.18.19/"
    )
    assert url.endswith(".AppImage")
    # file written with the pinned asset name
    appimage = instance / "QQ-50969_NapCat-v4.18.19-amd64.AppImage"
    assert appimage.exists()


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
    """A port occupied by an unmanaged process (a stale NapCat left
    over after a crash) must be killed and, when the kill cannot free
    the port, surface as an error — silently reusing it would report a
    phantom login."""
    monkeypatch.chdir(tmp_path)
    instance = _instance_dir("A-Bot-NapCat-5baf4a")  # pyright: ignore[reportPrivateUsage]
    instance.mkdir(parents=True, exist_ok=True)
    (instance / "QQ-50969_NapCat-v4.18.19-amd64.AppImage").write_bytes(b"x")

    monkeypatch.setattr(gw_mod, "_find_node", lambda: "node")
    monkeypatch.setattr(gw_mod, "_available_memory_mb", lambda: 4096.0)
    # the port answers HTTP but no process is in _processes -> stale
    monkeypatch.setattr(
        NapCatProvisioner,
        "_wait_http_port",
        lambda self, port, wait_seconds=2.0: _async_true(),  # pyright: ignore[reportUnknownLambdaType,reportUnknownArgumentType]
    )
    prov = NapCatProvisioner()
    # the stale-port owner lookup finds nothing (test sandbox has no
    # listener PID for the mocked port), so the fresh start still hits the
    # 'still in use' error after the kill attempt
    with pytest.raises(RuntimeError, match=r"still in use|could not terminate"):
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

    # HTTP probe fails (no server) -> falls through to QR payload;
    # logged_in stays False -> the cached uin is invalidated
    def _dead_endpoint(iid: str) -> str:
        return "http://127.0.0.1:1"

    monkeypatch.setattr(prov, "_endpoint", _dead_endpoint)

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


def test_looks_corrupt_detects_loader_failures() -> None:
    """Corruption fingerprints (missing shared objects, preload failures)
    must trigger the repair path; ordinary early exits must not."""
    assert gw_mod.NapCatProvisioner._looks_corrupt(  # pyright: ignore[reportPrivateUsage]
        "preload] failed. major.node: cannot open shared object file"
    )
    assert gw_mod.NapCatProvisioner._looks_corrupt(  # pyright: ignore[reportPrivateUsage]
        "error while loading shared libraries: libgtk-3.so.0"
    )
    assert gw_mod.NapCatProvisioner._looks_corrupt(  # pyright: ignore[reportPrivateUsage]
        "Trace/breakpoint trap"
    )
    # an OOM kill or a plain crash is NOT an install-corruption signature
    assert not gw_mod.NapCatProvisioner._looks_corrupt(  # pyright: ignore[reportPrivateUsage]
        "napcat exited early (code -9)"
    )


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
