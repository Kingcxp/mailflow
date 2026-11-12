# Configuration and operation

[Home](../../README.md) · [简体中文](#简体中文) · [Development](../development/setup.md)

Start with [configs/example.toml](../../configs/example.toml), copied to the
ignored `configs/local.toml`. Pass `-c configs/local.toml` consistently when
starting a host or running a command. Without `-c`, built-in defaults are used;
saving settings needs a writable configuration path. Relative data paths use
the process working directory, so launch from the same directory each time.

```bash
uv run mailflow config-check -c configs/local.toml
uv run mailflow tui -c configs/local.toml
```

`config-check` checks configuration locally; it does not prove a mailbox, model
or chat connection works. The settings interface provides the corresponding
connection checks. Field definitions live in [config.py](../../packages/mailflow-core/src/mailflow/config.py);
provider options come from the plugin's form and usage page.

Use `api_key_env`, `password_env`, or a supported whole-string `${ENV_VAR}`
reference for secrets. An unset referenced variable fails loading; embedded
`prefix-${VAR}` text stays literal. Saving preserves environment references.

## Accounts and startup

Keep `account_id` stable: it is part of mail identity. An IMAP account can use a
preset (`qq`, `163`, `gmail`, `outlook`) or explicit server settings. Receiving
and sending are separate capabilities; test both if you intend to send mail.
Credentials must match the mailbox service's requirements; a preset does not
provide OAuth or bypass provider access policies.

```toml
[[accounts]]
account_id = "personal"
provider = "imap"
email = "you@example.com"
enabled = true
state_sync = false

[accounts.options]
imap_host = "imap.example.com"
smtp_host = "smtp.example.com"
password = "${MAIL_PASSWORD}"
initial_sync = "new"
catch_up_missed = false
```

| Account option | Meaning |
| --- | --- |
| `initial_sync = "new"` | Default: start with newly arriving mail; do not analyze existing history automatically |
| `initial_sync = "recent"` / `"all"` | Explicitly import recent history (`initial_days`, default 30) or all available history |
| `catch_up_missed = true` | On restart, receive missed messages from a valid saved source cursor; off by default |
| `catch_up_summary` | Send a catch-up summary instead of individual new-mail notifications; defaults to true |
| `include_past_actions` | Opt in to extracting past tasks during history import; defaults to false |
| `limit` / `interval_seconds` | IMAP batch size / polling interval, defaults 20 / 300 seconds; `limit` is not an implicit startup history request |

Original mail becomes readable before background analysis finishes. History
browsing does not move the live cursor. History import and reanalysis are quiet
by default; stopping analysis or exiting cancels that task without restoring an
old analysis queue next time. A detached TUI is still running, so detach is not
a stop operation. An invalid cursor requires an explicit resynchronization
range; connection errors are never treated as deleted mail.

Source state synchronization is separately enabled with `state_sync = true`.
The account must declare the needed capabilities. Read/star/archive changes can
synchronize both ways; source trash/restore requires explicit confirmation.
Folder detection can be overridden with `archive_folder`, `trash_folder` and
`sent_folder`. Unknown results stay pending for verification rather than being
blindly repeated. Local permanent deletion never purges the source mailbox.

## Models, analysis and language

Add a model in settings or `[[llms]]`. The provider is a registered component ID,
not the Python package name. An explicit processor `llm` / `fallback_llms` binding
is preserved while valid. Without one, `default = true` selects the primary model,
falling back to the first entry when no default is marked; unspecified fallbacks
are filled from the remaining list. The TUI model controls update these bindings.
Without a usable model, rule processing remains available. The assistant needs
a model supporting the requested tools. Optional request fields are documented
by each backend in the [plugin repository](https://github.com/Kingcxp/mailflow-repo/tree/dev/llm_backend).

```toml
[[llms]]
llm_id = "primary"
provider = "openai-compatible"
base_url = "https://your-provider.example/v1"
model = "your-model-id"
api_key_env = "MAILFLOW_MODEL_KEY"
```

English (`en`) and Simplified Chinese (`zh-CN`) are built in. Select a language
in the interface or use `lang set zh-CN` in the command shell. Generated
summaries, analysis and briefings follow that language. Older analysis in a
different language is marked as failed and can be updated with the failed-mail
reanalysis operation; the original mail remains readable. The legacy
`general.summary_language` field is accepted for compatibility, not as an
override. Additional data-only language packs use `[i18n].extra_dirs`; see
[translations](../../translations).

OCR is optional and disabled by default. Install `rapidocr-onnxruntime` into the
same Python environment, then enable `general.ocr_images` (or the account
option). Existing extracted text remains readable and searchable when OCR is off.
Attachment download is independent of OCR.

## Tasks and notifications

Tasks may have no deadline. Overdue unfinished tasks remain available. Completing
a task permanently removes that occurrence; it cannot be reopened from a
completed-history view. Daily/weekly/monthly plans keep generating future
occurrences unless paused or ended. Future terminal previews are not reminders.
Snoozed, muted, deleted and completed items are excluded from applicable reminders.
The terminal previews the next 30 days. Monthly plans use month-end when their
chosen day is absent. Resuming a plan creates only the current occurrence rather
than every missed one, while existing unfinished occurrences remain available.

Choose a notification preset in settings or adjust the existing switches:

| Scope | Settings |
| --- | --- |
| Global | `general.mail_notifications`, `task_reminders`, `daily_digest`, `hourly_summary` |
| Channel | `enabled`, `minimum_urgency`, and the corresponding switches under `notifiers.options` |
| Chat | Subscription and per-chat summary choices, on channels supporting them |
| Quiet hours | `general.quiet_enabled`, `quiet_start`, `quiet_end`, in `general.timezone` |

Presets combine existing switches; they preserve credentials, administrators,
subscriptions and gateway settings. Both the global and applicable channel/chat
switches must permit delivery. Hourly summaries need global and channel opt-in.
Quiet hours are off by default, with 22:00–08:00 prefilled. Deferred content is
rechecked and combined after quiet hours; deferral is not a successful delivery.
Successful destinations are not sent the same failed delivery again.

The default reminder scan is every 60 seconds, with an early reminder two days
before at 08:00 and a due-day window. Missed historic windows are not replayed as
separate messages. Daily digests have independent optional hour/minute fields,
falling back to reminder time. Recurring tasks normally remind once per occurrence.

Bot setup distinguishes installation, running, login and delivery readiness.
Configure platform administrators and fixed targets, complete login when needed,
and run a delivery test separately from connection checks. Unsubscribing a chat
removes its subscription; any separately configured fixed target is reported.
Images/files depend on channel capabilities; text-only plugins do not provide
chat commands or every reminder type. Supported chat senders share pacing,
controlled by `options.send_interval_seconds` (default 2).

## Remote access

Add server authentication before starting the server:

```toml
[server]
host = "127.0.0.1"
port = 8800
username = "admin"
password_env = "MAILFLOW_SERVER_PASSWORD"
```

Set the referenced environment variable, then use:

```bash
uv run mailflow serve -c configs/local.toml
uv run mailflow tui --remote http://127.0.0.1:8800
```

The login screen requests credentials; remembered passwords use the operating
system credential store. Remote mail, tasks, writing, assistant operations,
files and backup/restore run against server data. Upload dialogs read client
files. Local plugin installation and gateway process management are not remote
TUI actions. Protect non-local access with HTTPS at a reverse proxy and network
access controls; the server uses shared administrator permissions.

## Data, backup and cleanup

The attachment library searches filenames, types, dates and source accounts and
can return to the originating mail. Downloaded/managed file references belong
to their user/session. Remote upload reads a client file; a model cannot invent
a path to upload. Managed files currently have a per-file limit of 4,000,000
bytes; platform limits may be lower. Backup export uses a separate archive path
and is not constrained by the ordinary managed-upload limit.

SQLite defaults to `data/mailflow.db`; logs default to `logs/mailflow.log`.
Mail is kept indefinitely (`general.mail_retention_days = 0`), trash for 7 days.
Enabled retention cleanup runs at 04:00 in the configured timezone and protects
important/starred mail and mail with unfinished tasks. Age-based cleanup is
separate from a task's due date. Existing retention choices are preserved.

Use Settings for backup creation, restore preview and confirmed restore. From
the local command shell:

```text
backup create --path backups/mailflow.zip
backup preview backups/mailflow.zip
backup restore backups/mailflow.zip CONFIRM
```

Without `--path`, local command backups go beside the configuration in a
`backups` directory. Automatic backup is separately opt-in with
`general.auto_backup = true`: by default 04:30, latest 7 copies, using
`backup_directory`, `backup_time` and `backup_keep`. A missed run is caught up
once. Backups include configuration, SQLite state and managed/cached files;
uncached source attachments still require mailbox access. A restore creates a
recovery backup and rolls back on failure. Treat backup archives as secrets;
bot backup export is only available to an administrator in a private chat.

`make clean` removes development output, not mail. The separately named
`clean-records` and `clean-trash` targets modify data in
`configs/development.toml`; the latter is irreversible. They are not setup steps.

## Common problems

| Symptom | Check next |
| --- | --- |
| Inbox stays empty | Account enabled, receive login, selected folder and initial/catch-up choice; use history browsing for older mail |
| Mail is readable but analysis fails | Active model, language, fallback and connection; retry failed analysis without reimporting mail |
| Reply cannot send | SMTP settings and source send capability; retain/edit the same draft before retrying |
| No notification | Effective global/channel/chat state, urgency, quiet hours, login and delivery failure list |
| Setting save fails | Writable `-c` file and its error message; inputs are retained for correction |
| Different data after restart | Same configuration path, working directory and storage path |

Use `mailflow doctor -c configs/local.toml` for local registration/configuration
information, not a live platform test. `mailflow --help` and the shared `help`
command give current syntax. See [contributing](../development/setup.md#contributing)
for safe issue reports.

## 简体中文

[返回首页](../../README.zh-CN.md) · [English](#configuration-and-operation)

将示例复制为 `configs/local.toml`，运行时统一传入 `-c configs/local.toml`。
未指定路径时使用内置默认值；保存设置需要可写的配置文件。相对数据路径以工作目录为准，
重启时请使用相同工作目录。`config-check` 仅检查配置，不会验证真实连接。

### 账户与启动

账户标识 `account_id` 参与邮件去重，请保持稳定。IMAP 可选择预设或填写服务器；
收件与发件需要分别检查，预设不提供 OAuth，也不能绕过邮箱服务的访问限制。
上面的账户示例可直接参考，密码通过环境变量提供。

| 选项 | 含义 |
| --- | --- |
| `initial_sync = "new"` | 默认只接收新邮件，不自动分析已有历史 |
| `"recent"` / `"all"` | 明确选择导入近期或全部历史；`initial_days` 默认 30 天 |
| `catch_up_missed = true` | 根据有效游标补收停用期间邮件，默认关闭 |
| `catch_up_summary` | 补收后发送汇总，默认开启，不逐封刷通知 |
| `include_past_actions` | 是否提取历史过期待办，默认关闭 |
| `limit` / `interval_seconds` | 每批大小／轮询间隔，默认 20／300 秒，不代表启动自动导入最近 20 封 |

原邮件先显示，分析在后台进行。历史浏览不改变收件游标。导入和重析默认静默，
终止或退出取消本次分析，下次不自动恢复旧队列；F8 分离后实例仍在运行。
游标失效需要主动选择重新同步范围，不会静默全量导入。

原邮箱联动由账户的 `state_sync` 单独开启，默认关闭，并受来源实际能力限制。
已读、收藏、归档可以双向同步；原邮箱移入回收站和恢复需要明确确认。
目录可通过 `archive_folder`、`trash_folder`、`sent_folder` 指定。
网络错误不视为删除；未知结果先核对，不盲目重试。永久清除只作用于本地。

### 模型与语言

在界面或 `[[llms]]` 中添加模型，`provider` 是组件标识，不是安装包名。
处理器有效的 `llm`、`fallback_llms` 显式绑定优先保留；未指定时使用标为
`default = true` 的主模型，没有默认项则取首项，未配置的后备由剩余列表补齐。
界面模型操作会同步绑定，仅手改列表顺序不一定覆盖已有绑定。没有可用模型时保留规则处理。
智能助手需要支持相应工具调用的模型，额外选项见对应插件说明。

通过界面或命令 `lang set zh-CN` 切换语言。总结、分析和摘要跟随当前界面语言；
旧语言的分析标为失败，可使用“重析失败邮件”更新，原文仍然可读。
旧字段 `general.summary_language` 仅兼容读取，不再覆盖语言选择。
额外 JSON 语言包通过 `[i18n].extra_dirs` 加载。

图片文字识别默认关闭，需在相同环境安装 `rapidocr-onnxruntime`，再开启
`general.ocr_images` 或账户选项。已识别文字不因关闭功能而消失，附件下载与识别独立。

### 待办与通知

待办允许没有截止时间，逾期后仍保留。完成会永久删除当期，不提供完成历史或重新打开；
周期计划继续运行，暂停和结束计划是独立操作。未来预览不进入提醒。
已推迟、关闭提醒、删除或完成的事项遵守统一的提醒过滤规则。
终端默认预览未来 30 天；月份没有指定日期时按月底处理。
恢复计划只生成当前一期，不补出停用期间全部事项，已有未完成事项仍保留。

通知预设复用现有开关，不改变凭据、管理员、订阅或网关。
全局、渠道及适用的聊天开关共同决定是否投递，每小时摘要需全局和渠道同时开启。
免打扰默认关闭，预填 22:00—08:00，使用配置时区；结束后重新检查并合并仍有意义的内容，
延后不算发送成功。部分目标失败只重试失败部分。

默认每 60 秒扫描提醒，在截止前两天 08:00 及到期当天窗口提醒；不逐条补发历史时间点。
每日摘要可单独设置时间，未设置时沿用提醒时间。周期待办通常每期提醒一次。

机器人需区分安装、运行、登录和可投递状态。配置管理员与固定目标，按需完成登录，
连接自查和实际测试发送是不同操作。取消订阅会移除订阅目标，并说明是否仍有固定推送目标。
图片、文件、聊天命令和提醒类型以插件实际能力为准；支持的聊天发送共用节奏，
`options.send_interval_seconds` 默认 2 秒。

### 远程与凭据

参考上面的 `[server]` 示例设置账号和环境变量密码，再启动 `serve`，通过远程终端登录。
记住的密码交给系统凭据存储。远程邮件、待办、写信、助手、附件和备份恢复操作服务端数据，
上传读取客户端文件。插件安装与网关进程管理在本地执行。
外网访问应通过 HTTPS 代理并限制网络访问。

密钥可使用 `api_key_env`、`password_env` 或受支持字段中的完整 `${ENV_VAR}` 引用。
变量未设置会导致配置加载失败，嵌在其他文字中的占位符不展开。保存保留变量引用，
不要把真实凭据放入公开示例或问题报告。

### 数据与恢复

附件库按文件名、类型、日期和账户查找，可返回来源邮件。
文件引用属于发起人和会话，远程上传读取客户端文件，模型不能凭空引用本地路径。
当前受管文件单个上限为 4,000,000 字节，平台可能有更小限制；备份导出走独立归档流程，
不受普通受管上传上限限制。

默认数据库为 `data/mailflow.db`，日志为 `logs/mailflow.log`。
邮件默认长期保留，回收站默认 7 天；主动开启按天数清理后，默认在配置时区 04:00 执行，
并保护重要、收藏和关联未完成待办的邮件。存放时间与待办到期时间不是同一规则。

在设置中创建备份、预览并确认恢复，或使用上面的 `backup` 命令。
命令未指定 `--path` 时保存到配置文件旁的 `backups` 目录。
自动备份单独开启，默认每天 04:30、保留 7 份，漏过时下次运行补一份；
由 `auto_backup`、`backup_directory`、`backup_time`、`backup_keep` 控制。
备份包含配置、SQLite 数据和受管／已缓存文件，未下载附件仍需从原邮箱获取。
恢复前生成保护备份，失败可回滚。备份包含敏感数据，机器人仅允许管理员私聊导出。

`make clean` 只清理开发产物。`clean-records`、`clean-trash` 使用
`configs/development.toml` 修改用户数据，后者不可恢复，不应作为安装步骤执行。

| 问题 | 优先检查 |
| --- | --- |
| 收件箱为空 | 账户启用、收件登录、文件夹、初始导入和补收选项；旧邮件使用历史浏览 |
| 原文可读但分析失败 | 模型、语言、后备和连接，直接重析失败项 |
| 回复发不出 | SMTP 和来源发送能力，保留同一草稿再重试 |
| 没有通知 | 实际生效的开关、重要程度、免打扰、登录及失败记录 |
| 保存失败 | `-c` 文件是否可写；按提示修正，输入不会丢失 |
| 重启后数据不同 | 配置路径、工作目录和数据库路径是否一致 |

`doctor` 提供本地配置和注册信息，不是真实连接测试。完整参数以 `mailflow --help`
和共享 `help` 输出为准，反馈问题前请脱敏。
