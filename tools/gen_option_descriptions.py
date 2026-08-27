"""One-off generator: refresh config.desc.* entries in both locale packs.

Run from the repo root:  uv run python tools/gen_option_descriptions.py
It rewrites only the ``config.desc`` block of en.json and zh-CN.json so
every editable option (including list-entry fields) documents itself in the
Settings UI. Keeping it in the repo makes the descriptions reviewable and
regenerable instead of hand-edited JSON.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

LOCALE_DIR = Path("packages/mailflow-core/src/mailflow/locale")

EN: dict[str, str] = {
    "general.language": "Default display language code (en, zh-CN, or an external pack).",
    "general.summary_language": (
        "Language the LLM writes summaries, reasons and reply drafts in (e.g. zh-CN). "
        "Empty follows the interface language."
    ),
    "general.browser_mode": (
        "How the TUI opens external web links: system uses the system browser; "
        "graphical renders inside the terminal via a Carbonyl service; disabled turns it off."
    ),
    "general.browser_render_url": (
        "Base URL of a Carbonyl-compatible terminal render service (e.g. http://127.0.0.1:8080); "
        "required when browser_mode = graphical."
    ),
    "general.timezone": (
        "IANA timezone used for display, the daily cleanup and reminders (e.g. Asia/Shanghai)."
    ),
    "general.mail_retention_days": (
        "Mail older than this many days is moved to trash by the daily cleanup. 0 keeps everything."
    ),
    "general.trash_retention_days": (
        "Trash entries older than this many days, counted from deletion, are purged for good."
    ),
    "general.cleanup_hour": "Local-time hour (0-23) at which the daily retention cleanup runs.",
    "general.cleanup_minute": "Local-time minute (0-59) at which the daily retention cleanup runs.",
    "general.queue_size": (
        "Maximum number of fetched mails waiting to be analyzed; sources pause when it is full."
    ),
    "general.workers": (
        "How many mails are analyzed in parallel. Raise it for more LLM throughput."
    ),
    "general.reminder_days_before": (
        "How many days before a timed action's due date the early reminder fires."
    ),
    "general.reminder_hour": (
        "Local-time hour (0-23) of the early reminder and of the daily digest."
    ),
    "general.reminder_minute": (
        "Local-time minute (0-59) of the early reminder and of the daily digest."
    ),
    "general.reminder_interval_seconds": (
        "How often the scheduler checks for due reminders (10-3600 seconds)."
    ),
    "general.auto_update": (
        "Check once a day for MailFlow releases and plugin updates and install them automatically."
    ),
    "logging.level": (
        "Default level for every mailflow logger; a sink can only narrow it further."
    ),
    "logging.console": (
        "Print rich console output. The TUI turns this off so logs cannot corrupt the screen."
    ),
    "logging.console_level": "Minimum level written to the console sink.",
    "logging.console_redirect": (
        "Optional file path: also mirror console output there. Empty means no mirror."
    ),
    "logging.file": "Write a rotating plain-text log file.",
    "logging.file_path": "Path of the rotating text log file.",
    "logging.file_level": "Minimum level written to the text log file.",
    "logging.file_max_bytes": "Rotate the text log once it grows past this size in bytes.",
    "logging.file_backup_count": "How many rotated text log files to keep.",
    "logging.jsonl": "Write a JSON-lines log file for machine processing.",
    "logging.jsonl_path": "Path of the JSON-lines log file.",
    "logging.jsonl_level": "Minimum level written to the JSON-lines file.",
    "logging.logger_levels": (
        "Per-logger level overrides, one 'logger = LEVEL' per line (e.g. mailflow.runtime = DEBUG)."
    ),
    "plugins.enabled": (
        "Allowlist of plugin ids, one per line. Leave empty to load every installed plugin."
    ),
    "plugins.disabled": (
        "Plugin ids that are never loaded, one per line. Applies on the next start."
    ),
    "plugins.repositories": (
        "Plugin marketplaces the Market tab browses. Manage them with the Repositories button."
    ),
    "storage.provider": "Storage backend component id (built in: sqlite).",
    "storage.path": "Database file path. Relative paths resolve against the working directory.",
    "storage.options": ("Backend-specific options, one 'key = value' per line or a JSON object."),
    "i18n.language": (
        "Display language actually used at startup; overrides general.language when set."
    ),
    "i18n.extra_dirs": (
        "Directories holding extra data-only JSON language packs, one path per line."
    ),
    "accounts[].account_id": (
        "Unique id for this mailbox; used by logs, snapshots and the mailbox browser."
    ),
    "accounts[].provider": (
        "Mail source component id handling this mailbox (built in: imap, fake)."
    ),
    "accounts[].email": "Address of the mailbox; also used as the From address of replies.",
    "accounts[].enabled": "Poll this mailbox. Disable it to keep the settings without fetching.",
    "accounts[].options": (
        "Provider options: for imap set preset (qq/163/outlook/gmail) or imap_host and imap_port, "
        "plus username and password."
    ),
    "llms[].llm_id": "Unique id processors reference. Must be unique across all LLMs.",
    "llms[].name": "Human-readable name shown in the UI. Falls back to the id when empty.",
    "llms[].provider": "LLM backend component id (built in: openai-compatible, anthropic).",
    "llms[].base_url": (
        "Base URL of the API; the chat path is appended (e.g. https://api.openai.com/v1)."
    ),
    "llms[].api_key": (
        "API token. Prefer api_key_env or a ${ENV_VAR} placeholder so it never lands in the file."
    ),
    "llms[].api_key_env": (
        "Name of the environment variable holding the token; resolved at every start."
    ),
    "llms[].model": "Model identifier sent with each request (e.g. gpt-4o-mini).",
    "llms[].headers": (
        "Extra HTTP headers, one 'name = value' per line. Token-like values are redacted from logs."
    ),
    "llms[].query": "Extra query-string parameters, one 'name = value' per line.",
    "llms[].extra_body": "Extra JSON body fields merged into every request.",
    "llms[].timeout_seconds": "Per-request timeout in seconds (at least 1).",
    "llms[].max_retries": "How many times a failed request is retried before falling back.",
    "llms[].default": (
        "Default LLM for processors without an explicit one. The first entry in the list wins."
    ),
    "llms[].fallback": ("LLM ids tried in order when this one fails. Derived from the list order."),
    "llms[].options": "Backend-specific options (e.g. path = chat/completions).",
    "processors[].processor_id": "Unique name of this processor instance.",
    "processors[].provider": ("Processor component id (built in: rules, llm-importance)."),
    "processors[].enabled": "Run this processor as part of the chain.",
    "processors[].priority": "Execution order, ascending: lower numbers run first.",
    "processors[].llm": "Named LLM this processor uses. Empty for rule-based processors.",
    "processors[].fallback_llms": (
        "LLM ids tried in order when the primary one fails, one per line."
    ),
    "processors[].failure_policy": (
        "continue runs the next processor after a failure; stop halts the chain for that mail."
    ),
    "processors[].retries": "Extra attempts after the first failed run (0-5).",
    "processors[].timeout_seconds": "Per-mail timeout for this processor, in seconds.",
    "processors[].options": "Processor-specific options, one 'key = value' per line or JSON.",
    "notifiers[].notifier_id": "Unique name of this notification channel.",
    "notifiers[].provider": "Notifier component id (built in: console).",
    "notifiers[].enabled": "Deliver notifications through this channel.",
    "notifiers[].minimum_urgency": (
        "Only mail at or above this urgency is delivered: ad < info < important < urgent."
    ),
    "notifiers[].options": "Channel-specific options, one 'key = value' per line or JSON.",
}

ZH: dict[str, str] = {
    "general.language": "默认界面语言代码（en、zh-CN 或外部语言包）。",
    "general.summary_language": (
        "大模型撰写摘要、理由与回复草稿所用的语言（如 zh-CN）。留空则跟随界面语言。"
    ),
    "general.browser_mode": (
        "TUI 打开外部网页的方式：system 用系统浏览器；graphical 在终端内通过 Carbonyl "
        "渲染服务显示；disabled 关闭。"
    ),
    "general.browser_render_url": (
        "Carbonyl 兼容终端渲染服务地址（如 http://127.0.0.1:8080），browser_mode=graphical 时必填。"
    ),
    "general.timezone": "用于显示、每日清理与提醒的 IANA 时区（如 Asia/Shanghai）。",
    "general.mail_retention_days": "超过该天数的邮件会被每日清理移入回收站；填 0 表示不移动。",
    "general.trash_retention_days": "回收站中自删除起超过该天数的记录会被彻底清除。",
    "general.cleanup_hour": "每日清理执行的本地时间小时（0-23）。",
    "general.cleanup_minute": "每日清理执行的本地时间分钟（0-59）。",
    "general.queue_size": "等待分析的邮件队列上限；队列满时邮件源会暂停拉取。",
    "general.workers": "并行分析邮件的数量。调大可提升大模型处理吞吐。",
    "general.reminder_days_before": "在定时事项到期前多少天触发提前提醒。",
    "general.reminder_hour": "提前提醒与每日摘要的本地时间小时（0-23）。",
    "general.reminder_minute": "提前提醒与每日摘要的本地时间分钟（0-59）。",
    "general.reminder_interval_seconds": "提醒调度器检查到期事项的间隔秒数（10-3600）。",
    "general.auto_update": "每天检查 MailFlow 新版本与插件更新，并自动安装。",
    "logging.level": "mailflow 日志树的默认级别；各输出通道只能在此基础上收窄。",
    "logging.console": "输出富文本控制台日志。TUI 会关闭它，避免日志破坏界面。",
    "logging.console_level": "控制台通道记录的最低级别。",
    "logging.console_redirect": "可选文件路径：把控制台日志同时镜像到该文件。留空表示不镜像。",
    "logging.file": "写入按大小轮转的纯文本日志文件。",
    "logging.file_path": "文本日志文件路径。",
    "logging.file_level": "文本日志记录的最低级别。",
    "logging.file_max_bytes": "文本日志超过该字节数后进行轮转。",
    "logging.file_backup_count": "保留多少个轮转后的文本日志文件。",
    "logging.jsonl": "写入便于程序处理的 JSON Lines 日志文件。",
    "logging.jsonl_path": "JSON Lines 日志文件路径。",
    "logging.jsonl_level": "JSON Lines 日志记录的最低级别。",
    "logging.logger_levels": (
        "按 logger 覆盖级别，每行一条“logger = 级别”（如 mailflow.runtime = DEBUG）。"
    ),
    "plugins.enabled": "插件白名单，每行一个插件 id。留空表示加载全部已安装插件。",
    "plugins.disabled": "永不加载的插件 id，每行一个。下次启动生效。",
    "plugins.repositories": "插件市场标签页浏览的仓库列表。可用“远程仓库”按钮管理。",
    "storage.provider": "存储后端组件 id（内置：sqlite）。",
    "storage.path": "数据库文件路径。相对路径基于当前工作目录解析。",
    "storage.options": "后端专属选项，每行一条“键 = 值”，或填写 JSON 对象。",
    "i18n.language": "启动时实际使用的界面语言；设置后优先于 general.language。",
    "i18n.extra_dirs": "存放额外纯数据 JSON 语言包的目录，每行一个路径。",
    "accounts[].account_id": "该邮箱的唯一 id；日志、运行状态与邮箱浏览器都会引用它。",
    "accounts[].provider": "处理该邮箱的邮件源组件 id（内置：imap、fake）。",
    "accounts[].email": "邮箱地址，同时作为回复邮件的发件地址。",
    "accounts[].enabled": "是否轮询该邮箱。关闭后保留配置但不再收取邮件。",
    "accounts[].options": (
        "邮件源专属选项：imap 可设置 preset（qq/163/outlook/gmail）或 imap_host、imap_port，"
        "以及 username 与 password。"
    ),
    "llms[].llm_id": "处理器引用的唯一 id，在所有大模型中不可重复。",
    "llms[].name": "界面上显示的名称。留空时回退为 id。",
    "llms[].provider": "大模型后端组件 id（内置：openai-compatible、anthropic）。",
    "llms[].base_url": "API 基础地址，后面会拼接对话路径（如 https://api.openai.com/v1）。",
    "llms[].api_key": "API 令牌。建议改用 api_key_env 或 ${环境变量} 占位符，避免明文写入配置文件。",
    "llms[].api_key_env": "存放令牌的环境变量名，每次启动时解析。",
    "llms[].model": "每次请求发送的模型标识（如 gpt-4o-mini）。",
    "llms[].headers": "额外 HTTP 请求头，每行一条“名称 = 值”。疑似令牌的值会在日志中被打码。",
    "llms[].query": "额外查询参数，每行一条“名称 = 值”。",
    "llms[].extra_body": "合并进每次请求体的额外 JSON 字段。",
    "llms[].timeout_seconds": "单次请求超时秒数（至少 1）。",
    "llms[].max_retries": "请求失败后重试的次数，用尽后才切换到后备模型。",
    "llms[].default": "作为未显式指定模型的处理器的默认模型。列表中第一项即为默认。",
    "llms[].fallback": "该模型失败时依次尝试的模型 id，由列表顺序自动推导。",
    "llms[].options": "后端专属选项（如 path = chat/completions）。",
    "processors[].processor_id": "该处理器实例的唯一名称。",
    "processors[].provider": "处理器组件 id（内置：rules、llm-importance）。",
    "processors[].enabled": "是否在处理链中运行该处理器。",
    "processors[].priority": "执行顺序，升序排列：数值越小越先执行。",
    "processors[].llm": "该处理器使用的大模型 id。基于规则的处理器留空。",
    "processors[].fallback_llms": "主模型失败时依次尝试的模型 id，每行一个。",
    "processors[].failure_policy": "continue：失败后继续下一个处理器；stop：该邮件的处理链就此中断。",
    "processors[].retries": "首次失败后的额外重试次数（0-5）。",
    "processors[].timeout_seconds": "该处理器处理单封邮件的超时秒数。",
    "processors[].options": "处理器专属选项，每行一条“键 = 值”，或填写 JSON。",
    "notifiers[].notifier_id": "该通知渠道的唯一名称。",
    "notifiers[].provider": "通知器组件 id（内置：console）。",
    "notifiers[].enabled": "是否通过该渠道发送通知。",
    "notifiers[].minimum_urgency": "只有达到该紧急度及以上的邮件才会推送：ad < info < important < urgent。",
    "notifiers[].options": "渠道专属选项，每行一条“键 = 值”，或填写 JSON。",
}


def _write(path: Path, descriptions: dict[str, str]) -> None:
    payload: Any = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=OrderedDict)
    payload["messages"]["config"]["desc"] = OrderedDict(sorted(descriptions.items()))
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    missing = set(EN) ^ set(ZH)
    if missing:
        raise SystemExit(f"en/zh-CN description keys differ: {sorted(missing)}")
    _write(LOCALE_DIR / "en.json", EN)
    _write(LOCALE_DIR / "zh-CN.json", ZH)
    print(f"wrote {len(EN)} option descriptions to both locale packs")


if __name__ == "__main__":
    main()
