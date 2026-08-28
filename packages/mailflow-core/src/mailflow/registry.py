"""Component registry and plugin registrar.

Ownership is assigned at registration time: every component records the
plugin id that registered it. Runtime code never searches for "the first
plugin with capability X" — component ids are bound to factories explicitly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from mailflow.bot_export import BotExportContext, BotExportResult
from mailflow.config import (
    LLMConfig,
    MailAccountConfig,
    MailFlowConfig,
    NotifierConfig,
    ProcessorConfig,
    StorageConfig,
)
from mailflow.contracts import (
    GatewayProvisioner,
    LLMBackend,
    LLMEnhancer,
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
BotExporterFactory = Callable[[BotExportContext], BotExportResult]
LLMEnhancerFactory = Callable[[ProcessorConfig], LLMEnhancer]
GatewayProvisionerFactory = Callable[[], GatewayProvisioner]

Factory = (
    SourceFactory
    | LLMFactory
    | ProcessorFactory
    | NotifierFactory
    | StorageFactory
    | BotExporterFactory
    | LLMEnhancerFactory
    | GatewayProvisionerFactory
)


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

    def source_factory(self, component_id: str) -> SourceFactory:
        return cast(SourceFactory, self.factory(ComponentKind.MAIL_SOURCE, component_id))

    def llm_factory(self, component_id: str) -> LLMFactory:
        return cast(LLMFactory, self.factory(ComponentKind.LLM_BACKEND, component_id))

    def processor_factory(self, component_id: str) -> ProcessorFactory:
        return cast(ProcessorFactory, self.factory(ComponentKind.MAIL_PROCESSOR, component_id))

    def notifier_factory(self, component_id: str) -> NotifierFactory:
        return cast(NotifierFactory, self.factory(ComponentKind.NOTIFIER, component_id))

    def storage_factory(self, component_id: str) -> StorageFactory:
        return cast(StorageFactory, self.factory(ComponentKind.STORAGE, component_id))

    def llm_enhancer_factory(self, component_id: str) -> LLMEnhancerFactory:
        return cast(LLMEnhancerFactory, self.factory(ComponentKind.LLM_ENHANCER, component_id))

    def bot_exporter_factory(self, component_id: str) -> BotExporterFactory:
        return cast(BotExporterFactory, self.factory(ComponentKind.BOT_EXPORTER, component_id))

    def gateway_provisioner_factory(self, component_id: str) -> GatewayProvisionerFactory:
        return cast(
            GatewayProvisionerFactory,
            self.factory(ComponentKind.GATEWAY_PROVISIONER, component_id),
        )

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

    def add_bot_exporter(self, framework_id: str, factory: BotExporterFactory) -> None:
        """Register a factory that exports a configured MailFlow instance as
        a plugin for the chatbot framework ``framework_id`` (e.g. ``nonebot``)."""
        self._register(ComponentKind.BOT_EXPORTER, framework_id, factory)

    def add_llm_enhancer(self, component_id: str, factory: LLMEnhancerFactory) -> None:
        """Register an LLM enhancer: bounded customization of the built-in
        LLM analysis (system prompt, extra messages, output post-processing)."""
        self._register(ComponentKind.LLM_ENHANCER, component_id, factory)

    def add_gateway_provisioner(
        self, component_id: str, factory: GatewayProvisionerFactory
    ) -> None:
        """Register a gateway provisioner: installs/start/supervises one
        chat-platform bot runtime (e.g. ``napcat``, ``wechaty``). The
        component id is the provider key used by the Bots tab."""
        self._register(ComponentKind.GATEWAY_PROVISIONER, component_id, factory)

    @property
    def config(self) -> MailFlowConfig:
        return self._config


__all__ = [
    "BotExporterFactory",
    "ComponentRegistry",
    "Factory",
    "GatewayProvisionerFactory",
    "LLMEnhancerFactory",
    "LLMFactory",
    "NotifierFactory",
    "PluginRegistrar",
    "ProcessorFactory",
    "SourceFactory",
    "StorageFactory",
]
