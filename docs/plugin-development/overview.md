# Plugin development

The authoritative interfaces are
[contracts.py](../../packages/mailflow-core/src/mailflow/contracts.py), with
[domain models](../../packages/mailflow-core/src/mailflow/domain.py) and
[configuration models](../../packages/mailflow-core/src/mailflow/config.py).
Implement the current core contract in both bundled and marketplace plugins;
do not copy protocol definitions into separate guides.

## Component types

| Kind / decorator | Required behavior |
| --- | --- |
| Mail source / `source` | Emit normalized mail; close owned connections; implement reply and optional history/preflight capabilities as declared |
| LLM backend / `llm` | Return `LLMCompletion`, including tool calls; accept `temperature`, `options` and `tools` |
| Processor / `processor` | Process `MailMessage` with `ProcessingContext`, return `ProcessorResult`, respect continue/stop decisions |
| LLM enhancer / `llm_enhancer` | Adapt system prompts, extra messages and resulting analysis through the three enhancer hooks |
| Notifier / `notifier` | Deliver a record; declare text/target capabilities only when actually implemented; propagate delivery failures |
| Storage / `storage` | Implement the complete storage protocol, including analysis updates, drafts, preferences, tasks and trash |
| Gateway / `gateway_provisioner` | Detect, install, start, inspect, provide QR data and stop an instance; isolate owned resources |
| Framework export / `bot_exporter` | Synchronously turn `BotExportContext` into `BotExportResult` and a usable generated host |

## Registration and dependencies

A distribution exposes one entry point in the `mailflow.plugins` group. The
entry point resolves to a plugin object implementing the two pluggy hooks;
`define_plugin` can generate them:

```python
from mailflow.config import NotifierConfig
from mailflow.domain import MailRecord
from mailflow.plugin_api import define_plugin

PLUGIN = define_plugin("mailflow-notify-demo", name="Demo", version="0.1.0")


@PLUGIN.notifier("demo")
class DemoNotifier:
    def __init__(self, config: NotifierConfig) -> None:
        self.config = config

    async def notify(self, record: MailRecord) -> None:
        print(record.mail.subject)


plugin = PLUGIN.build()
```

```toml
[project.entry-points."mailflow.plugins"]
mailflow-notify-demo = "mailflow_notify_demo.plugin:plugin"
```

Most factories receive their corresponding typed configuration. Gateway
provisioner factories take no arguments; exporters receive `BotExportContext`.
For custom hooks, form fields and probes, use
[PluginRegistrar](../../packages/mailflow-core/src/mailflow/registry.py).
The declarative registration API is
[plugin_api.py](../../packages/mailflow-core/src/mailflow/plugin_api.py).

Declare `mailflow-core` and all runtime dependencies in `pyproject.toml`.
Keep the distribution name, plugin ID, version and marketplace metadata aligned;
configuration `provider` selects the registered component ID. Avoid importing
bundled hosts or another plugin's private implementation. Installations must
resolve dependencies and report missing imports rather than silently skip them.

## Behavior that matters

Sources must use account/source-scoped mail identity. Advance a live UID/cursor
only after `emit` succeeds, preserve retryable items after batch failure, and
keep history browsing independent of live progress. Keep attachment bytes until
OCR/extraction completes. Handle cancellation and close network resources.

LLM transports must forward the caller's supported request parameters and return
structured tool calls. Preserve message roles and tool results. Unsupported
capabilities need an explicit error; never silently discard tools. A backend
that only accepts `messages` is incompatible with current routing.

Storage must preserve manual urgency, feedback and local task state during
reanalysis, deduplicate retries, and retain valid analysis when a new attempt
fails. Reply send failures must retain draft content. Notifiers must raise on
failed delivery so the runtime can retry without recording success.

Gateways own only their instance's processes, ports, files and bridges. Failed
startup and cancellation must clean up newly allocated resources. Generated bot
hosts must stop their service and pass external identity into shared permissions.

All hosts use bundled registration plus external entry-point discovery with the
same enable/disable filters. Loading errors must name the plugin and cause.
A plugin update or toggle may require restart; report that requirement.
User-visible form labels, progress and errors need English and Chinese resources.

## Validation and publishing

Use testkit fakes for source, model, notifier, HTTP and process boundaries.
Validate signatures, actual parameter forwarding, result types and declared
capabilities, including failure/cancellation paths; instantiation alone is not
a contract test.

```bash
make validate-plugins
```

Marketplace layout, metadata, README generation and the companion validator are
documented in the [plugin repository](https://github.com/Kingcxp/mailflow-repo).
Its validator calls the same `mailflow-testkit` contract checks as this workspace.
Keep live credentials and external services out of automated verification.
