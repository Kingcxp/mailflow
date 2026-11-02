"""The shared validator loads source entry points and rejects actual contract failures."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("behavior", ["valid", "missing-tools", "wrong-result", "real-io"])
def test_source_contract_validation_is_offline_and_checks_behavior(
    tmp_path: Path, behavior: str
) -> None:
    root = tmp_path / "mailflow-contract-fixture"
    source = root / "src"
    source.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "mailflow-contract-fixture"\nversion = "0.1.0"\n'
        '[project.entry-points."mailflow.plugins"]\nmailflow-contract-fixture = "contract_fixture:plugin"\n',
        encoding="utf-8",
    )
    signature = "self, messages, *, temperature=None, options=None, tools=None"
    body = "return LLMCompletion(text='fixture', tool_calls=[ToolCall(call_id='fixture', name='find_mail')])"
    if behavior == "missing-tools":
        signature = "self, messages, *, temperature=None, options=None"
    elif behavior == "wrong-result":
        body = "return None"
    elif behavior == "real-io":
        body = "import socket; socket.create_connection(('example.invalid', 443))"
    code = (
        "from mailflow.plugin_api import define_plugin\n"
        "from mailflow.contracts import LLMCompletion, ToolCall\n"
        "builder = define_plugin('mailflow-contract-fixture', name='Fixture', version='0.1.0')\n"
        "@builder.llm('fixture')\nclass Backend:\n"
        "    backend_id = 'fixture'\n"
        "    def __init__(self, config): pass\n"
        f"    async def chat({signature}):\n        {body}\n"
        "plugin = builder.build()\n"
    )
    (source / "contract_fixture.py").write_text(code, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            "-m",
            "mailflow_testkit.plugin_smoke",
            str(root),
            "--source",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=15,
    )
    output = result.stdout + result.stderr
    if behavior == "valid":
        assert result.returncode == 0, output
    else:
        assert result.returncode != 0
        assert "mailflow-contract-fixture" in output
        if behavior == "missing-tools":
            assert "tools" in output
        elif behavior == "real-io":
            assert "real I/O forbidden" in output
    assert not list(source.glob("__pycache__"))
