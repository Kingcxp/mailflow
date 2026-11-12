# Embedding

[Home](../../README.md) · [简体中文](#简体中文) · [Architecture](../architecture/overview.md)

Use the same service entry point as the bundled hosts. Supply the bundled plugin
manager when those components are needed, keep it on one event loop, and always
await shutdown.

```python
import asyncio

from mailflow.commands import CommandRouter
from mailflow.config import load_config
from mailflow.permissions import PermissionContext
from mailflow.service import start_service
from mailflow_bundled import create_plugin_manager


async def main() -> None:
    path = "configs/local.toml"
    config = load_config(path)
    service = await start_service(
        config,
        config_path=path,
        plugin_manager=create_plugin_manager(config),
        background=False,
    )
    try:
        router = CommandRouter(service)
        # Resolve identity and administrator status in the host.
        context = PermissionContext(sender="reader", provider="my-host")
        response = await router.execute("mail list", context=context)
        print(response.text)
    finally:
        await service.stop()


asyncio.run(main())
```

A supplied plugin manager owns discovery; `create_plugin_manager` registers the
bundled set and discovers external entry points. Bare `mailflow-core` hosts can
provide their own manager and components. Do not import TUI or CLI internals.
This example opens local data for a command without launching mail sources,
reminders, automatic backups or gateway processes. For a long-running mail host,
use `background=True` and keep the service alive until the host shuts down.

## Service and permissions

[MailFlowService](../../packages/mailflow-core/src/mailflow/service.py) provides
mail/history access, analysis, tasks, reply drafts, settings, plugins and runtime
status. Use those methods rather than mutating storage or runtime internals.
Persistent settings need `config_path`; remote operations must match the server's
explicit supported methods.

Use `service.workspace_call(action, values)` for the shared workspace workflows,
including composition, files, assistant history, backup and source operations.
These operations retain their permission and capability checks. Keep managed
file references within the originating user/session; a file ID is not public
authorization. Never accept a model-provided filesystem path as an uploaded file.

Every external request needs a
[PermissionContext](../../packages/mailflow-core/src/mailflow/permissions.py)
with sender, provider, instance and chat IDs where applicable. Resolve
`is_admin` from the host's trusted configuration. Pass it to `CommandRouter`,
or wrap direct service calls in `permission_scope(context)`. The default
in-process context is trusted local administration and must not be inherited
by an external request.

Use `service.t(key, **params)` for user messages and translate exceptions through
the shared error component. Return the shared structured command response so
adapters can render equivalent results without rebuilding command logic.

## Events and logging

```python
async def on_processed(event: str, **payload) -> None:
    record = payload["record"]
    print(record.mail.subject)


unsubscribe = service.on("mailflow.mail.processed", on_processed)
# Call unsubscribe() when the host no longer needs the subscription.
```

Runtime events include `mailflow.mail.received`, `mailflow.mail.processed`,
`mailflow.account.error`, `mailflow.cleanup.done` and `mailflow.action.reminder`.
Service events include `mail.deleted`, `mail.urgency.changed`, `reply.created`,
`reply.sent`, `language.changed` and `config.changed`. Handlers receive `event=`
and payload keyword arguments. REST/WebSocket hosts convert these into the
shared remote event frame rather than inventing another payload shape.

Pass host log handlers via `extra_log_handlers` when starting the service.
Core does not reconfigure the root logger. Avoid logging request credentials,
raw configuration or secret-bearing transport headers.

## Ownership

The service owns runtime tasks, storage, gateways and bridges. Let its reload
path replace affected components and stop the previous ones. Stop the service before
closing the host event loop; do not start a second runtime over the same service
or independently close resources it still owns.

Assistant plans and confirmation tokens are temporary. On cancellation, call
the shared cancellation operation rather than only clearing host widgets.
Persist final history and object references through the service; do not restore
tasks or replay confirmations after restarting your host.

See [architecture](../architecture/overview.md) for data rules and
[plugin development](../plugin-development/overview.md) for component interfaces.

## 简体中文

[返回首页](../../README.zh-CN.md) · [English](#embedding)

通过 `start_service` 使用与内置入口相同的核心服务；需要内置组件时传入
`create_plugin_manager(config)`，不要依赖界面或命令行私有实现。
上面的示例使用 `background=False`，只打开本地数据，不启动收件、提醒、备份或网关进程。
长期运行的邮件宿主改用 `background=True`，并在同一事件循环上等待服务停止。
有插件管理器时由它负责发现，不需要再次扫描。

外部请求必须显式提供 `PermissionContext`，包含平台、发起人、实例和会话身份，
管理员状态由宿主可信配置决定。传给 `CommandRouter.execute`，或在直接调用服务时使用
`permission_scope(context)`。不能让外部请求继承默认的本地管理员身份。

邮件、待办、配置和草稿通过服务方法处理；写信、附件、助手历史、备份和来源操作可使用
共享 `workspace_call`，不要绕过权限或直接修改存储。文件引用只属于原用户和会话，
不能把模型生成的本地路径当成用户上传文件。提示使用翻译组件，异常走共享错误转换。

通过 `service.on` 订阅事件，不再使用时调用返回的取消订阅函数。
日志处理器可在启动时传入，不要记录凭据、请求密钥或完整配置。
服务拥有其启动的任务、存储、桥接和网关；重载只替换受影响组件，宿主退出前必须等待
`service.stop()`，不要额外启动一份运行时或提前关闭服务仍在使用的资源。

助手取消必须调用共享取消操作，不能只清空界面。重启仅恢复历史结果与引用，
不恢复执行任务或旧确认令牌。
