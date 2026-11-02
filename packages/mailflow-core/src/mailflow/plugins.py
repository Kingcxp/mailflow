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
import re
from collections.abc import Callable
from importlib import metadata
from typing import Any

import pluggy

from mailflow.config import MailFlowConfig
from mailflow.domain import ComponentKind, ComponentSnapshot, PluginSnapshot
from mailflow.errors import error_code
from mailflow.plugin_market import safe_failure_reason
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
        kinds: list[ComponentKind] | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.name = name or plugin_id
        self.version = version
        self.description = description
        self.kinds = list(kinds or [])

    def to_snapshot(self, components: list[str]) -> PluginSnapshot:
        return PluginSnapshot(
            plugin_id=self.plugin_id,
            name=self.name,
            version=self.version,
            description=self.description,
            kinds=self.kinds,
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
        self._bundled_plugin_ids: set[str] = set()
        self._failures: dict[str, str] = {}
        self._failure_details: dict[str, tuple[str, dict[str, Any]]] = {}
        self._loaded_ids: set[str] = set()
        self._loaders: dict[str, tuple[Callable[[], Any], bool]] = {}

    @property
    def failures(self) -> dict[str, str]:
        return dict(self._failures)

    def _failed(self, plugin_id: str, stage: str, exc: Exception) -> None:
        reason = f"{stage}: {safe_failure_reason(exc)}"
        self._failures[plugin_id] = reason
        if isinstance(exc, ModuleNotFoundError):
            dependency = exc.name or str(exc)
            if re.fullmatch(r"[\w.-]+", dependency):
                self._failure_details[plugin_id] = (
                    "errors.plugin_dependency",
                    {"dependency": dependency},
                )
            else:
                self._failure_details[plugin_id] = ("errors.plugin_contract", {})
        elif isinstance(exc, (TypeError, ValueError)):
            self._failure_details[plugin_id] = ("errors.plugin_contract", {})
        else:
            self._failure_details[plugin_id] = (error_code(exc), {})
        self._infos.setdefault(plugin_id, PluginInfo(plugin_id))
        logger.error("plugin %r failed: %s", plugin_id, reason)

    def failure_detail(self, plugin_id: str, translate: Callable[..., str]) -> dict[str, Any]:
        code, params = self._failure_details.get(plugin_id, ("errors.plugin_contract", {}))
        return {"code": code, "params": dict(params), "message": translate(code, **params)}

    def load(
        self, plugin_id: str, loader: Callable[[], Any], *, bundled: bool = False
    ) -> str | None:
        """Load one declared plugin, isolating missing dependencies and bad hooks."""
        if bundled or plugin_id not in self._loaders:
            self._loaders[plugin_id] = (loader, bundled)
        if bundled:
            self._bundled_plugin_ids.add(plugin_id)
        if plugin_id in self._plugin_objects:
            return None
        self._infos.setdefault(plugin_id, PluginInfo(plugin_id))
        if not self._is_enabled(plugin_id):
            return None
        try:
            plugin = loader()
        except Exception as exc:
            self._failed(plugin_id, "import", exc)
            return None
        registered = self.register(plugin, bundled=bundled, origin=plugin_id)
        if registered != plugin_id and plugin_id not in self._failures:
            # Older distributions used component aliases as entry-point names.
            self._infos.pop(plugin_id, None)
            self._loaders.pop(plugin_id, None)
        return registered

    # -- registration ----------------------------------------------------------

    def register(
        self, plugin: Any, *, bundled: bool = False, origin: str | None = None
    ) -> str | None:
        """Register one plugin object (module or instance); returns its id."""
        try:
            info = plugin.mailflow_plugin_info()
            if not isinstance(info, PluginInfo) or not info.plugin_id:
                raise TypeError("mailflow_plugin_info must return PluginInfo with a plugin_id")
            if not callable(getattr(plugin, "mailflow_register", None)):
                raise TypeError("missing mailflow_register hook")
        except Exception as exc:
            self._failed(origin or type(plugin).__name__, "metadata", exc)
            return None
        plugin_id = str(info.plugin_id)
        if plugin_id in self._plugin_objects:
            if plugin is not self._plugin_objects[plugin_id]:
                logger.warning(
                    "duplicate plugin %r ignored; keeping the first implementation", plugin_id
                )
            return None
        try:
            self._pm.register(plugin, name=plugin_id)
        except Exception as exc:
            self._failed(plugin_id, "registration", exc)
            return None
        self._plugin_objects[plugin_id] = plugin
        self._infos[plugin_id] = info
        if bundled:
            self._bundled_plugin_ids.add(plugin_id)
        self._failures.pop(plugin_id, None)
        logger.debug("registered plugin %r (%s)", plugin_id, info.name)
        return plugin_id

    # -- discovery --------------------------------------------------------------

    def discover(self, config: MailFlowConfig | None = None) -> list[str]:
        """Load entry points in the ``mailflow.plugins`` group; returns ids."""
        if config is not None:
            self._config = config
        discovered: list[str] = []
        for entry_point in metadata.entry_points().select(group=ENTRY_POINT_GROUP):
            name = entry_point.name
            # Older bundled wheels used a component alias as the entry-point
            # name. Recognize their distribution before importing it.
            distribution = entry_point.dist
            package = distribution.name if distribution is not None else ""
            if package in (
                self._bundled_plugin_ids
                | set(self._config.plugins.enabled)
                | set(self._config.plugins.disabled)
            ):
                name = package
            plugin_id = self.load(name, entry_point.load)
            if plugin_id is not None:
                discovered.append(plugin_id)
        return discovered

    def unregister(self, plugin_id: str) -> None:
        """Forget an uninstalled external plugin before the next registry build."""
        plugin = self._plugin_objects.pop(plugin_id, None)
        if plugin is not None:
            self._pm.unregister(plugin)
        self._infos.pop(plugin_id, None)
        self._failures.pop(plugin_id, None)
        self._loaded_ids.discard(plugin_id)
        self._loaders.pop(plugin_id, None)

    # -- filtering ----------------------------------------------------------------

    def _is_enabled(self, plugin_id: str) -> bool:
        filter_config = self._config.plugins
        if plugin_id in filter_config.disabled:
            return False
        if filter_config.enabled:
            return plugin_id in filter_config.enabled
        return True

    def enabled_infos(self) -> list[PluginInfo]:
        return [
            info
            for pid, info in self._infos.items()
            if pid in self._plugin_objects and self._is_enabled(pid) and pid not in self._failures
        ]

    # -- registry construction ---------------------------------------------------

    def build_registry(self, config: MailFlowConfig | None = None) -> ComponentRegistry:
        """Build a fresh registry; registration is deterministic per startup."""
        if config is not None:
            self._config = config
        for plugin_id, (loader, bundled) in list(self._loaders.items()):
            if plugin_id not in self._plugin_objects and self._is_enabled(plugin_id):
                self.load(plugin_id, loader, bundled=bundled)
        registry = ComponentRegistry()
        self._loaded_ids.clear()
        for plugin_id, plugin in self._plugin_objects.items():
            if not self._is_enabled(plugin_id):
                logger.info("plugin %r disabled by config; skipping", plugin_id)
                continue
            staged = ComponentRegistry()
            registrar = PluginRegistrar(staged, self._config, plugin_id)
            try:
                plugin.mailflow_register(registrar, self._config)
                actual = {component.kind for component in staged.snapshots()}
                declared = set(self._infos[plugin_id].kinds)
                if not actual or actual != declared:
                    raise ValueError(
                        f"declared kinds {sorted(declared)} do not match registered kinds {sorted(actual)}"
                    )
                registry.merge(staged)
            except Exception as exc:
                self._failed(plugin_id, "registration", exc)
            else:
                self._failures.pop(plugin_id, None)
                self._loaded_ids.add(plugin_id)
        return registry

    def component_ids_for(self, plugin_id: str) -> list[str]:
        """Return component ids declared by a plugin, even when disabled."""
        return [snapshot.component_id for snapshot in self.components_for(plugin_id)]

    def components_for(self, plugin_id: str) -> list[ComponentSnapshot]:
        """Return owned components, including their kind, even when disabled."""
        plugin = self._plugin_objects.get(plugin_id)
        if plugin is None:
            pending = self._loaders.get(plugin_id)
            if pending is None:
                return []
            # Explicit ownership lookup (for uninstall) may inspect a disabled
            # package. Normal discovery still never imports disabled plugins.
            try:
                plugin = pending[0]()
            except Exception as exc:
                logger.error("plugin %r ownership lookup failed: %s", plugin_id, exc)
                return []
        config = self._config.model_copy(deep=True)
        config.plugins.disabled = [item for item in config.plugins.disabled if item != plugin_id]
        if config.plugins.enabled and plugin_id not in config.plugins.enabled:
            config.plugins.enabled.append(plugin_id)
        registry = ComponentRegistry()
        registrar = PluginRegistrar(registry, config, plugin_id)
        try:
            plugin.mailflow_register(registrar, config)
        except Exception as exc:
            logger.error("plugin %r ownership lookup failed: %s", plugin_id, exc)
            return []
        return registry.snapshots()

    # -- snapshots ------------------------------------------------------------------

    def snapshots(self, registry: ComponentRegistry) -> list[PluginSnapshot]:
        snapshots: list[PluginSnapshot] = []
        for info in self._infos.values():
            snapshot = info.to_snapshot(
                [c.component_id for c in registry.snapshots() if c.plugin_id == info.plugin_id]
            )
            snapshot.status = self.status(info.plugin_id)
            snapshot.error = self._failures.get(info.plugin_id, "")
            snapshots.append(snapshot)
        return snapshots

    def status(self, plugin_id: str) -> str:
        if not self._is_enabled(plugin_id):
            return "disabled"
        if plugin_id in self._failures:
            return "error"
        return "enabled" if plugin_id in self._loaded_ids else "not_loaded"

    @property
    def plugin_ids(self) -> list[str]:
        return list(self._plugin_objects)

    @property
    def known_plugin_ids(self) -> set[str]:
        return set(self._infos)

    @property
    def bundled_plugin_ids(self) -> set[str]:
        """Plugin ids supplied by the application distribution."""
        return set(self._bundled_plugin_ids)

    def kind_label(self, kind: ComponentKind) -> str:
        return kind.value


def make_manager(
    config: MailFlowConfig | None = None, discover_plugins: bool = True
) -> PluginManager:
    manager = PluginManager(config)
    if discover_plugins:
        manager.discover()
    return manager
