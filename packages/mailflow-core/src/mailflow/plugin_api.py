"""Declarative plugin authoring: decorators build the two pluggy hooks.

The classic plugin style implements ``mailflow_plugin_info`` and
``mailflow_register`` by hand; this API produces the same hooks from a
class-based declaration, so a plugin module shrinks to a definition plus
one decorated class per component:

```python
from mailflow.plugin_api import define_plugin

PLUGIN = define_plugin(
    "mailflow-notify-demo",
    name="Demo Notifier",
    version="0.1.0",
    description="One-line summary",
)

@PLUGIN.notifier("demo")
class DemoNotifier:
    async def notify(self, record) -> None:
        ...

plugin = PLUGIN.build()
```

Decorated classes are the factories themselves (they are instantiated with
the matching config section, e.g. ``NotifierConfig``); the component kinds
are collected from the registrations, so ``PluginInfo.kinds`` stays in sync
automatically.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo

Factory = Callable[..., Any]


@dataclass
class _Registration:
    kind: ComponentKind
    component_id: str
    factory: Factory


class PluginBuilder:
    """Collects registrations and builds a plugin object with the two hooks."""

    def __init__(
        self,
        plugin_id: str,
        *,
        name: str = "",
        version: str = "",
        description: str = "",
    ) -> None:
        self._plugin_id = plugin_id
        self._name = name or plugin_id
        self._version = version
        self._description = description
        self._registrations: list[_Registration] = []

    def _register(
        self, kind: ComponentKind, component_id: str | None = None
    ) -> Callable[[Factory], Factory]:
        def decorate(factory: Factory) -> Factory:
            resolved = (
                component_id
                or getattr(factory, "component_id", None)
                or getattr(factory, "backend_id", None)
                or getattr(factory, "processor_id", None)
                or factory.__name__
            )
            self._registrations.append(
                _Registration(kind=kind, component_id=str(resolved), factory=factory)
            )
            return factory

        return decorate

    def source(self, component_id: str | None = None) -> Callable[[Factory], Factory]:
        return self._register(ComponentKind.MAIL_SOURCE, component_id)

    def processor(self, component_id: str | None = None) -> Callable[[Factory], Factory]:
        return self._register(ComponentKind.MAIL_PROCESSOR, component_id)

    def llm(self, component_id: str | None = None) -> Callable[[Factory], Factory]:
        return self._register(ComponentKind.LLM_BACKEND, component_id)

    def notifier(self, component_id: str | None = None) -> Callable[[Factory], Factory]:
        return self._register(ComponentKind.NOTIFIER, component_id)

    def storage(self, component_id: str | None = None) -> Callable[[Factory], Factory]:
        return self._register(ComponentKind.STORAGE, component_id)

    def bot_exporter(self, component_id: str | None = None) -> Callable[[Factory], Factory]:
        return self._register(ComponentKind.BOT_EXPORTER, component_id)

    def llm_enhancer(self, component_id: str | None = None) -> Callable[[Factory], Factory]:
        return self._register(ComponentKind.LLM_ENHANCER, component_id)

    def build(self) -> Any:
        """Return a plugin object exposing the two pluggy hooks."""
        plugin_id = self._plugin_id
        name = self._name
        version = self._version
        description = self._description
        registrations = list(self._registrations)
        info = PluginInfo(
            plugin_id=plugin_id,
            name=name,
            version=version,
            description=description,
            kinds=[registration.kind for registration in registrations],
        )

        class _DeclaredPlugin:
            def mailflow_plugin_info(self) -> PluginInfo:
                return info

            def mailflow_register(self, registrar: Any, config: Any) -> None:
                for registration in registrations:
                    add = {
                        ComponentKind.MAIL_SOURCE: registrar.add_source,
                        ComponentKind.MAIL_PROCESSOR: registrar.add_processor,
                        ComponentKind.LLM_BACKEND: registrar.add_llm,
                        ComponentKind.NOTIFIER: registrar.add_notifier,
                        ComponentKind.STORAGE: registrar.add_storage,
                        ComponentKind.BOT_EXPORTER: registrar.add_bot_exporter,
                        ComponentKind.LLM_ENHANCER: registrar.add_llm_enhancer,
                    }[registration.kind]
                    add(registration.component_id, registration.factory)

        return _DeclaredPlugin()


def define_plugin(
    plugin_id: str,
    *,
    name: str = "",
    version: str = "",
    description: str = "",
) -> PluginBuilder:
    """Start a declarative plugin definition."""
    return PluginBuilder(plugin_id, name=name, version=version, description=description)


__all__ = ["PluginBuilder", "define_plugin"]
