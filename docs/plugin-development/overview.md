# Plugin development

[Home](../../README.md) · [简体中文](#简体中文) ·
[Marketplace guide](https://github.com/Kingcxp/mailflow-repo/blob/dev/docs/00-getting-started.md)

The authoritative interfaces are
[contracts.py](../../packages/mailflow-core/src/mailflow/contracts.py), with
[domain models](../../packages/mailflow-core/src/mailflow/domain.py) and
[configuration models](../../packages/mailflow-core/src/mailflow/config.py).
Implement the current core contract in both bundled and marketplace plugins;
do not copy protocol definitions into separate guides.

## Component types

| Kind / decorator | Required behavior |
| --- | --- |
| Mail source / `source` | Emit normalized mail and close owned connections; declare optional history, send, attachment and source-state capabilities accurately |
| LLM backend / `llm` | Return `LLMCompletion`, including tool calls; accept `temperature`, `options` and `tools` |
| Processor / `processor` | Process `MailMessage` with `ProcessingContext`, return `ProcessorResult`, respect continue/stop decisions |
| LLM enhancer / `llm_enhancer` | Adapt system prompts, extra messages and resulting analysis through the three enhancer hooks |
| Notifier / `notifier` | Deliver a record; accurately declare text/target/image/file capabilities; propagate delivery failures |
| Storage / `storage` | Implement the storage protocol, including analysis updates, drafts, preferences, tasks and trash; support optional queries/backup only when implemented |
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

Full composition, attachment retrieval, send-result checks and source-state
changes are optional protocols. Declare only implemented capabilities. A legacy
reply source stays usable for replies; it must not appear to support compose,
move or restore merely because it is a mail source. Source mutations must scope
their identifiers, preserve locations after moves and report unknown results;
never expunge unrelated mail or infer deletion from connection failure.

LLM transports must forward the caller's supported request parameters and return
structured tool calls. Preserve message roles and tool results. Unsupported
capabilities need an explicit error; never silently discard tools. A backend
that only accepts `messages` is incompatible with current routing.

Storage must preserve manual urgency, feedback and local task state during
reanalysis, deduplicate retries, and retain valid analysis when a new attempt
fails. Reply send failures must retain draft content. Notifiers must raise on
failed delivery so the runtime can retry without recording success.

Rich notifications extend the existing text contract with `NotificationMessage`
and capability declarations. A channel supporting only `notify(record)` remains
valid for mail delivery, but does not thereby support reminders, chat commands,
images or files. Check HTTP and platform business results. When per-target
delivery is supported, return only after that target accepts the send. Shared
delivery owns pacing, quiet hours and retry receipts; do not add a competing queue.

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

## 简体中文

[返回首页](../../README.zh-CN.md) · [English](#plugin-development)

接口以核心的 `contracts.py`、领域模型和配置模型为唯一来源。两仓插件遵守同一契约，
不要在说明中复制另一套接口定义。插件类型和职责见上表；完整示例演示通过
`define_plugin` 注册文字通知器，并通过 `mailflow.plugins` 入口加载。

安装包名、插件标识、入口名称、版本和市场元数据保持一致，配置的 `provider`
选择注册的组件标识。所有运行依赖写入 `pyproject.toml`，安装必须解析依赖，
缺少导入或加载失败要报告具体插件和原因，不能静默跳过。

邮件源按账户隔离身份，成功交给运行时后才推进游标；历史浏览不改变实时进度，
批次失败不丢前面成功的邮件。附件保留字节到识别结束，支持取消并释放连接。
完整写信、取附件、核对发送结果、原邮箱移动与恢复都是可选能力，不支持时明确禁用。
移动后保留新位置，未知结果待核对，不能影响同文件夹内其他邮件。

模型后端接受实际调用传入的 `temperature`、`options`、`tools`，返回统一结构和工具调用，
不能静默丢弃参数。存储保留人工优先级、反馈和有效分析，重试去重，兼容旧数据。

通知插件只有真实投递成功才能正常返回；配置、HTTP 或平台业务失败必须抛出明确失败。
仅实现邮件推送不代表支持待办提醒、聊天、图片或文件。富消息扩展现有契约，
发送节奏、免打扰和按目标重试由共享层负责，不另建一套队列。

网关只管理本实例拥有的进程、端口、目录和桥接；取消、启动失败和退出要释放资源。
导出宿主负责服务生命周期并传递可信身份，所有提示通过中英文翻译组件。

使用 `make validate-plugins` 运行离线契约检查，验证真实参数、返回值、能力及失败路径，
不以“能实例化”代替行为验证。不连接真实服务，共享插件修改后同步验证两个仓库。
市场目录、元数据和 README 生成方式见插件仓库说明。
