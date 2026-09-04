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
from dataclasses import dataclass, field
from typing import Any

from mailflow.config import MailFlowConfig
from mailflow.contracts import GatewayInstance, GatewayProvisioner
from mailflow.domain import ComponentKind
from mailflow.registry import ComponentRegistry

logger = logging.getLogger("mailflow.gateway")

_PREF_PREFIX = "gateway.instance."


class GatewayNotInstalledError(RuntimeError):
    """The gateway's payload is missing (e.g. its data directory was
    deleted while the app was stopped). Not transient: supervision must
    mark the instance and stop retrying instead of looping forever."""


_STATUS_RUNNING = "running"


@dataclass
class InstallProgress:
    """Shared install progress: written by a provisioner's download loop
    (any thread), polled by the TUI guide to render a progress bar."""

    stage: str = "pending"
    message: str = ""
    percent: float = 0.0  # 0..100
    _done: bool = field(default=False, repr=False)

    def update(self, percent: float, message: str | None = None, stage: str | None = None) -> None:
        self.percent = min(max(percent, 0.0), 100.0)
        if message is not None:
            self.message = message
        if stage is not None:
            self.stage = stage

    def finish(self, message: str = "") -> None:
        self.percent = 100.0
        if message:
            self.message = message
        self._done = True


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
        # gates concurrent gateway launches (each NapCat is a full QQ
        # Electron app, ~1.5-2 GB RAM); shared by the startup resumes and
        # the guided-provision start so the host never OOMs
        self._start_limiter = asyncio.Semaphore(2)

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
        """Restore instances that were running at shutdown (autostart).

        The actual gateway launches are serialized by
        ``self._start_limiter`` inside :meth:`_supervise`, so many
        instances at boot never launch more than two QQ/NapCat Electron
        apps at once (~1.5-2 GB each)."""
        self._stop_event = asyncio.Event()
        # scan preferences for known instances
        for instance in await self._list_persisted():
            key = self._key(instance.provider, instance.instance_id)
            self._instances[key] = instance
            # resume RUNNING instances, but ALSO instances left in `error`
            # (e.g. the deploy failed on missing system libraries): once the
            # operator fixed the environment and restarted, the supervisor
            # must retry the deploy — otherwise the instance is stuck in
            # error forever and the only way out is deleting the notifier.
            if instance.status in (_STATUS_RUNNING, "error") and instance.extra.get(
                "autostart", True
            ):
                self._supervise_tasks[instance.instance_id] = asyncio.create_task(
                    self._supervise(instance, resume=True),
                    name=f"gateway-{instance.instance_id}",
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

    async def _ensure_side_channels(self, instance: GatewayInstance) -> None:
        """Give the provisioner a chance to recreate in-process bridges.

        Some gateways route chat events through a listener that lives in
        the MailFlow process (the onebot httpClients bridge). After an app
        restart the gateway child may still be running — so ``status``
        reports RUNNING and ``start()`` is never called again — while the
        in-process listener is gone. Provisioners that implement an
        ``ensure_bridge(instance_id, options)`` hook get called here to
        recreate it idempotently; others get nothing.
        """
        ensure = getattr(self.provisioner(instance.provider), "ensure_bridge", None)
        if ensure is None:
            return
        try:
            await ensure(instance.instance_id, instance.extra.get("options", {}))
        except Exception as exc:
            logger.warning(
                "gateway %s.%s bridge ensure failed: %s",
                instance.provider,
                instance.instance_id,
                exc,
            )

    async def _supervise(self, instance: GatewayInstance, *, resume: bool = False) -> None:
        """Restart a crashed gateway with bounded backoff.

        ``resume`` is True for instances restored at startup: the
        process is not running yet, so a 'stopped' status must trigger a
        start (a plain stopped status would be treated as user-stopped
        and never restarted)."""
        backoff = 5
        first = True
        while not self._stop_event.is_set():
            try:
                current = await self.provisioner(instance.provider).status(instance.instance_id)
            except Exception as exc:
                current = instance.model_copy(update={"error": str(exc)})
            if current.status == _STATUS_RUNNING:
                # status() returns a bare instance without extra; storing
                # it as-is would drop extra.options (bot_url) and
                # extra.autostart from the in-memory state — the bridge
                # then never recreates after an app restart (phantom
                # running, ECONNREFUSED on the bridge port)
                current = current.model_copy(
                    update={
                        "extra": {**instance.extra, **current.extra}
                        if current.extra
                        else instance.extra,
                    }
                )
                self._instances[self._key(instance.provider, instance.instance_id)] = current
                await self._save_state(current)
                backoff = 5
                # the gateway process is alive, but in-process side channels
                # (e.g. the onebot event bridge that lives in *this* process,
                # not in the gateway's) can still be missing after an app
                # restart. Let the provisioner resurrect them.
                await self._ensure_side_channels(instance)
                # poll every 60s while healthy (the status probe hits the
                # network; on low-RAM VMs the gateway itself is the
                # resource hog, so keep supervision light)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=60)
                except TimeoutError:
                    continue
                return
            if current.status == "starting":
                # the child process is alive but its HTTP API is not up yet
                # (NapCat: waiting for a QR scan). Restarting here KILLS the
                # login flow and loops forever — the exact restart storm
                # from the user log (ready -> starting -> restarting every
                # 5s). Wait patiently; only a dead process ("stopped") or
                # an error falls through to the restart path.
                logger.debug(
                    "gateway %s.%s starting (%s); waiting",
                    instance.provider,
                    instance.instance_id,
                    current.error or "waiting for login",
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=30)
                except TimeoutError:
                    continue
                return
            if current.status == "stopped":
                if resume and first:
                    # startup resume: the gateway was running at shutdown
                    # but is not now — start it once
                    first = False
                    logger.info(
                        "gateway %s.%s: resuming (was running at shutdown)",
                        instance.provider,
                        instance.instance_id,
                    )
                else:
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
                async with self._start_limiter:
                    started = await self.provisioner(instance.provider).start(
                        instance.instance_id, instance.extra.get("options", {})
                    )
                # merge persisted options/autostart into the relaunched
                # instance's bare extra (same reason as in provision())
                started = started.model_copy(update={"extra": {**instance.extra, **started.extra}})
                self._instances[self._key(started.provider, started.instance_id)] = started
                await self._save_state(started)
                backoff = 5
                first = False
            except GatewayNotInstalledError as exc:
                # the payload is gone (deleted data dir, moved install):
                # retrying forever is pointless and hides the problem.
                # Mark the instance error so the UI can tell the user to
                # re-run the setup, then stop supervising it.
                failed = instance.model_copy(update={"status": "error", "error": str(exc)})
                self._instances[self._key(instance.provider, instance.instance_id)] = failed
                await self._save_state(failed)
                logger.error(
                    "gateway %s.%s payload missing; stopping supervision: %s",
                    instance.provider,
                    instance.instance_id,
                    exc,
                )
                return
            except Exception as exc:
                logger.error(
                    "gateway %s.%s restart failed: %s", instance.provider, instance.instance_id, exc
                )
                # surface the failure in the UI: an unpersisted error left
                # the table showing "running" while nothing would answer
                failed = instance.model_copy(
                    update={"status": "error", "error": f"restart failed: {exc}"}
                )
                self._instances[self._key(instance.provider, instance.instance_id)] = failed
                await self._save_state(failed)

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
        progress = InstallProgress()
        self._last_progress = progress  # polled by the TUI guide
        install_options = {**options, "_progress": progress}
        try:
            await provisioner.install(instance_id, install_options)
        except Exception as exc:
            instance.status = "error"
            instance.error = f"install failed: {exc}"
            await self._save_state(instance)
            raise RuntimeError(instance.error) from exc
        instance.status = "starting"
        await self._save_state(instance)
        try:
            async with self._start_limiter:
                running = await provisioner.start(instance_id, options)
        except Exception as exc:
            instance.status = "error"
            instance.error = f"start failed: {exc}"
            await self._save_state(instance)
            raise RuntimeError(instance.error) from exc
        # start() returns a bare extra ({port, pid}); the persisted
        # options/autostart must survive the swap or the supervisor's next
        # restart loses bot_url and the bridge never comes back
        running = running.model_copy(update={"extra": {**instance.extra, **running.extra}})
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


__all__ = ["GatewayManager", "GatewayNotInstalledError"]
