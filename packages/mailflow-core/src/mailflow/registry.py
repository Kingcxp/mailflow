"""Component registry and plugin registrar.

Ownership is assigned at registration time: every component records the
plugin id that registered it. Runtime code never searches for "the first
plugin with capability X" — component ids are bound to factories explicitly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

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
from mailflow.forms import FormField

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


# Probe: async (options, t) -> status string, registered by plugins for the
# "Test connection" button and the Notifications tab live-status column.
ProbeFn = Callable[[dict[str, Any], Any], Awaitable[str]]


class ComponentRegistry:
    """Holds typed factories plus the ownership snapshot for each component."""

    def __init__(self) -> None:
        self._snapshots: dict[tuple[ComponentKind, str], ComponentSnapshot] = {}
        self._factories: dict[ComponentKind, dict[str, Factory]] = {
            kind: {} for kind in ComponentKind
        }
        self._form_fields: dict[tuple[ComponentKind, str], tuple[FormField, ...]] = {}
        self._probes: dict[tuple[ComponentKind, str], ProbeFn] = {}

    def register(
        self, kind: ComponentKind, component_id: str, plugin_id: str, factory: Factory
    ) -> None:
        # the key is (kind, component_id): one id may legitimately appear in
        # several kinds (e.g. 'wechaty' is both a NOTIFIER and a
        # GATEWAY_PROVISIONER), and a conflict is only a conflict within the
        # same kind
        key = (kind, component_id)
        if key in self._snapshots:
            raise ValueError(
                f"component {component_id!r} already registered by plugin "
                f"{self._snapshots[key].plugin_id!r}"
            )
        self._snapshots[key] = ComponentSnapshot(
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
        for snapshot in self._snapshots.values():
            if snapshot.component_id == component_id:
                return snapshot.plugin_id
        return None

    def has(self, kind: ComponentKind, component_id: str) -> bool:
        return component_id in self._factories[kind]

    def component_ids(self, kind: ComponentKind) -> list[str]:
        return sorted(self._factories[kind])

    def form_fields(self, kind: ComponentKind, component_id: str) -> tuple[FormField, ...]:
        """Form fields a plugin declared for one component ('' when none)."""
        return self._form_fields.get((kind, component_id), ())

    def set_form_fields(
        self, kind: ComponentKind, component_id: str, fields: tuple[FormField, ...]
    ) -> None:
        """Record the ordered form fields a plugin declared for one component."""
        self._form_fields[(kind, component_id)] = fields

    def probe(self, kind: ComponentKind, component_id: str) -> ProbeFn | None:
        """A plugin-registered connection probe for one component (None when none)."""
        return self._probes.get((kind, component_id))

    def set_probe(self, kind: ComponentKind, component_id: str, probe: ProbeFn) -> None:
        """Record a plugin-registered connection probe for one component."""
        self._probes[(kind, component_id)] = probe

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

    def add_form_fields(
        self,
        kind: ComponentKind,
        component_id: str,
        fields: tuple[FormField, ...],
    ) -> None:
        """Declare the ordered form fields a component's login/option form
        needs. The TUI renders them generically (text/password/number/
        boolean/list/select/textarea); the core stores them as pure data."""
        if not fields:
            return
        self._registry.set_form_fields(kind, component_id, fields)

    def add_probe(
        self,
        kind: ComponentKind,
        component_id: str,
        probe: ProbeFn,
    ) -> None:
        """Register a connection probe for one component; used by the
        'Test' button and the Notifications tab's live status column."""
        self._registry.set_probe(kind, component_id, probe)

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
