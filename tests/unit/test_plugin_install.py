"""Installer behavior is tested with subprocess fakes, never a real market."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from mailflow.plugin_market import MarketPlugin, PluginMarket, detect_plugin_folders


@pytest.mark.parametrize("operation", ["install", "uninstall"])
async def test_installer_targets_host_python_and_resolves_dependencies(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="done", stderr="")

    monkeypatch.setattr("mailflow.plugin_market.shutil.which", Mock(return_value="uv"))
    monkeypatch.setattr("mailflow.plugin_market.subprocess.run", run)
    monkeypatch.setattr(PluginMarket, "is_installed", Mock(return_value=False))
    plugin = MarketPlugin(id="fake", package="fake-package", source="local-folder")
    assert await getattr(PluginMarket([]), operation)(plugin) == "done"
    assert calls[0][:3] == ["uv", "pip", operation]
    assert calls[0][calls[0].index("--python") + 1] == sys.executable
    assert "--no-deps" not in calls[0]


async def test_dependency_install_failure_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mailflow.plugin_market.shutil.which", Mock(return_value="uv"))
    monkeypatch.setattr(PluginMarket, "is_installed", Mock(return_value=False))
    monkeypatch.setattr(
        "mailflow.plugin_market.subprocess.run",
        Mock(
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
