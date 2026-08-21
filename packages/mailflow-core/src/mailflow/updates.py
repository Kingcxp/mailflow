"""Version checking and updates for MailFlow itself and installed plugins.

- MailFlow updates follow GitHub releases (urllib, no auth): the latest
  release tag is compared with ``mailflow.__version__``.
- Plugin updates follow the marketplace ``plugin.json`` version for the
  plugin's recorded update source. Plugins without a remote source (local
  installs, or a repository that was removed) are never auto-updated.
- Applying updates reuses :meth:`PluginMarket.install` (``uv pip install
  --no-deps``); MailFlow itself is upgraded through ``uv pip install
  --upgrade`` of the installed mailflow distributions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any
from urllib.request import Request, urlopen

from mailflow import __version__
from mailflow.plugin_market import PluginMarket

logger = logging.getLogger("mailflow.updates")

_GITHUB_RELEASE_API = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
_FETCH_TIMEOUT = 15.0

_MAILFLOW_PACKAGES = (
    "mailflow-core",
    "mailflow-bundled",
    "mailflow-cli",
    "mailflow-tui",
    "mailflow-testkit",
)


@dataclass
class UpdateReport:
    """Result of one update check."""

    mailflow_current: str
    mailflow_latest: str = ""
    mailflow_update: bool = False
    plugin_updates: dict[str, tuple[str, str]] = field(default_factory=lambda: {})

    @property
    def has_updates(self) -> bool:
        return self.mailflow_update or bool(self.plugin_updates)


def _fetch_json(url: str, timeout: float = _FETCH_TIMEOUT) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "mailflow"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _version_key(version: str) -> tuple[tuple[int, Any], ...]:
    """Sort key for a release version.

    Numeric components rank above non-numeric ones, so a real release
    always compares newer than an odd tag ("nightly-x"); equal versions
    written with different depth ("1.0" vs "1.0.0") must not differ.
    """
    parts: list[tuple[int, Any]] = []
    for part in version.strip().lstrip("v").split("."):
        number = re.fullmatch(r"\d+", part)
        parts.append((1, int(number.group(0))) if number else (0, part))
    return tuple(parts)


def _version_newer(candidate: str, current: str) -> bool:
    """True when ``candidate`` is a strictly newer release than ``current``."""
    candidate_key = _version_key(candidate)
    current_key = _version_key(current)
    width = max(len(candidate_key), len(current_key))
    pad = ((1, 0),) * (width - len(candidate_key))
    return candidate_key + pad > current_key + ((1, 0),) * (width - len(current_key))


def latest_mailflow_release(
    current: str = __version__, owner: str = "Kingcxp", repo: str = "mailflow"
) -> str:
    """The latest release tag, or ``current`` when the API is unreachable."""
    try:
        payload = _fetch_json(_GITHUB_RELEASE_API.format(owner=owner, repo=repo))
        tag = str(payload.get("tag_name", "")).lstrip("v")
        return tag or current
    except Exception as exc:
        logger.warning("mailflow release check failed: %s", exc)
        return current


def installed_plugin_versions(group: str = "mailflow.plugins") -> dict[str, str]:
    """Map installed plugin entry-point ids to their distribution versions."""
    versions: dict[str, str] = {}
    for entry_point in metadata.entry_points().select(group=group):
        distribution = entry_point.dist
        if distribution is None:
            continue
        versions[entry_point.name] = distribution.version
    return versions


def check_plugin_updates(
    market: PluginMarket,
    installed: dict[str, str],
    sources: dict[str, str],
) -> dict[str, tuple[str, str]]:
    """plugin_id -> (installed, repository) for plugins with a remote source
    whose repository version is newer. Local/unknown-source plugins and
    plugins whose repository disappeared are left untouched."""
    updates: dict[str, tuple[str, str]] = {}
    for plugin_id, old in installed.items():
        source = sources.get(plugin_id, "")
        if not source or not source.startswith(("git+", "https://", "http://")):
            continue
        found = market.find(plugin_id)
        if found is None:
            continue
        _repository, plugin = found
        if plugin.version and _version_newer(plugin.version, old):
            updates[plugin_id] = (old, plugin.version)
    return updates


def check_updates(
    market: PluginMarket,
    *,
    installed_plugins: dict[str, str] | None = None,
    sources: dict[str, str] | None = None,
    mailflow_current: str = __version__,
) -> UpdateReport:
    """Full update check: MailFlow release tag plus marketplace plugin versions."""
    installed = installed_plugins or installed_plugin_versions()
    latest = latest_mailflow_release(mailflow_current)
    plugin_updates = check_plugin_updates(market, installed, sources or {})
    return UpdateReport(
        mailflow_current=mailflow_current,
        mailflow_latest=latest,
        mailflow_update=_version_newer(latest, mailflow_current),
        plugin_updates=plugin_updates,
    )


async def apply_plugin_updates(
    market: PluginMarket, updates: dict[str, tuple[str, str]]
) -> dict[str, str]:
    """Install the newer plugin versions; returns plugin_id -> outcome."""
    results: dict[str, str] = {}
    for plugin_id, (_old, _new) in updates.items():
        found = market.find(plugin_id)
        if found is None:
            results[plugin_id] = "skipped: source repository gone"
            continue
        _repository, plugin = found
        try:
            output = await market.install(plugin, check=False)
            results[plugin_id] = output or "updated"
        except Exception as exc:
            logger.error("update of %r failed: %s", plugin_id, exc)
            results[plugin_id] = f"failed: {exc}"
    return results


async def upgrade_mailflow() -> str:
    """Upgrade the installed mailflow distributions via uv pip."""
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv executable not found on PATH; cannot update mailflow")
    installed = {name for name in _MAILFLOW_PACKAGES if _distribution_installed(name)}
    if not installed:
        raise RuntimeError("no mailflow distributions found to update")
    command = [uv, "pip", "install", "--upgrade", *sorted(installed)]
    result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"uv pip install --upgrade failed: {(result.stderr or result.stdout).strip()[:500]}"
        )
    return (result.stdout or result.stderr or "").strip()


def _distribution_installed(name: str) -> bool:
    try:
        metadata.distribution(name)
        return True
    except metadata.PackageNotFoundError:
        return False


__all__ = [
    "UpdateReport",
    "apply_plugin_updates",
    "check_plugin_updates",
    "check_updates",
    "installed_plugin_versions",
    "latest_mailflow_release",
    "upgrade_mailflow",
]
