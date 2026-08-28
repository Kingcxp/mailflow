"""Chat-platform gateway management: install, start, supervise and stop
gateway processes (NapCat, WeChaty bridges, ...).

The manager owns the *lifecycle* (persisted instance state, process
supervision with backoff, shutdown ordering); the *how* lives in
``GatewayProvisioner`` plugins (component kind GATEWAY_PROVISIONER). Hosts
call the service facade, which delegates here — the TUI never talks to
processes directly.

Instance state is persisted in storage preferences so a restart resumes
gateways that were running (unless the user disabled autostart).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from mailflow.config import MailFlowConfig
from mailflow.contracts import GatewayInstance, GatewayProvisioner
from mailflow.domain import ComponentKind
from mailflow.registry import ComponentRegistry

logger = logging.getLogger("mailflow.gateway")

_PREF_PREFIX = "gateway.instance."
_STATUS_RUNNING = "running"


class GatewayManager:
    """Owns every managed gateway instance for one service."""

    def __init__(
        self,
        config: MailFlowConfig,
        registry: ComponentRegistry,
        storage: Any,
    ) -> None:
        self._config = config
        self._registry = registry
        self._storage = storage
        self._provisioners: dict[str, GatewayProvisioner] = {}
        self._instances: dict[str, GatewayInstance] = {}
        self._supervise_tasks: dict[str, asyncio.Task[Any]] = {}
        self._stop_event = asyncio.Event()

    # -- provisioner resolution -------------------------------------------------

    def provisioner(self, provider: str) -> GatewayProvisioner:
        """The provisioner plugin for ``provider`` (cached instance)."""
        if provider in self._provisioners:
            return self._provisioners[provider]
        if not self._registry.has(ComponentKind.GATEWAY_PROVISIONER, provider):
            raise KeyError(f"no gateway provisioner {provider!r} registered")
        factory = self._registry.gateway_provisioner_factory(provider)
        provisioner = factory()
        self._provisioners[provider] = provisioner
        return provisioner

    def providers(self) -> list[str]:
        return self._registry.component_ids(ComponentKind.GATEWAY_PROVISIONER)

    # -- instance state ---------------------------------------------------------

    def _key(self, provider: str, instance_id: str) -> str:
        return f"{provider}.{instance_id}"

    async def _load_state(self, provider: str, instance_id: str) -> GatewayInstance | None:
        raw = await self._storage.get_preference(f"{_PREF_PREFIX}{provider}.{instance_id}")
        if not raw:
            return None
        try:
            return GatewayInstance.model_validate_json(raw)
        except Exception:
            return None

    async def _save_state(self, instance: GatewayInstance) -> None:
        await self._storage.set_preference(
            f"{_PREF_PREFIX}{instance.provider}.{instance.instance_id}",
            instance.model_dump_json(),
        )

    async def instances(self) -> list[GatewayInstance]:
        """Every known instance, newest first."""
        return sorted(self._instances.values(), key=lambda i: i.instance_id, reverse=True)

    def instance(self, provider: str, instance_id: str) -> GatewayInstance | None:
        return self._instances.get(self._key(provider, instance_id))

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        """Restore instances that were running at shutdown (autostart)."""
        self._stop_event = asyncio.Event()
        # scan preferences for known instances
        for instance in await self._list_persisted():
            self._instances[self._key(instance.provider, instance.instance_id)] = instance
            if instance.status == _STATUS_RUNNING and instance.extra.get("autostart", True):
                self._supervise_tasks[instance.instance_id] = asyncio.create_task(
                    self._supervise(instance), name=f"gateway-{instance.instance_id}"
                )

    async def _list_persisted(self) -> list[GatewayInstance]:
        found: list[GatewayInstance] = []
        # preferences are keyed `gateway.instance.<provider>.<instance>` and
        # provisioners record their instance ids in a list pref
        for provider in self.providers():
            raw = await self._storage.get_preference(f"{_PREF_PREFIX}{provider}.ids")
            if not raw:
                continue
            for instance_id in raw.split(","):
                instance_id = instance_id.strip()
                if not instance_id:
                    continue
                instance = await self._load_state(provider, instance_id)
                if instance is not None:
                    found.append(instance)
        return found

    async def stop(self) -> None:
        """Terminate every supervised gateway and cancel supervisors."""
        self._stop_event.set()
        tasks = list(self._supervise_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._supervise_tasks.clear()
        for instance in list(self._instances.values()):
            if instance.status == _STATUS_RUNNING:
                try:
                    await self.provisioner(instance.provider).stop(instance.instance_id)
                except Exception as exc:
                    logger.warning(
                        "gateway %s.%s stop failed: %s",
                        instance.provider,
                        instance.instance_id,
                        exc,
                    )
                instance.status = "stopped"
                await self._save_state(instance)

    async def _supervise(self, instance: GatewayInstance) -> None:
        """Restart a crashed gateway with bounded backoff."""
        backoff = 5
        while not self._stop_event.is_set():
            try:
                current = await self.provisioner(instance.provider).status(instance.instance_id)
            except Exception as exc:
                current = instance.model_copy(update={"error": str(exc)})
            if current.status == _STATUS_RUNNING:
                self._instances[self._key(instance.provider, instance.instance_id)] = current
                await self._save_state(current)
                backoff = 5
                # poll every 30s while healthy
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=30)
                except TimeoutError:
                    continue
                return
            if current.status == "stopped":
                # user stopped it: stop supervising
                return
            # error/unknown: restart after backoff
            logger.warning(
                "gateway %s.%s not running (%s); restarting in %ds",
                instance.provider,
                instance.instance_id,
                current.error or current.status,
                backoff,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            backoff = min(backoff * 2, 60)
            try:
                started = await self.provisioner(instance.provider).start(
                    instance.instance_id, instance.extra.get("options", {})
                )
                self._instances[self._key(started.provider, started.instance_id)] = started
                await self._save_state(started)
                backoff = 5
            except Exception as exc:
                logger.error(
                    "gateway %s.%s restart failed: %s", instance.provider, instance.instance_id, exc
                )

    # -- provisioning API (used by the Bots tab guide) ---------------------------

    async def detect(self, provider: str) -> str:
        """Status line for the guide's first step."""
        try:
            return await self.provisioner(provider).detect()
        except Exception as exc:
            return f"error: {exc}"

    async def provision(
        self,
        provider: str,
        instance_id: str,
        options: dict[str, Any],
        *,
        autostart: bool = True,
    ) -> GatewayInstance:
        """Install (if needed), start and supervise one instance.

        Returns the running instance. The caller (TUI guide) then reads the
        QR via :meth:`qr` and saves the notifier config.
        """
        provisioner = self.provisioner(provider)
        key = self._key(provider, instance_id)
        instance = self._instances.get(key) or GatewayInstance(
            provider=provider,
            instance_id=instance_id,
            extra={"options": options, "autostart": autostart},
        )
        self._instances[key] = instance
        instance.status = "installing"
        instance.error = ""
        await self._save_state(instance)
        try:
            await provisioner.install(instance_id, options)
        except Exception as exc:
            instance.status = "error"
            instance.error = f"install failed: {exc}"
            await self._save_state(instance)
            raise RuntimeError(instance.error) from exc
        instance.status = "starting"
        await self._save_state(instance)
        try:
            running = await provisioner.start(instance_id, options)
        except Exception as exc:
            instance.status = "error"
            instance.error = f"start failed: {exc}"
            await self._save_state(instance)
            raise RuntimeError(instance.error) from exc
        self._instances[key] = running
        await self._save_state(running)
        # record the instance id in the provider's list for restart scanning
        raw = await self._storage.get_preference(f"{_PREF_PREFIX}{provider}.ids") or ""
        ids = [i for i in raw.split(",") if i.strip()] if raw else []
        if instance_id not in ids:
            ids.append(instance_id)
            await self._storage.set_preference(f"{_PREF_PREFIX}{provider}.ids", ",".join(ids))
        # supervise while running
        self._supervise_tasks[instance_id] = asyncio.create_task(
            self._supervise(running), name=f"gateway-{instance_id}"
        )
        return running

    async def qr(self, provider: str, instance_id: str) -> str:
        """QR payload for the login step ('' when not supported)."""
        try:
            return await self.provisioner(provider).qr(instance_id)
        except Exception:
            return ""

    async def shutdown_instance(self, provider: str, instance_id: str) -> None:
        """Stop one instance and stop supervising it."""
        key = self._key(provider, instance_id)
        task = self._supervise_tasks.pop(instance_id, None)
        if task is not None:
            task.cancel()
            # a cancelled supervisor raises CancelledError on await: swallow
            # it like any other shutdown noise
            await asyncio.gather(task, return_exceptions=True)
        instance = self._instances.get(key)
        if instance is None:
            return
        try:
            await self.provisioner(provider).stop(instance_id)
        except Exception as exc:
            logger.warning("gateway %s stop failed: %s", key, exc)
        instance.status = "stopped"
        await self._save_state(instance)


__all__ = ["GatewayManager"]
