"""Build commands and cleanup boundaries without running compiler or package downloads."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_tool(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleanup_removes_artifacts_without_entering_environments_or_user_data(
    tmp_path: Path,
) -> None:
    cleaner = load_tool("clean")
    removed = [
        "build/reports/pytest.xml",
        "dist/wheels/app.whl",
        "htmlcov/index.html",
        "pkg/__pycache__/x.pyc",
        "pkg/demo.egg-info/PKG-INFO",
        ".coverage.worker",
        "pytest.xml",
        "dist-wheels/old.whl",
    ]
    kept = [
        "mail.db",
        "source.py",
        "configs/development.toml",
        "data/cache/.coverage",
        "logs/app.log",
        ".git/cache/.coverage",
        ".venv/lib/build/data.db",
        ".venv312/lib/__pycache__/x.pyc",
        "nested/.git/HEAD",
        "nested/__pycache__/x.pyc",
    ]
    for name in removed + kept:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep or remove", encoding="utf-8")
    cleaner.clean_artifacts(tmp_path)
    assert not any((tmp_path / name).exists() for name in removed)
    assert all((tmp_path / name).read_text(encoding="utf-8") == "keep or remove" for name in kept)
    assert cleaner.clean_artifacts(tmp_path) == ([], [])


def test_cleanup_does_not_follow_an_artifact_link_outside_the_workspace(tmp_path: Path) -> None:
    cleaner = load_tool("clean")
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "evidence").write_text("kept", encoding="utf-8")
    try:
        (root / "dist").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating directory links requires an OS permission")
    cleaner.clean_artifacts(root)
    assert (outside / "evidence").read_text(encoding="utf-8") == "kept"


def test_config_cleanup_needs_confirmation_and_keeps_the_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleaner = load_tool("clean")
    monkeypatch.setattr(cleaner, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    names = ["development.toml", "local.toml", "custom.local.toml", "example.toml"]
    (tmp_path / "configs").mkdir()
    for name in names:
        (tmp_path / "configs" / name).write_text("data", encoding="utf-8")
    assert cleaner.clean_target("config") == 0
    assert all((tmp_path / "configs" / name).exists() for name in names)
    assert cleaner.clean_target("config", force=True) == 0
    assert sorted(path.name for path in (tmp_path / "configs").iterdir()) == ["example.toml"]


def test_wheel_build_uses_the_workspace_from_another_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = load_tool("build_all")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["build_all", "--offline"])

    def fake_uv(name: str) -> str:
        return "/fake/uv"

    monkeypatch.setattr(builder.shutil, "which", fake_uv)
    calls: list[tuple[list[str], Path]] = []

    def run(command: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder.subprocess, "run", run)
    builder.main()
    command, cwd = calls[0]
    assert command[:2] == ["/fake/uv", "build"]
    assert {"--all-packages", "--wheel", "--offline"}.issubset(command)
    assert command[command.index("--out-dir") + 1] == str(ROOT / "dist/wheels")
    assert command[command.index("--python") + 1] == sys.executable
    assert cwd == ROOT


def test_release_version_mismatch_stops_before_building(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = load_tool("build_all")
    monkeypatch.setattr(sys, "argv", ["build_all", "--version", "v999.0"])
    with pytest.raises(SystemExit, match="2"):
        builder.main()


@pytest.mark.parametrize("mode", ["standalone", "onefile"])
@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_frozen_build_includes_every_runtime_package_once(
    mode: str, platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = load_tool("build_exe")
    monkeypatch.setattr(sys, "argv", ["build_exe", "--mode", mode])
    monkeypatch.setattr(
        builder, "sys", SimpleNamespace(executable=sys.executable, platform=platform)
    )
    commands: list[list[str]] = []

    def run(
        command: list[str], *, cwd: Path, check: bool, stdin: int
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == ROOT
        assert stdin == subprocess.DEVNULL
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder.subprocess, "run", run)
    builder.main()
    command = commands[0]
    expected: list[str] = []
    for folder in [*ROOT.glob("packages/*"), *ROOT.glob("plugins/*")]:
        project_file = folder / "pyproject.toml"
        if project_file.is_file() and folder.name != "mailflow-testkit":
            project = tomllib.loads(project_file.read_text(encoding="utf-8"))
            expected.extend(
                Path(p).name
                for p in project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
            )
    included = [
        value.split("=", 1)[1] for value in command if value.startswith("--include-package=")
    ]
    assert sorted(included) == sorted(expected)
    assert len(included) == len(set(included))
    assert f"--{mode}" in command
    assert f"--output-dir={ROOT / 'dist' / mode}" in command
    filename = "mailflow.exe" if platform == "win32" else "mailflow"
    assert f"--output-filename={filename}" in command
    assert "--assume-yes-for-downloads" not in command
