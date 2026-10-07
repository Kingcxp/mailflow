"""Re-analyze mails from a config's database through the real pipeline.

Used to check the analysis prompt against a live endpoint: takes the config
path, re-runs the LLM processor over stored mails (default: the ones that would
be affected by a past-deadline rule) and prints the resulting action items, so
a date-resolution fix can be judged on real mail rather than a stub.

    uv run python tools/reanalyze_stored.py tmp-verify/v.toml [--limit N]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mailflow.config import ProcessorConfig, load_config
from mailflow.contracts import ProcessingContext
from mailflow.processors import LLMImportanceProcessor


async def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("configs/development.toml")
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    from mailflow.domain import ComponentKind
    from mailflow.llm import LLMRouterImpl
    from mailflow_bundled import create_plugin_manager

    config = load_config(config_path)
    manager = create_plugin_manager(config, discover_external=False)
    registry = manager.build_registry()
    backends = {
        llm.llm_id: registry.llm_factory(llm.provider)(llm)
        for llm in config.llms
        if registry.has(ComponentKind.LLM_BACKEND, llm.provider)
    }
    if not backends:
        raise SystemExit(f"no LLM backend registered for {[llm.provider for llm in config.llms]}")
    router = LLMRouterImpl(backends, {llm.llm_id: llm for llm in config.llms})

    storage = registry.storage_factory(config.storage.provider)(config.storage)
    await storage.initialize()
    try:
        records = await storage.list_mails()
        records.sort(key=lambda record: record.mail.received_at, reverse=True)
        if limit:
            records = records[:limit]
        processor = LLMImportanceProcessor(
            ProcessorConfig(
                processor_id="llm-importance",
                provider="llm-importance",
                llm=config.llms[0].llm_id,
                fallback_llms=[llm.llm_id for llm in config.llms[1:]],
            ),
            router,
        )
        context = ProcessingContext(account_id="verify", timezone=config.general.timezone)
        kept_total = 0
        for record in records:
            result = await processor.process(record.mail, context)
            analysis = result.analysis
            if analysis is None:
                continue
            items = analysis.action_items
            kept_total += len(items)
            print(f"\n=== {record.mail.subject[:70]!r}")
            print(f"    sent={record.mail.date.isoformat()} ({analysis.urgency.value})")
            print(f"    reason={analysis.reason[:110]}")
            if not items:
                print("    action_items: (none)")
            for item in items:
                print(f"    -> {item.due_at.isoformat()} {item.action_type} {item.summary[:70]}")
        print(f"\ntotal action items across {len(records)} mail(s): {kept_total}")
    finally:
        await storage.close()


asyncio.run(main())
