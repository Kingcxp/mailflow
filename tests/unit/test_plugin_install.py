"""Installer behavior is tested with subprocess fakes, never a real market."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from mailflow.plugin_market import MarketPlugin, PluginMarket, detect_plugin_folders


@pytest.mark.parametrize("operation", ["install", "uninstall"])
async def test_installer_targets_host_python_and_resolves_dependencies(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailflow import gateway_resources

    calls: list[list[str]] = []
    entered, release = threading.Event(), threading.Event()
    release.set()

    class Process:
        returncode: int | None = None
        terminated = False

        def communicate(self) -> tuple[bytes, bytes]:
            entered.set()
            release.wait(timeout=2)
            if self.returncode is None:
                self.returncode = 0
            return b"done", b""

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15
            release.set()

    processes: list[Process] = []

    def launch(args: list[str], **kwargs: Any) -> Any:
        calls.append(args)
        process = Process()
        processes.append(process)
        return process

    def terminate(process: Process, **_kwargs: Any) -> None:
        process.terminate()

    monkeypatch.setattr("mailflow.plugin_market.shutil.which", Mock(return_value="uv"))
    monkeypatch.setattr(gateway_resources.subprocess, "Popen", launch)
    monkeypatch.setattr(gateway_resources, "terminate_process", terminate)
    monkeypatch.setattr(PluginMarket, "is_installed", Mock(return_value=False))
    plugin = MarketPlugin(id="fake", package="fake-package", source="local-folder")
    assert await getattr(PluginMarket([]), operation)(plugin) == "done"
    assert calls[0][:3] == ["uv", "pip", operation]
    assert calls[0][calls[0].index("--python") + 1] == sys.executable
    assert "--no-deps" not in calls[0]
    entered.clear()
    release.clear()
    task = asyncio.create_task(getattr(PluginMarket([]), operation)(plugin))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[-1].terminated
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_dependency_install_failure_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mailflow.plugin_market.shutil.which", Mock(return_value="uv"))
    monkeypatch.setattr(PluginMarket, "is_installed", Mock(return_value=False))
    monkeypatch.setattr(
        "mailflow.plugin_market.run_command",
        AsyncMock(
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="missing-dependency")
        ),
    )
    with pytest.raises(RuntimeError, match=r"fake.*missing-dependency"):
        await PluginMarket([]).install(MarketPlugin(id="fake", package="fake", source="local"))


def test_local_repository_and_standalone_project_are_detected(tmp_path: Path) -> None:
    plugin = tmp_path / "notifier/mailflow-fake"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text('[project]\nname = "mailflow-fake"\n', encoding="utf-8")
    assert detect_plugin_folders(tmp_path) == [plugin]
    assert detect_plugin_folders(plugin) == [plugin]
