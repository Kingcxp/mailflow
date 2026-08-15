"""Component registry and plugin registrar.

Ownership is assigned at registration time: every component records the
plugin id that registered it. Runtime code never searches for "the first
plugin with capability X" — component ids are bound to factories explicitly.
"""

from __future__ import annotations

from collections.abc import Callable

from mailflow.config import (
    LLMConfig,
    MailAccountConfig,
    MailFlowConfig,
    NotifierConfig,
    ProcessorConfig,
    StorageConfig,
)
from mailflow.contracts import (
    LLMBackend,
    LLMRouter,
    MailProcessor,
    MailSource,
    Notifier,
    StorageBackend,
)
from mailflow.domain import ComponentKind, ComponentSnapshot

SourceFactory = Callable[[MailAccountConfig], MailSource]
LLMFactory = Callable[[LLMConfig], LLMBackend]
ProcessorFactory = Callable[[ProcessorConfig, LLMRouter], MailProcessor]
NotifierFactory = Callable[[NotifierConfig], Notifier]
StorageFactory = Callable[[StorageConfig], StorageBackend]

Factory = SourceFactory | LLMFactory | ProcessorFactory | NotifierFactory | StorageFactory


class ComponentRegistry:
    """Holds typed factories plus the ownership snapshot for each component."""

    def __init__(self) -> None:
        self._snapshots: dict[str, ComponentSnapshot] = {}
        self._factories: dict[ComponentKind, dict[str, Factory]] = {
            kind: {} for kind in ComponentKind
        }

    def register(
        self, kind: ComponentKind, component_id: str, plugin_id: str, factory: Factory
    ) -> None:
        if component_id in self._snapshots:
            raise ValueError(
                f"component {component_id!r} already registered by plugin "
                f"{self._snapshots[component_id].plugin_id!r}"
            )
        self._snapshots[component_id] = ComponentSnapshot(
            component_id=component_id, kind=kind, plugin_id=plugin_id
        )
        self._factories[kind][component_id] = factory

    def factory(self, kind: ComponentKind, component_id: str) -> Factory:
        try:
            return self._factories[kind][component_id]
        except KeyError as exc:
            raise KeyError(f"no {kind.value} component {component_id!r}") from exc

    def plugin_for(self, component_id: str) -> str | None:
        snapshot = self._snapshots.get(component_id)
        return snapshot.plugin_id if snapshot is not None else None

    def has(self, kind: ComponentKind, component_id: str) -> bool:
        return component_id in self._factories[kind]

    def component_ids(self, kind: ComponentKind) -> list[str]:
        return sorted(self._factories[kind])

    def snapshots(self) -> list[ComponentSnapshot]:
        return sorted(self._snapshots.values(), key=lambda s: (s.kind.value, s.component_id))

    def __repr__(self) -> str:
        counts = {kind.value: len(ids) for kind, ids in self._factories.items()}
        return f"ComponentRegistry({counts})"


class PluginRegistrar:
    """Registers components on behalf of exactly one plugin."""

    def __init__(self, registry: ComponentRegistry, config: MailFlowConfig, plugin_id: str) -> None:
        self._registry = registry
        self._config = config
        self.plugin_id = plugin_id

    # -- internal ------------------------------------------------------------

    def _register(self, kind: ComponentKind, component_id: str, factory: Factory) -> None:
        self._registry.register(kind, component_id, self.plugin_id, factory)

    # -- public factories ------------------------------------------------------

    def add_source(self, component_id: str, factory: SourceFactory) -> None:
        self._register(ComponentKind.MAIL_SOURCE, component_id, factory)

    def add_llm(self, component_id: str, factory: LLMFactory) -> None:
        self._register(ComponentKind.LLM_BACKEND, component_id, factory)

    def add_processor(self, component_id: str, factory: ProcessorFactory) -> None:
        self._register(ComponentKind.MAIL_PROCESSOR, component_id, factory)

    def add_notifier(self, component_id: str, factory: NotifierFactory) -> None:
        self._register(ComponentKind.NOTIFIER, component_id, factory)

    def add_storage(self, component_id: str, factory: StorageFactory) -> None:
        self._register(ComponentKind.STORAGE, component_id, factory)

    @property
    def config(self) -> MailFlowConfig:
        return self._config


__all__ = [
    "ComponentRegistry",
    "Factory",
    "LLMFactory",
    "NotifierFactory",
    "PluginRegistrar",
    "ProcessorFactory",
    "SourceFactory",
    "StorageFactory",
]
