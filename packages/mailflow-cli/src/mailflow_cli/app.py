"""MailFlow CLI host.

All business behavior is delegated to the Core service facade and command
router — callbacks contain no mail/reply/urgency logic of their own. Every
started service is stopped in ``finally``.

# pyright: basic — typer's decorator stubs are too loose for strict mode;
# this glue file carries no business logic to type-check strictly.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer
from mailflow.commands import CommandRouter
from mailflow.config import MailFlowConfig, load_config
from mailflow.plugins import PluginManager
from mailflow.service import start_service
from mailflow_bundled import create_plugin_manager
from rich.console import Console
from rich.table import Table
from rich.text import Text

app = typer.Typer(
    name="mailflow",
    help="MailFlow: unified multi-account mail inbox with LLM analysis.",
    no_args_is_help=True,
)

console = Console()


def _load_config(config_path: str | None) -> MailFlowConfig:
    if config_path:
        return load_config(config_path)
    return MailFlowConfig()


def _render_response(response: Any) -> None:
    """Render a transport-neutral CommandResponse with rich styling."""
    text = Text()
    for span in response.spans:
        text.append(span.text, style=span.style or None)
    console.print(text)


@app.command()
def run(
    config_path: str | None = typer.Option(
        None, "--config", "-c", help="Path to the TOML config file (defaults used when omitted)"
    ),
) -> None:
    """Start the service in the foreground (Ctrl+C to stop)."""

    async def _run() -> None:
        config = _load_config(config_path)
        service = await start_service(config)
        try:
            console.print(service.t("cli.started"))
            await service.wait()
        finally:
            await service.stop()
            console.print(service.t("cli.stopped"))

    from contextlib import suppress

    with suppress(KeyboardInterrupt):
        asyncio.run(_run())


@app.command()
def command(
    command_text: str = typer.Argument(..., help="One MailFlow command, e.g. 'mail list'"),
    config_path: str | None = typer.Option(
        None, "--config", "-c", help="Path to the TOML config file (defaults used when omitted)"
    ),
) -> None:
    """Execute a single MailFlow command and print the result."""

    async def _run() -> None:
        config = _load_config(config_path)
        manager = create_plugin_manager(config, discover_external=False)
        service = await start_service(
            config, plugin_manager=manager, discover_plugins=False, enable_logging=False
        )
        router = CommandRouter(service)
        try:
            response = await router.execute(command_text)
            _render_response(response)
        finally:
            await service.stop()

    asyncio.run(_run())


@app.command()
def shell(
    config_path: str | None = typer.Option(
        None, "--config", "-c", help="Path to the TOML config file (defaults used when omitted)"
    ),
) -> None:
    """Start an interactive MailFlow command shell."""

    async def _run() -> None:
        config = _load_config(config_path)
        manager = create_plugin_manager(config, discover_external=False)
        service = await start_service(config, plugin_manager=manager, discover_plugins=False)
        router = CommandRouter(service)
        try:
            console.print(service.t("cli.shell_welcome"))
            while True:
                try:
                    line = await asyncio.to_thread(input, service.t("cli.shell_prompt"))
                except (EOFError, KeyboardInterrupt):
                    console.print(service.t("cli.shell_goodbye"))
                    return
                line = line.strip()
                if not line:
                    continue
                if line in ("exit", "quit"):
                    console.print(service.t("cli.shell_goodbye"))
                    return
                response = await router.execute(line)
                _render_response(response)
        finally:
            await service.stop()

    asyncio.run(_run())


@app.command()
def config_check(
    config_path: str | None = typer.Option(
        None, "--config", "-c", help="Path to the TOML config file (defaults used when omitted)"
    ),
) -> None:
    """Validate the configuration file without any network or service start."""
    try:
        load_config(config_path)
    except (OSError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print("[green]Config OK[/green]")


@app.command()
def snapshot(
    config_path: str | None = typer.Option(
        None, "--config", "-c", help="Path to the TOML config file (defaults used when omitted)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the snapshot as JSON"),
) -> None:
    """Show a snapshot of the running system: plugins, accounts, LLMs, bindings."""
    config = _load_config(config_path)

    async def _run() -> None:
        manager = create_plugin_manager(config, discover_external=False)
        service = await start_service(
            config, plugin_manager=manager, discover_plugins=False, enable_logging=False
        )
        try:
            snapshot_data = service.snapshot()
            if json_output:
                print(
                    json.dumps(snapshot_data.model_dump(mode="json"), ensure_ascii=False, indent=2)
                )
                return
            table = Table(title=service.t("cli.snapshot_title"))
            table.add_column(service.t("plugin.header_id"), style="cyan")
            table.add_column(service.t("plugin.header_name"))
            table.add_column(service.t("account.header_status"))
            for plugin in snapshot_data.plugins:
                table.add_row(plugin.plugin_id, plugin.name, "")
            console.print(table)
            account_table = Table(
                title=service.t("account.title", count=len(snapshot_data.accounts))
            )
            account_table.add_column(service.t("account.header_id"), style="cyan")
            account_table.add_column(service.t("account.header_email"))
            account_table.add_column(service.t("account.header_provider"))
            account_table.add_column(service.t("account.header_status"))
            for account in snapshot_data.accounts:
                account_table.add_row(
                    account.account_id, account.email, account.provider, account.status
                )
            console.print(account_table)
        finally:
            await service.stop()

    asyncio.run(_run())


@app.command()
def doctor(
    config_path: str | None = typer.Option(
        None, "--config", "-c", help="Path to the TOML config file (defaults used when omitted)"
    ),
) -> None:
    """Summarize registrations and configuration without network calls."""
    config = _load_config(config_path)
    manager = create_plugin_manager(config)
    registry = manager.build_registry()
    console.print(service_doctor(config, manager, registry))


def service_doctor(config: MailFlowConfig, manager: PluginManager, registry: Any) -> Text:
    """Build the doctor text (kept pure for testing)."""
    enabled_plugins = manager.enabled_infos()
    components = registry.snapshots()
    text = Text()
    text.append("MailFlow doctor\n", style="bold cyan")
    text.append(f"  plugins registered: {len(enabled_plugins)}\n")
    text.append(f"  components: {len(components)}\n")
    text.append(f"  accounts configured: {len(config.accounts)}\n")
    text.append(f"  llms configured: {len(config.llms)}\n")
    text.append(f"  processors configured: {len(config.processors)}\n")
    text.append(f"  notifiers configured: {len(config.notifiers)}\n")
    text.append(f"  storage provider: {config.storage.provider}\n")
    return text


def main() -> None:
    app()


if __name__ == "__main__":
    main()
