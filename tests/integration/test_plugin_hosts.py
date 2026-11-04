"""Official host entry paths discover an installed fake entry point, with UI/server execution bounded."""

from __future__ import annotations

import asyncio
import importlib
import sys
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from mailflow.config import LLMConfig, MailFlowConfig, write_config
from mailflow.contracts import LLMCompletion, MessageDict
from mailflow.domain import ComponentKind
from mailflow.plugin_api import define_plugin


class FakeBackend:
    backend_id = "fake-contract"

    def __init__(self, config: LLMConfig) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[MessageDict],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMCompletion:
        self.calls.append(
            {"messages": messages, "temperature": temperature, "options": options, "tools": tools}
        )
        return LLMCompletion(text="local")


def fake_plugin(plugin_id: str = "mailflow-contract", backend: type[Any] = FakeBackend) -> Any:
    builder = define_plugin(plugin_id)
    builder.llm("fake-contract")(backend)
    return builder.build()


def install_fake_entry_point(monkeypatch: pytest.MonkeyPatch, plugin: Any) -> None:
    module = ModuleType("mailflow_contract_fixture")
    module.plugin = plugin  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    entry = metadata.EntryPoint(
        name=plugin.mailflow_plugin_info().plugin_id,
        value=f"{module.__name__}:plugin",
        group="mailflow.plugins",
    )
    monkeypatch.setattr(metadata, "entry_points", lambda: metadata.EntryPoints([entry]))


@pytest.mark.parametrize("host", ["command", "snapshot", "tui", "server", "export"])
def test_hosts_discover_external_plugins(
    host: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = importlib.import_module("mailflow_cli.app")
    install_fake_entry_point(monkeypatch, fake_plugin())
    config = MailFlowConfig()
    config.storage.path = str(tmp_path / "mail.db")
    config.logging.file = config.logging.console = False
    config.server.username, config.server.password = "validation", "validation"
    path = tmp_path / "config.toml"
    write_config(config, path)
    seen: list[bool] = []
    if host == "command":
        cli.command("plugin show mailflow-contract", config_path=str(path))
        assert "mailflow-contract" in capsys.readouterr().out
    elif host == "snapshot":
        cli.snapshot(config_path=str(path), json_output=True)
        assert '"plugin_id": "mailflow-contract"' in capsys.readouterr().out
    elif host == "tui":
        from mailflow_tui import runner

        class HeadlessApp:
            def __init__(self, service: Any, *args: Any, **kwargs: Any) -> None:
                seen.append(service.registry.has(ComponentKind.LLM_BACKEND, "fake-contract"))

            async def run_async(self) -> None:
                pass

        monkeypatch.setattr("mailflow_tui.app.MailFlowApp", HeadlessApp)
        asyncio.run(runner.run_local_host(str(path), with_server=False))
        assert seen == [True]
    elif host == "server":

        class HeadlessServer:
            def __init__(self, uv_config: Any) -> None:
                pass

            async def serve(self) -> None:
                pass

        def create_app(service: Any) -> Any:
            seen.append(service.registry.has(ComponentKind.LLM_BACKEND, "fake-contract"))
            return object()

        monkeypatch.setattr("uvicorn.Server", HeadlessServer)
        monkeypatch.setattr("mailflow_server.create_app", create_app)
        cli.serve(config_path=str(path), host=None, port=None)
        assert seen == [True]
    else:
        output = tmp_path / "export"
        assert cli.run_export(str(path), "nonebot", str(output)) == 0
        manifests = list(output.rglob("pyproject.toml"))
        assert len(manifests) == 1
        assert "mailflow-contract" in manifests[0].read_text(encoding="utf-8")
