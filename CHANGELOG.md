# Changelog

Notable user-visible changes are recorded here. `Unreleased` describes the
current `dev` branch, not a published release. See [GitHub Releases](https://github.com/Kingcxp/mailflow/releases)
for artifacts and Git history for individual commits.

## [Unreleased]

### Added

- Independent assistant tab with scoped requests, live progress, confirmed changes,
  source references and private, searchable session history.
- Saved rolling filters, account-isolated mail conversations, full composition
  with To/Cc/Bcc and attachments, and shared draft/confirmation state.
- Task categories/tags, recurring plans, per-occurrence adjustments, skip and
  pause controls. Completing a task removes the occurrence permanently.
- Attachment search/download, managed files on supported chat platforms,
  restore previews and opt-in scheduled backups.
- Notification presets, quiet hours, connection checks, per-target failures and
  optional rich messages on capable channels.
- Persistent terminal instances with list, attach, detach and stop operations.
- Opt-in startup catch-up from source cursors and per-account source state
  synchronization, including confirmed source trash/restore.

### Changed

- Mail originals appear before background analysis. History import and reanalysis
  are quiet by default; cancelled analysis is not resumed on the next launch.
- The assistant is separate from ordinary mail filtering; editing input or
  switching tabs does not silently execute or cancel a request.
- Generated analysis follows the interface language. Analysis in another language
  is eligible for failed-mail reanalysis without hiding the original mail.
- Mail retention is unlimited by default, source synchronization and automatic
  backups/updates are opt-in, and existing settings remain readable.
- CLI, TUI, remote service and bot integrations use shared permissions, drafts,
  effective-task rules and plugin contracts.
- Documentation now separates first use, operation, architecture and contribution;
  plugin usage remains generated from bilingual marketplace metadata.

### Fixed

- Stop help, cleanup, checks and application startup from implicitly synchronizing
  or replacing the Python environment. Only `make sync` installs dependencies;
  updates explicitly target the running interpreter.
- Preserve account-scoped mail identity, source handoff progress, MIME/OCR bytes,
  manual priority and valid previous analysis during retries and reanalysis.
- Use actual receipt dates consistently in filtering, migration and trash restore;
  retain ingestion origin when restoring mail.
- Verify the requested source operation before retrying; an unknown result does
  not resend a move/delete or overwrite its original source location.
- Keep attachment selection usable, retain failed selections and retry only those;
  avoid duplicate cached uploads.
- Recheck deferred notification content on retry and preserve successful target
  receipts. HTTP success with platform rejection is not a successful delivery.
- Revoke cancelled assistant confirmations in the service, isolate confirmations
  by user/session, and display cancelled/expired state in old and new history.
- Keep backups private, avoid including earlier exported backups recursively,
  and support local command creation, preview and confirmed restore.
- Avoid restarting unrelated gateways for ordinary notification changes and keep
  one-shot commands separate from background runtime/process ownership.

## [0.1.0]

Initial framework baseline: host-independent core, CLI and terminal hosts,
plugin discovery, SQLite persistence, optional model analysis, reminders,
confirmed reply drafts and workspace build/check tooling.

---

## 简体中文

`Unreleased` 记录当前 `dev` 分支中尚未发布的变化，不代表已发布版本；
实际产物见发布页面，逐次修改见 Git 历史。

本轮主要补齐独立智能助手、会话记录、邮件往来、完整写信与附件、周期待办、
通知预设与免打扰、后台终端实例、备份和主动开启的原邮箱联动。
邮件先可读再分析，历史导入与重析默认静默；完成待办永久移除当期，周期计划继续。
总结跟随界面语言，切换语言后可重析旧语言结果。

同时修复日期筛选、来源操作核对与重试、附件多选及失败恢复、通知按目标重试、
助手取消确认和旧历史状态，以及备份嵌套膨胀和本地备份命令。
数据与配置保持兼容，详细变化按上面的新增、调整和修复分类列出。
Make 帮助、清理、检查和启动不再隐式重装依赖；清理保留自定义名称的 Python 环境，
主动更新只操作正在运行的解释器。
