"""Shared command surface for ``mailflow tui`` and ``mailflow-tui``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import typer
from mailflow.config import MailFlowConfig, load_config
from mailflow.i18n import I18n
from rich.console import Console
from rich.table import Table

app = typer.Typer(invoke_without_command=True, no_args_is_help=False)
console = Console()


def _execute(function: Callable[[], Any], config_path: str | None = None) -> None:
    from mailflow_tui.sessions import SessionError

    t = I18n().t
    try:
        config = load_config(config_path) if config_path else MailFlowConfig()
        t = I18n(config.general.language).t
        function()
    except SessionError as exc:
        console.print(t(exc.key, **exc.params), style="red", markup=False)
        raise typer.Exit(1) from exc
    except (OSError, ValueError, EOFError, TimeoutError) as exc:
        console.print(t("sessions.unavailable", name=""), style="red", markup=False)
        raise typer.Exit(1) from exc


@app.callback()
def launch(
    ctx: typer.Context,
    config_path: str | None = typer.Option(None, "--config", "-c"),
    background: bool = typer.Option(
        False, "--background", "-d", help="Keep the TUI running in the background"
    ),
    name: str | None = typer.Option(None, "--name", "-s", help="Name this TUI instance"),
    local: bool = typer.Option(False, "--local", help="Also start the admin REST+WS server"),
    remote: str | None = typer.Option(None, "--remote", help="Connect to a remote MailFlow server"),
) -> None:
    """Start a persistent TUI; F8 detaches and Ctrl+Q stops it."""
    ctx.obj = config_path
    if ctx.invoked_subcommand is None:
        from mailflow_tui.runner import run_tui

        _execute(
            lambda: run_tui(
                config_path, local=local, remote_url=remote, background=background, name=name
            ),
            config_path,
        )


@app.command("list")
def list_sessions(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Print instance information as JSON"),
) -> None:
    """List this user's running TUI instances without starting a service."""
    from mailflow_tui.sessions import list_instances

    def show() -> None:
        config = load_config(ctx.obj) if ctx.obj else MailFlowConfig()
        t = I18n(config.general.language).t
        items = asyncio.run(list_instances())
        if json_output:
            console.print_json(
                json.dumps(
                    [
                        {
                            "id": info.id,
                            "name": info.name,
                            "state": state,
                            "pid": info.pid,
                            "config": info.config,
                            "cwd": info.cwd,
                            "created_at": info.created_at,
                        }
                        for info, state in items
                    ]
                )
            )
            return
        if not items:
            console.print(t("sessions.empty"), markup=False)
            return
        table = Table()
        for key in ("name", "state", "pid", "config"):
            table.add_column(t("sessions.header_" + key))
        for info, state in items:
            table.add_row(
                info.name, t("sessions.state_" + state), str(info.pid), info.config or "—"
            )
        console.print(table)

    _execute(show, ctx.obj)


def _session_action(
    ctx: typer.Context, action: str, name: str | None, takeover: bool = False
) -> None:
    from mailflow_tui.sessions import attach_instance, find_instance, request, stop_instance

    async def run() -> None:
        info = await find_instance(name)
        t = I18n(info.language).t
        if action == "attach":
            result = await attach_instance(info, takeover=takeover)
        elif action == "detach":
            await request(info, "detach")
            result = "detached"
        else:
            await stop_instance(info)
            result = "stopped"
        console.print(t("sessions." + result, name=info.name), markup=False)

    _execute(lambda: asyncio.run(run()), ctx.obj)


@app.command()
def attach(
    ctx: typer.Context,
    name: str | None = typer.Argument(
        None, help="Instance name or ID; optional when only one exists"
    ),
    takeover: bool = typer.Option(
        False, "--takeover", help="Detach the previous terminal before attaching"
    ),
) -> None:
    """Reconnect to an existing TUI, preserving its current screen and tasks."""
    _session_action(ctx, "attach", name, takeover)


@app.command()
def detach(ctx: typer.Context, name: str | None = typer.Argument(None)) -> None:
    """Detach an instance's terminal while leaving the TUI and service running."""
    _session_action(ctx, "detach", name)


@app.command()
def stop(ctx: typer.Context, name: str | None = typer.Argument(None)) -> None:
    """Stop an instance and release its mail tasks and gateway resources."""
    _session_action(ctx, "stop", name)
