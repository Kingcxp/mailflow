"""Unit tests for the plugin template generator."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from mailflow.plugin_template import CATEGORIES, scaffold_plugin, template_files

EXPECTED_FILES = ("plugin.json", "pyproject.toml")


@pytest.mark.parametrize("category", CATEGORIES)
def test_template_files_have_expected_structure(category: str) -> None:
    plugin_id = f"mailflow-demo-{category.replace('_', '-')}"
    files = template_files(plugin_id, category)
    for name in EXPECTED_FILES:
        assert name in files
    package = f"mailflow_demo_{category.replace('-', '_')}"
    assert f"src/{package}/plugin.py" in files
    assert f"src/{package}/__init__.py" in files

    metadata = json.loads(files["plugin.json"])
    assert metadata["id"] == plugin_id
    assert metadata["categories"] == [category]
    assert metadata["package"] == plugin_id
    assert "readme" in metadata

    pyproject = files["pyproject.toml"]
    assert f'{plugin_id} = "{package}.plugin:plugin"' in pyproject


@pytest.mark.parametrize("category", CATEGORIES)
def test_generated_module_compiles_and_loads(category: str, tmp_path: Path) -> None:
    kind_value = {
        "mail_source": "mail_source",
        "processor": "mail_processor",
        "llm_backend": "llm_backend",
        "notifier": "notifier",
        "storage": "storage",
    }[category]
    plugin_id = f"mailflow-demo-{category.replace('_', '-')}"
    target = scaffold_plugin(tmp_path / "plugin", plugin_id, category)
    module_path = target / "src" / f"mailflow_demo_{category.replace('-', '_')}" / "plugin.py"
    # compile + import in an isolated subprocess (avoids polluting the runner)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, sys;"
                f"spec = importlib.util.spec_from_file_location('tpl_{category}', r'{module_path}');"
                "m = importlib.util.module_from_spec(spec); sys.modules['tpl'] = m;"
                "spec.loader.exec_module(m);"
                "info = m.plugin.mailflow_plugin_info();"
                f"assert info.plugin_id == '{plugin_id}', info.plugin_id;"
                f"assert '{kind_value}' in [k.value for k in info.kinds]"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_scaffold_writes_into_target_directory(tmp_path: Path) -> None:
    target = scaffold_plugin(tmp_path / "sub" / "dir", "mailflow-my-thing", "notifier")
    assert target == tmp_path / "sub" / "dir"
    assert (target / "plugin.json").is_file()
    assert (target / "pyproject.toml").is_file()


def test_invalid_category_and_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown template category"):
        template_files("mailflow-x", "bogus")
    with pytest.raises(ValueError, match="plugin id"):
        template_files("Bad Plugin!", "notifier")


def test_processor_template_registers_via_registrar(tmp_path: Path) -> None:
    """The generated module registers a factory for its category."""
    target = scaffold_plugin(tmp_path, "mailflow-demo-processor", "processor")
    module_path = target / "src" / "mailflow_demo_processor" / "plugin.py"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, sys;"
                f"spec = importlib.util.spec_from_file_location('tpl', r'{module_path}');"
                "m = importlib.util.module_from_spec(spec); sys.modules['tpl'] = m;"
                "spec.loader.exec_module(m);"
                "from mailflow.registry import PluginRegistrar, ComponentRegistry;"
                "from mailflow.config import MailFlowConfig;"
                "r = PluginRegistrar(ComponentRegistry(), MailFlowConfig(), 'x');"
                "m.plugin.mailflow_register(r, MailFlowConfig());"
                "assert r._registry.processor_factory('demo-processor') is not None"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_readme_template_demonstrates_rich_markdown(tmp_path: Path) -> None:
    files = template_files("mailflow-demo-notifier", "notifier")
    readme = json.loads(files["plugin.json"])["readme"]
    assert '<span style="color:' in readme
    assert "~~strikethrough~~" in readme
    assert "**bold**" in readme
