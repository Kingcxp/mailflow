"""Real CLI processes, real bundled plugins and SQLite, with no external services.

The built-in fake mail source is configured through TOML. Runtime, command routing,
storage, serialization and process startup are not replaced by test doubles.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mailflow.config import MailFlowConfig, load_config, write_config


@pytest.fixture
def cli_config(tmp_path: Path) -> Path:
    config = MailFlowConfig.model_validate(
        {
            "general": {"auto_update": False, "ocr_images": False},
            "logging": {"console": False, "file": False, "jsonl": False},
            "storage": {"provider": "sqlite", "path": str(tmp_path / "mail.db")},
            "plugins": {
                "repositories": [],
                "enabled": [
                    "mailflow-mail-fake",
                    "mailflow-storage-sqlite",
                    "mailflow-export-nonebot",
                    "mailflow-export-astrbot",
                ],
            },
            "accounts": [
                {
                    "account_id": f"account-{label}",
                    "provider": "fake",
                    "options": {
                        "base_time": datetime.now(UTC).isoformat(),
                        "mails": [
                            {
                                "message_id": "shared-rfc-id",
                                "subject": f"CLI account {label}",
                                "body": f"Stored message from {label}",
                            }
                        ],
                    },
                }
                for label in ("alpha", "beta")
            ],
        }
    )
    path = tmp_path / "mailflow.toml"
    write_config(config, path)
    # Receiving belongs to a running host. Single CLI operations must only
    # inspect/change this persisted mailbox, never ingest the source again.
    seed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            "-c",
            "import asyncio,sys; from mailflow.config import load_config; "
            "from mailflow.service import start_service; "
            "from mailflow_bundled import create_plugin_manager\n"
            "async def seed():\n"
            " config=load_config(sys.argv[1])\n"
            " service=await start_service(config, plugin_manager=create_plugin_manager(config), "
            "discover_plugins=False, enable_logging=False)\n"
            " try:\n"
            "  async with asyncio.timeout(5):\n"
            "   while await service.count_mails() < 2: await asyncio.sleep(0.01)\n"
            "  await service.wait_idle()\n"
            " finally: await service.stop()\n"
            "asyncio.run(seed())",
            str(path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert seed.returncode == 0, seed.stdout + seed.stderr
    return path


def run_cli(
    config: Path, *args: str, code: int = 0, columns: int = 160
) -> subprocess.CompletedProcess[str]:
    arguments = (
        ["tui", "--config", str(config), *args[1:]]
        if args and args[0] == "tui"
        else [*args, "--config", str(config)]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            "-c",
            "from mailflow_cli.app import main; main()",
            *arguments,
        ],
        cwd=config.parent,
        env={**os.environ, "NO_COLOR": "1", "COLUMNS": str(columns), "PYTHONUTF8": "1"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    return result


async def test_tui_instances_preserve_screen_across_detach_reconnect_and_takeover(
    cli_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailflow_tui.session_terminal import read_frame, send_frame
    from mailflow_tui.sessions import (
        SessionError,
        SessionInfo,
        connect,
        find_instance,
        list_instances,
        request,
        stop_instance,
    )

    session_dir = cli_config.parent / "sessions"
    monkeypatch.setenv("MAILFLOW_SESSION_DIR", str(session_dir))
    config_before = await asyncio.to_thread(cli_config.read_bytes)
    run_cli(cli_config, "tui", "--background", "--name", "inbox", columns=80)
    info = await find_instance("inbox")

    async def output_until(reader: asyncio.StreamReader, expected: str) -> None:
        output = ""
        async with asyncio.timeout(8):
            while expected not in output:
                frame = await read_frame(reader)
                output += str(frame.get("data", ""))

    async def event_until(reader: asyncio.StreamReader, expected: str) -> None:
        async with asyncio.timeout(8):
            while (await read_frame(reader)).get("event") != expected:
                pass

    attached: list[asyncio.StreamWriter] = []
    second: SessionInfo | None = None
    try:
        listing = json.loads(run_cli(cli_config, "tui", "list", "--json").stdout)
        assert [(row["name"], row["state"]) for row in listing] == [("inbox", "detached")]
        assert "token" not in listing[0]
        run_cli(cli_config, "tui", "--background", "--name", "inbox")
        assert (await find_instance("inbox")).pid == info.pid
        run_cli(cli_config, "tui", "--background", "--name", "duplicate", code=1)
        with pytest.raises(SessionError, match=r"sessions\.auth_failed"):
            await request(dataclasses.replace(info, token="wrong"), "stop")
        assert (await request(info, "status"))["state"] == "detached"

        reader, writer, _ = await connect(info, "attach")
        attached.append(writer)
        send_frame(writer, {"event": "Resize", "size": [120, 40]})
        await output_until(reader, "CLI account")
        assert (await request(info, "status"))["state"] == "attached"
        with pytest.raises(SessionError, match=r"sessions\.attached"):
            await connect(info, "attach")
        send_frame(writer, {"event": "Key", "key": "f1", "character": None})
        await output_until(reader, "Keyboard help")

        # A new terminal takes over the existing modal instead of composing a new TUI.
        next_reader, next_writer, _ = await connect(info, "attach", takeover=True)
        attached.append(next_writer)
        await event_until(reader, "detached")
        await output_until(next_reader, "Keyboard help")
        send_frame(next_writer, {"event": "Key", "key": "f8", "character": None})
        await event_until(next_reader, "detached")
        assert (await request(info, "status"))["state"] == "detached"

        reader, writer, _ = await connect(info, "attach")
        attached.append(writer)
        await output_until(reader, "Keyboard help")
        run_cli(cli_config, "tui", "detach", "inbox")
        await event_until(reader, "detached")
        assert (await find_instance("inbox")).pid == info.pid

        # Losing the terminal also detaches; neither this nor a second instance stops mail.
        _reader, writer, _ = await connect(info, "attach")
        writer.close()
        await writer.wait_closed()
        async with asyncio.timeout(3):
            while (await request(info, "status"))["state"] != "detached":  # noqa: ASYNC110 -- observe another process
                await asyncio.sleep(0.01)
        second_config = cli_config.parent / "second.toml"
        config = load_config(cli_config)
        config.storage.path = str(cli_config.parent / "second.db")
        write_config(config, second_config)
        run_cli(second_config, "tui", "--background", "--name", "second")
        second = await find_instance("second")
        assert second.pid != info.pid and second.port != info.port
        assert len(await list_instances()) == 2
        run_cli(cli_config, "tui", "stop", code=1)
        run_cli(cli_config, "tui", "stop", "inbox")
        assert [(item.name, state) for item, state in await list_instances()] == [
            ("second", "detached")
        ]
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", info.port)
        # Stopping releases the database/name locks and preserves the real config.
        run_cli(cli_config, "tui", "--background", "--name", "inbox")
        assert (await find_instance("inbox")).pid != info.pid
        assert await asyncio.to_thread(cli_config.read_bytes) == config_before

        # An invalid host config exits visibly and never appears as a live instance.
        config.storage.path = str(cli_config.parent / "broken.db")
        config.storage.provider = "missing-storage"
        broken = cli_config.parent / "broken.toml"
        write_config(config, broken)
        failed = run_cli(broken, "tui", "--background", "--name", "broken", code=1)
        assert any(str(log) in failed.stdout for log in session_dir.glob("*.log"))
        assert {item.name for item, _state in await list_instances()} == {"inbox", "second"}

        # The TUI's exit action shuts down the host, rather than leaving it detached.
        reader, writer, _ = await connect(second, "attach")
        attached.append(writer)
        send_frame(writer, {"event": "Key", "key": "ctrl+q", "character": None})
        await event_until(reader, "stopped")
        assert {item.name for item, _state in await list_instances()} == {"inbox"}
    finally:
        for writer in attached:
            writer.close()
            await writer.wait_closed()
        for remaining, _state in await list_instances():
            await stop_instance(remaining)


def test_cli_restarts_preserve_account_scoped_mail_priority_and_actions(cli_config: Path) -> None:
    first = run_cli(cli_config, "command", "mail list").stdout
    for label in ("alpha", "beta"):
        assert first.count(f"CLI account {label}") == 1

    run_cli(cli_config, "command", "mail urgency 1 urgent")
    detail = run_cli(cli_config, "command", "mail show 1").stdout
    assert "urgent" in detail and "Stored message from" in detail

    run_cli(cli_config, "command", 'action add "CLI persistent task" --due "2099-11-02 09:00"')
    assert "CLI persistent task" in run_cli(cli_config, "command", "action list").stdout
    run_cli(cli_config, "command", "action delete 1")
    assert "CLI persistent task" not in run_cli(cli_config, "command", "action list").stdout

    again = run_cli(cli_config, "command", "mail list").stdout
    for label in ("alpha", "beta"):
        assert again.count(f"CLI account {label}") == 1
    snapshot = json.loads(run_cli(cli_config, "snapshot", "--json").stdout)
    assert {account["account_id"] for account in snapshot["accounts"]} == {
        "account-alpha",
        "account-beta",
    }


@pytest.mark.parametrize("framework", ["nonebot", "astrbot"])
def test_cli_export_produces_loadable_config_and_runtime_dependencies(
    cli_config: Path, framework: str
) -> None:
    output = cli_config.parent / framework
    run_cli(cli_config, "export", "--framework", framework, "--output", str(output))
    if framework == "nonebot":
        embedded = output / "src/nonebot_plugin_mailflow/config.toml"
        project = tomllib.loads((output / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = project["project"]["dependencies"]
        compile(
            (output / "src/nonebot_plugin_mailflow/__init__.py").read_text(encoding="utf-8"),
            "exported_nonebot",
            "exec",
        )
    else:
        embedded = output / "config.toml"
        dependencies = (output / "requirements.txt").read_text(encoding="utf-8").splitlines()
        compile((output / "main.py").read_text(encoding="utf-8"), "exported_astrbot", "exec")
    restored = load_config(embedded)
    assert {account.account_id for account in restored.accounts} == {
        "account-alpha",
        "account-beta",
    }
    assert any("mailflow-storage-sqlite" in dependency for dependency in dependencies)
    assert not any("mailflow-export-" in dependency for dependency in dependencies)


def test_cli_business_failure_exits_nonzero_without_changing_data(cli_config: Path) -> None:
    run_cli(cli_config, "command", "mail list")
    failure = run_cli(cli_config, "command", "mail show missing-mail-37", code=1)
    assert "missing-mail-37" in failure.stdout
    remaining = run_cli(cli_config, "command", "mail list").stdout
    assert "CLI account alpha" in remaining and "CLI account beta" in remaining
    output = cli_config.parent / "invalid-export"
    rejected = run_cli(
        cli_config, "export", "--framework", "missing-framework", "--output", str(output), code=1
    )
    assert "missing-framework" in rejected.stdout
    assert "nonebot" in rejected.stdout and "astrbot" in rejected.stdout
    assert not output.exists()


def test_clean_commands_do_not_receive_mail_again_or_wait_for_runtime(cli_config: Path) -> None:
    import time

    started = time.monotonic()
    run_cli(cli_config, "command", "clean records")
    run_cli(cli_config, "command", "clean trash CONFIRM")
    assert time.monotonic() - started < 10
    assert "CLI account" not in run_cli(cli_config, "command", "mail list").stdout
    assert "CLI account" not in run_cli(cli_config, "command", "trash list").stdout
    snapshot = json.loads(run_cli(cli_config, "snapshot", "--json").stdout)
    assert all(account["status"] == "stopped" for account in snapshot["accounts"])
