"""NapCat provisioner tests: AppImage entry resolution and launch."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import mailflow_notify_onebot.gateway as gw_mod
import pytest
from mailflow_notify_onebot.gateway import (
    NapCatProvisioner,
    _instance_dir,  # pyright: ignore[reportPrivateUsage]
)


@pytest.fixture(autouse=True)
def _linux_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    # simulate the Linux host path (AppImage flow) regardless of the OS
    monkeypatch.setattr(gw_mod, "_IS_WINDOWS", False)


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
    assert captured["cwd"] == str(instance.resolve())


def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args[0] if args else [], 0, stdout="", stderr="")


async def _async_false() -> bool:
    return False
