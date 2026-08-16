"""The CLI `export` command body (run_export) — a pure function kept free of
typer so it is directly testable."""

from __future__ import annotations

from pathlib import Path

import pytest
from mailflow_cli.app import run_export


def test_run_export_nonebot_writes_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_export(None, "nonebot", str(tmp_path))
    assert code == 0
    assert (tmp_path / "pyproject.toml").is_file()
    assert (tmp_path / "src" / "nonebot_plugin_mailflow" / "config.toml").is_file()
    out = capsys.readouterr().out
    assert "Exported nonebot-plugin-mailflow" in out
    assert "src/nonebot_plugin_mailflow/__init__.py" in out
    # runtime plugins are declared, exporter plugins are not
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "mailflow-storage-sqlite" in pyproject
    assert "mailflow-export-nonebot" not in pyproject
    assert "mailflow-export-astrbot" not in pyproject


def test_run_export_astrbot_writes_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_export(None, "astrbot", str(tmp_path))
    assert code == 0
    assert (tmp_path / "main.py").is_file()
    assert (tmp_path / "metadata.yaml").is_file()
    out = capsys.readouterr().out
    assert "Exported astrbot_plugin_mailflow" in out


def test_run_export_unknown_framework_lists_available(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_export(None, "missing-framework", str(tmp_path))
    assert code == 1
    out = capsys.readouterr().out
    assert "No exporter registered" in out
    assert "nonebot" in out and "astrbot" in out


def test_run_export_embeds_configured_accounts(tmp_path: Path) -> None:
    import tomllib

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[[accounts]]\naccount_id = "acct-1"\nprovider = "fake"\nemail = "me@example.com"\n',
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    code = run_export(str(config_file), "nonebot", str(out_dir))
    assert code == 0
    embedded = out_dir / "src" / "nonebot_plugin_mailflow" / "config.toml"
    with embedded.open("rb") as handle:
        data = tomllib.load(handle)
    assert data["accounts"][0]["account_id"] == "acct-1"
