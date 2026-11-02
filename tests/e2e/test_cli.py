"""Real CLI processes, real bundled plugins and SQLite, with no external services.

The built-in fake mail source is configured through TOML. Runtime, command routing,
storage, serialization and process startup are not replaced by test doubles.
"""

from __future__ import annotations

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
    return path


def run_cli(config: Path, *args: str, code: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            "-c",
            "from mailflow_cli.app import main; main()",
            *args,
            "--config",
            str(config),
        ],
        cwd=config.parent,
        env={**os.environ, "NO_COLOR": "1", "COLUMNS": "160", "PYTHONUTF8": "1"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    return result


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
