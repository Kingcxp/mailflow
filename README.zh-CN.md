<div align="center">

# MailFlow

**统一多账户邮件收件箱：插件化过滤、LLM 智能分析、富终端界面与可嵌入核心**

*Unified multi-account mail inbox — plugin pipeline, LLM analysis, rich TUI, embeddable core*

[English](README.md) · [简体中文](README.zh-CN.md)

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![uv](https://img.shields.io/badge/uv-workspace-6c33af?logo=astral)](https://docs.astral.sh/uv/)
[![CI](https://github.com/Kingcxp/mailflow/actions/workflows/ci.yml/badge.svg)](https://github.com/Kingcxp/mailflow/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-201%20passed-67C23A)]()
[![Type checking](https://img.shields.io/badge/mypy%2Fpyright-strict-67C23A)]()
[![Linting](https://img.shields.io/badge/ruff-passing-67C23A)]()
[![Status](https://img.shields.io/badge/status-v0.1.0%20baseline-E6A23C)]()

</div>

MailFlow 把来自多个账户和提供商的邮件汇成一条流，用四级紧急度契约给每封邮件分类，
从邮件中提取带时间的待办（考试、会议、跑腿）形成日程表并提醒，把一切存进可恢复的回收站，
再通过 Textual 终端界面、彩色命令外壳或任何内嵌核心的聊天机器人宿主呈现出来。
用插件扩展它——邮件源、LLM 后端、处理器、通知器、存储、机器人导出器——插件从插件商城安装。

## 功能特性

- **多账户、多适配器** —— 提供商适配器合并成一条有界流；单账户故障隔离。
- **四级紧急度契约** —— `ad #909399`（垃圾）· `info #67C23A`（有用）· `important #E6A23C`（值得读）· `urgent #F56C6C`（立即处理）。
  颜色是公开契约的一部分，CLI、TUI 与通知器共用。
- **LLM 分析** —— OpenAI 兼容的 chat completions（可用于 OpenCode 中继、llama.cpp、vLLM）；具名 LLM 按序回退；
  结构化摘要、理由、回复草稿与带时间的待办项。
- **带时间待办 + 提醒** —— 考试/会议/跑腿，含时间、类型、内容与准备备注，可下钻到源邮件；
  到期前两天固定时刻与到期日零点触发提醒。
- **两步确认回复** —— 起草 → 准备（短期令牌）→ 确认；防重复发送，编辑会使令牌失效。
- **可恢复保留策略** —— 可配置邮件保留期（默认 30 天），每日 04:00 清理；删除的邮件可在回收站恢复 7 天。
- **丰富的日志** —— 基于队列的富控制台输出、轮转文件、JSONL；级别、重定向与密钥脱敏全部可配置；
  绝不触碰宿主的根日志器。
- **i18n** —— 内置英文（默认）与简体中文；其他语言以纯数据 JSON 包加载；语言选择持久化。
- **插件商城**（VS Code 风格）—— 搜索、分类筛选、Markdown 详情，命令行与 TUI 均可安装/卸载/启用/禁用；
  内置插件按类别组织（mail_source、processor、llm_backend、notifier、storage、bot_exporter）；
  禁用插件绝不会导致启动失败（孤立配置项会跳过并告警）。
- **机器人框架导出** —— 把配置好的实例导出为 NoneBot2、AstrBot 或任何其他聊天机器人框架的插件
  （`mailflow export --framework <id>`、带文件树的 TUI 导出向导、`make bot-plugin-*`）；
  导出器本身也是插件，新框架只需安装一个插件，无需改动核心。
- **完整配置管理** —— 每个选项在 TUI 与 `config` 命令中可见（必填/可选、默认值、说明、脱敏密钥）；`config set` 持久化。
- **质量门槛** —— 144 项单元/集成/端到端测试，mypy 与 pyright 严格模式，ruff 检查 + 格式化，
  Nuitka standalone/onefile 可执行文件，文档门槛。

## 安装

需要 **Python ≥ 3.11** 与 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/Kingcxp/mailflow.git
cd mailflow
uv sync --all-packages --group dev
```

## 快速开始

```bash
# 编辑 configs/development.toml：填入你的邮箱与 LLM 端点
uv run mailflow tui -c configs/development.toml
uv run mailflow shell -c configs/development.toml

# 或复制示例配置，填入令牌后运行
cp configs/example.toml configs/local.toml
export YOUR_TOKEN=your-token
uv run mailflow run -c configs/local.toml
```

详见 [docs/development/setup.md](docs/development/setup.md)。

## 命令

```
help                      彩色命令文档
mail list|show|delete|urgency <id> <level|auto>
action list|show|add|delete   定时任务；add "<摘要>" --due "YYYY-MM-DD HH:MM" [--type] [--notes]
plugin list|show          插件、适配器、账户、llms、绑定
plugin repo add|list|remove    管理插件商城
plugin market list|show|search <query>   浏览/搜索带 markdown 详情的插件
plugin install|uninstall <id>  安装或移除插件
plugin enable|disable <id>     启用/禁用插件（下次启动生效）
export --framework <id> --output <dir>   生成聊天机器人框架插件（NoneBot、AstrBot 等）
reply create|compose cn/en|edit|prepare|confirm|cancel   compose：信件模板（自动日期、署名右对齐）
lang get|set <code>       切换语言（持久化）
trash list|restore        恢复已删除的邮件
config list|get|set       查看和修改每个选项
```

## 内嵌到机器人或服务

```python
from mailflow.service import start_service
from mailflow_bundled import create_plugin_manager

service = await start_service(
    config,
    plugin_manager=create_plugin_manager(config),
    extra_log_handlers=[my_host_handler],
)
snapshot = service.snapshot()  # 插件、账户、LLMs、绑定
mails = await service.list_mails()  # 完整记录 + 分析 + 待办
service.on("mail.processed", handler)  # 异步事件
await service.commands.execute("mail list")
await service.stop()
```

详见 [docs/development/embedding.md](docs/development/embedding.md)。

## 紧急度契约

| 级别       | 颜色    | 含义                                             |
| ----------- | -------- | ------------------------------------------------- |
| `ad`        | #909399  | 无关广告 / 垃圾                                  |
| `info`      | #67C23A  | 有用但不紧急（课程通知）                          |
| `important` | #E6A23C  | 需要阅读（验证码）                                |
| `urgent`    | #F56C6C  | 必须现在或在特定时间处理（考试）                  |

手动覆盖在设置期间优先；重置恢复自动值。

## 质量门槛

```bash
make help           # 分组、带色的目标列表
make check          # lint + format + mypy + pyright + pytest + docs 门槛
make coverage       # 每包覆盖率报告
make build          # 为每个包构建 wheel
make bot-plugin-nonebot | bot-plugin-astrbot   # 导出 NoneBot / AstrBot 插件
make bot-plugin FRAMEWORK=<id> OUTPUT=<dir>     # 为任意已安装导出器导出
make exe-standalone # Nuitka standalone（onefile 前的冒烟测试）
make exe-onefile
```

## 文档

| 领域 | 链接 |
| ---- | ----- |
| 架构 | [总览](docs/architecture/overview.md) · [领域与邮件](docs/architecture/domain-and-mail.md) · [插件](docs/architecture/plugin-system.md) · [流水线](docs/architecture/pipeline.md) · [LLM](docs/architecture/llm.md) · [日志](docs/architecture/logging.md) · [存储与保留](docs/architecture/storage-and-retention.md) · [回复](docs/architecture/replies.md) · [TUI](docs/architecture/tui.md) · [机器人导出](docs/architecture/bot-export.md) |
| 开发 | [环境搭建](docs/development/setup.md) · [部署](docs/development/deployment.md) · [内嵌](docs/development/embedding.md) · [测试](docs/development/tests.md) · [质量](docs/development/quality.md) · [打包](docs/development/packaging.md) |
| 插件开发 | [总览](docs/plugin-development/overview.md) · [邮件源](docs/plugin-development/mail-source.md) · [处理器](docs/plugin-development/processor.md) · [LLM 后端](docs/plugin-development/llm-backend.md) · [通知器](docs/plugin-development/notifier.md) · [存储](docs/plugin-development/storage.md) · [机器人导出器](docs/plugin-development/bot-exporter.md) |
| 配置 | [总览](docs/configuration/overview.md) · [i18n](docs/configuration/i18n.md) |
| 给 AI 代理 | [不变量](docs/agent/invariants.md) · [模块地图](docs/agent/module-map.md) · [变更手册](docs/agent/change-playbook.md) |
| 决策 | [ADRs](docs/adr/0001-uv-workspace.md) · [0002-pluggy-pipeline](docs/adr/0002-pluggy-pipeline.md) · [0003-host-independent-core](docs/adr/0003-host-independent-core.md) |
| 构建历史 | [BUILD_LOG](docs/build-log/BUILD_LOG.md) · [English README](README.md) |

## 插件商城

从远程仓库浏览并安装插件：

```bash
uv run mailflow plugin repo add mailflow-repo https://github.com/Kingcxp/mailflow-repo
uv run mailflow plugin market list
uv run mailflow plugin market show mailflow-notify-ntfy
uv run mailflow plugin install mailflow-notify-ntfy     # 重启后加载
```

[mailflow-repo](https://github.com/Kingcxp/mailflow-repo) 仓库承载商城：每个插件一个文件夹，
按类别分组，添加插件只需一个 pull request，绝不触碰其他插件的文件。
其 docs/ 目录是插件开发指南，pull request 工作流只校验每个 PR 改动的插件。

**写你自己的插件** —— TUI 提供新插件向导（Market 标签 → New）：在目录树中选择文件夹，
可选创建子文件夹，选择模板类别（邮件源 / 处理器 / LLM 后端 / 通知器 / 存储 / 机器人导出器），
MailFlow 生成完整可加载的模板。内嵌核心的宿主也可通过 `mailflow.plugin_template.scaffold_plugin` 使用向导。

**把 MailFlow 导出为机器人插件** —— Market 标签的 Export 按钮打开同一个目录树向导，
选择框架（NoneBot、AstrBot 等）后从你的配置实例导出可安装的框架插件。
命令行等价物是 `mailflow export --framework <id> --output <dir>`；导出器是插件，
新框架只需安装一次。导出的插件内置完整聊天命令面：以 `mailflow`（NoneBot）或
`/mailflow`（AstrBot）开头的消息会分发到共享命令路由器，长回复自动拆分为多条消息，
每日日程摘要也会分页推送到聊天。参见 [docs/architecture/bot-export.md](docs/architecture/bot-export.md)。

**本地化与样式** —— 插件可在 plugin.json 中提供多语言一句话简介与 markdown readme
（`descriptions` / `readmes`）；CLI 与 TUI 自动使用与应用语言匹配的版本，
`market show` 用富 markdown 效果渲染 readme（**粗体**、~~删除线~~、`<span style="color:#ff5500">彩色文字</span>`）。

## 项目布局

```
packages/mailflow-core      与宿主无关的领域、流水线、服务门面、机器人导出
packages/mailflow-bundled   组合根：官方插件集
packages/mailflow-cli       富 Typer 宿主（run/command/shell/export/...）
packages/mailflow-tui       Textual UI（Mail/Actions/Runtime/Logs/Market/Settings + 导出向导）
packages/mailflow-testkit   测试用确定性假件（mailflow-mail-fake 是仅用于开发的源插件）
plugins/*                   可发现的适配器、处理器与机器人导出器
configs/ · translations/    示例配置与语言包
docs/                       架构、开发、代理文档
```

## 协议

MIT。真实提供商适配器（IMAP、Gmail、Outlook）是规划中的未来插件，不属于 0.1.0 基线——
参见 [CHANGELOG.md](CHANGELOG.md)。
