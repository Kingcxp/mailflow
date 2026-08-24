"""reconfigure() must not stall a settings change behind a source task that
is parked in a blocking connect (to_thread cannot be interrupted)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from mailflow.config import MailFlowConfig
from mailflow.events import EventBus
from mailflow.runtime import MailFlowRuntime


class _Store:
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...


async def test_reconfigure_returns_despite_uncancellable_source() -> None:
    runtime = MailFlowRuntime(
        MailFlowConfig(),
        sources={},
        account_configs=[],
        pipeline=None,  # type: ignore[arg-type]
        storage=_Store(),  # type: ignore[arg-type]
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
    )

    async def _run_source(account: Any, adapter: Any) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # simulate a to_thread connect that cannot be interrupted:
            # swallow the first cancel and keep blocking briefly
            await asyncio.sleep(0.2)
            raise

    runtime._sources = {"a": cast(Any, object())}  # pyright: ignore[reportPrivateUsage]
    runtime._account_configs = []  # pyright: ignore[reportPrivateUsage]
    task = asyncio.create_task(_run_source({"account_id": "a"}, None), name="source-a")
    runtime._tasks = [task]  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0)  # let the task start sleeping

    await asyncio.wait_for(
        runtime.reconfigure(
            config=MailFlowConfig(),
            sources={},
            pipeline=None,  # type: ignore[arg-type]
            notifiers=[],
            notifier_configs=[],
        ),
        timeout=3.0,
    )
    assert runtime._account_status.get("a") in (None, "stopped")  # pyright: ignore[reportPrivateUsage]
