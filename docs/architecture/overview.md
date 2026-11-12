# Architecture

[Home](../../README.md) · [简体中文](#简体中文) · [Embedding](../development/embedding.md)

MailFlow is one service with multiple hosts. The terminal, CLI, authenticated
REST/WebSocket server and bot integrations share data models, business methods,
permissions and confirmations.

## Code map

| Location | Responsibility |
| --- | --- |
| `packages/mailflow-core` | Models, configuration, contracts, processing, scheduling, commands and service lifecycle |
| `packages/mailflow-bundled` | Explicit registration of shipped plugins |
| `packages/mailflow-cli` | Process entry points and shared command output |
| `packages/mailflow-tui` | Local/remote terminal interface and persistent terminal sessions |
| `packages/mailflow-server` | Authenticated HTTP/WebSocket transport and remote model conversion |
| `packages/mailflow-testkit` | Reusable fakes and offline contract verification |
| `plugins` | Bundled sources, model backends, notifiers, storage and framework exporters |
| [mailflow-repo](https://github.com/Kingcxp/mailflow-repo/tree/dev) | Marketplace metadata, additional plugins and generated usage pages |

The authoritative interfaces are [contracts.py](../../packages/mailflow-core/src/mailflow/contracts.py).
The service facade is [service.py](../../packages/mailflow-core/src/mailflow/service.py);
[workflows.py](../../packages/mailflow-core/src/mailflow/workflows.py) exposes shared
workspace operations for hosts. Neither transport nor UI owns a separate mail or task model.

## Mail and analysis

A source hands a normalized message to the runtime. The original is stored and
announced before analysis, then workers extract optional image text, run the
processor chain and save eligible results. New arrivals, history imports,
catch-up and reanalysis retain distinct notification semantics. Analysis tasks
are temporary and cancelled on stop; their old queues are not resumed on launch.

Identity includes source/account scope. Retrying within one account deduplicates
mail; identical Message-ID values in different accounts stay separate. Sources
advance cursors only after runtime handoff. History browsing does not advance the
live cursor, and failure later in a batch does not lose earlier accepted mail.
IMAP catch-up needs an explicit setting and a valid scoped UID/UIDVALIDITY cursor.

Attachment bytes survive until extraction finishes. Mail records persist text,
HTML, attachment metadata and OCR text; managed uploads and fetched attachment
caches are stored separately. Search covers subject, sender, body, summary,
filename and OCR text. Date filters use the source receipt timestamp, including
indexed queries and restored records.

Reanalysis retains manual priority, feedback, local task state and valid previous
results on failure. Lightweight wording changes do not create fresh reminder
identities. Generated analysis follows the selected language; stale-language
analysis is marked for reanalysis while the original remains accessible.

## Tasks, delivery and writing

One effective-task view drives lists, search, reminders, digests and cleanup.
Completing an occurrence removes it permanently and retains only the suppression
marker needed to prevent regeneration. Recurring plans continue independently;
pause/skip and per-occurrence changes preserve identity. Legacy state remains
readable, but there is no new completed-task history. Due windows use the
configured timezone; overdue unfinished items remain visible.

Notification selection respects effective task state, quiet hours and global,
channel and chat switches. Deferral is not success. Receipts are recorded only
on delivery acceptance, separately per target; retry does not resend successful
targets and rechecks that deferred content still matters. Capability declarations
control optional images/files and supported notification kinds.

Composition reuses drafts, preparation and explicit confirmation. Editing
invalidates the old confirmation. Per-draft locking prevents repeated or
concurrent confirmation from sending twice. A failed send preserves content;
an uncertain result is checked before retry. Failure to store a sent copy is
reported separately and does not send the message again.

## Source state and recovery

Local trash is the default deletion scope. Source read/star/archive synchronization
is opt-in per account. Explicit source trash/restore operations retain original
and resulting locations and a progress journal. Fresh known state is required
before retrying a pending operation; a read failure or UID change is not evidence
of deletion. Permanent deletion affects local data only.

Backup includes the supported configuration, SQLite data and available managed
files/caches. Restore validates the archive, creates a recovery backup and rolls
back on failure. Backups are delivered directly rather than reimported as
managed files, avoiding nested backups in later exports.

## Permissions and lifecycle

[PermissionContext](../../packages/mailflow-core/src/mailflow/permissions.py)
carries provider, sender, instance and chat identity. Read operations use the
ordinary-user permissions; mutation, sending, configuration, plugin and gateway
management require administration. Transports must provide explicit trusted
identity; model output and mail/plugin content cannot grant permissions.

Assistant plans and writing confirmations are bound to the initiator/session.
Cancellation revokes the server-side plan as well as the interface state.
History stores final content and references, not executable tasks or reusable
tokens; cancelled/expired confirmations remain distinguishable after restart.

The service owns storage, runtime loops, bridges and gateways. One-shot commands
and the command shell use `background=False`, so they do not launch automatic
source/reminder/backup loops or own another running instance's processes.
Runtime replacement stops affected old resources. Ordinary notification setting
changes update the relevant components without restarting unrelated gateways.
Shutdown cancels owned tasks and closes owned resources on the same event loop.
A detached persistent TUI remains alive until explicitly stopped.

## Further reading

[Configuration](../configuration/overview.md) covers user rules and defaults;
[embedding](../development/embedding.md) explains host integration;
[plugin development](../plugin-development/overview.md) defines extension boundaries;
[development](../development/setup.md) covers testing and contribution.

## 简体中文

[返回首页](../../README.zh-CN.md) · [English](#architecture)

MailFlow 由统一核心服务和多个入口组成。终端、命令行、远程服务及机器人共用模型、
权限、草稿、确认和业务方法；目录职责见上表，核心接口以 `contracts.py` 为准。

邮件先保存并可读，再进入可取消的分析任务。新邮件、历史导入、补收和重析区分通知语义，
退出后不恢复旧分析队列。身份包含账户范围，同账户重试去重、不同账户互不合并。
来源只有在运行时接收成功后才能推进游标；历史浏览不推进实时游标。
IMAP 补收需要主动开启且游标有效，不能把连接失败当成删除。

附件字节保留到识别结束，邮件记录保存元数据和识别文本，受管文件及下载缓存另行保存。
普通搜索覆盖正文、摘要、文件名和识别文字；日期筛选统一使用真实收件时间。
重析保留人工优先级、反馈、本地待办状态和上一次有效结果，不因轻微文案变化重复提醒。
生成语言跟随界面，旧语言结果可重析，原文不受影响。

所有待办入口使用统一有效视图。完成永久删除当期，仅留防重建标记；
周期计划独立继续，暂停、跳过和本期延期保持身份。逾期未完成事项仍可查看。
提醒遵守时区、免打扰和各层开关，延后不算成功，按目标保存投递凭据，重试重新检查内容。

写信沿用草稿、预览和确认；编辑使旧确认失效，并发或重复确认不重复发信。
发送失败保留内容，结果未知先核对；已发副本保存失败不再发送。
助手确认绑定用户和会话，取消同时撤销服务端计划；历史保存结果与引用，不保存可恢复任务或令牌。

默认删除仅进入本地回收站。原邮箱联动按账户开启，来源删除与恢复保存位置和操作进度；
重试前核对，未知状态不盲目执行。本地永久清除不影响原邮箱。
备份包含可用本地数据与文件，恢复先生成保护副本，失败回滚；导出不会作为文件再次存入下一份备份。

服务只拥有自己的任务、存储和网关资源。单次命令和交互命令行不启动自动循环，
普通通知设置不重启无关网关，重载和退出清理对应资源。分离终端后后台实例仍在运行。
