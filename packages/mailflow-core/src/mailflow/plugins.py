"""Pluggy-based plugin discovery and the plugin manager.

Pluggy is used for *discovery and registration only* — processor execution
ordering, retries and failure policies are owned by the pipeline (see
``mailflow.pipeline``). A plugin exposes two hooks:

- ``mailflow_plugin_info()`` → ``PluginInfo`` (id, name, version, description)
- ``mailflow_register(registrar, config)`` → registers factories

Component ownership (plugin id) is stamped at registration time by the
``PluginRegistrar``.
"""

from __future__ import annotations

import logging
from importlib import metadata
from typing import Any

import pluggy

from mailflow.config import MailFlowConfig
from mailflow.domain import ComponentKind, PluginSnapshot
from mailflow.registry import ComponentRegistry, PluginRegistrar

logger = logging.getLogger("mailflow.plugins")

HOOKSPEC_GROUP = "mailflow"
ENTRY_POINT_GROUP = "mailflow.plugins"

hookspec = pluggy.HookspecMarker(HOOKSPEC_GROUP)


class PluginInfo:
    """Static description returned by ``mailflow_plugin_info``."""

    def __init__(
        self,
        plugin_id: str,
        name: str = "",
        version: str = "",
        description: str = "",
    ) -> None:
        self.plugin_id = plugin_id
        self.name = name or plugin_id
        self.version = version
        self.description = description

    def to_snapshot(self, components: list[str]) -> PluginSnapshot:
        return PluginSnapshot(
            plugin_id=self.plugin_id,
            name=self.name,
            version=self.version,
            description=self.description,
            components=components,
        )


class MailFlowHookSpecs:
    """Hook specifications implemented by every MailFlow plugin."""

    @hookspec
    def mailflow_plugin_info(self) -> PluginInfo:
        """Return the static plugin description."""
        raise NotImplementedError

    @hookspec
    def mailflow_register(self, registrar: PluginRegistrar, config: MailFlowConfig) -> None:
        """Register the plugin's component factories with ``registrar``."""
        raise NotImplementedError


class PluginManager:
    """Discovers and registers plugins; builds the component registry."""

    def __init__(self, config: MailFlowConfig | None = None) -> None:
        self._config = config or MailFlowConfig()
        self._pm = pluggy.PluginManager(HOOKSPEC_GROUP)
        self._pm.add_hookspecs(MailFlowHookSpecs)
        self._infos: dict[str, PluginInfo] = {}
        self._plugin_objects: dict[str, Any] = {}

    # -- registration ----------------------------------------------------------

    def register(self, plugin: Any) -> str | None:
        """Register one plugin object (module or instance); returns its id."""
        try:
            info = plugin.mailflow_plugin_info()
        except AttributeError:
            logger.error("plugin %r lacks mailflow_plugin_info hook", plugin)
            return None
        plugin_id = str(info.plugin_id)
        if plugin_id in self._plugin_objects:
            logger.debug("plugin %r already registered", plugin_id)
            return None
        self._plugin_objects[plugin_id] = plugin
        self._infos[plugin_id] = info
        self._pm.register(plugin, name=plugin_id)
        logger.debug("registered plugin %r (%s)", plugin_id, info.name)
        return plugin_id

    # -- discovery --------------------------------------------------------------

    def discover(self) -> list[str]:
        """Load entry points in the ``mailflow.plugins`` group; returns ids."""
        discovered: list[str] = []
        for entry_point in metadata.entry_points().select(group=ENTRY_POINT_GROUP):
            try:
                plugin = entry_point.load()
            except Exception as exc:
                logger.error("failed to load entry point %r: %s", entry_point.name, exc)
                continue
            plugin_id = self.register(plugin)
            if plugin_id is not None:
                discovered.append(plugin_id)
        return discovered

    # -- filtering ----------------------------------------------------------------

    def _is_enabled(self, plugin_id: str) -> bool:
        filter_config = self._config.plugins
        if plugin_id in filter_config.disabled:
            return False
        if filter_config.enabled:
            return plugin_id in filter_config.enabled
        return True

    def enabled_infos(self) -> list[PluginInfo]:
        return [info for pid, info in self._infos.items() if self._is_enabled(pid)]

    # -- registry construction ---------------------------------------------------

    def build_registry(self) -> ComponentRegistry:
        """Build a fresh registry; registration is deterministic per startup."""
        registry = ComponentRegistry()
        for plugin_id, plugin in self._plugin_objects.items():
            if not self._is_enabled(plugin_id):
                logger.info("plugin %r disabled by config; skipping", plugin_id)
                continue
            registrar = PluginRegistrar(registry, self._config, plugin_id)
            try:
                plugin.mailflow_register(registrar, self._config)
            except Exception as exc:
                logger.error("plugin %r failed to register: %s", plugin_id, exc)
        return registry

    # -- snapshots ------------------------------------------------------------------

    def snapshots(self, registry: ComponentRegistry) -> list[PluginSnapshot]:
        return [
            info.to_snapshot(
                [c.component_id for c in registry.snapshots() if c.plugin_id == info.plugin_id]
            )
            for info in self.enabled_infos()
        ]

    @property
    def plugin_ids(self) -> list[str]:
        return list(self._plugin_objects)

    def kind_label(self, kind: ComponentKind) -> str:
        return kind.value


def make_manager(
    config: MailFlowConfig | None = None, discover_plugins: bool = True
) -> PluginManager:
    manager = PluginManager(config)
    if discover_plugins:
        manager.discover()
    return manager
