<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark-dark.svg">
    <img src="assets/wordmark-light.svg" alt="Waymark Mail" width="660">
  </picture>
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="docs/configuration/overview.md#简体中文">使用说明</a> ·
  <a href="https://github.com/Kingcxp/waymark-mail-repo/tree/dev">插件仓库</a> ·
  <a href="https://github.com/Kingcxp/waymark-mail/issues">问题反馈</a>
</p>

<p align="center">
  <a href="https://github.com/Kingcxp/waymark-mail/actions/workflows/ci.yml?query=branch%3Adev"><img src="https://github.com/Kingcxp/waymark-mail/actions/workflows/ci.yml/badge.svg?branch=dev" alt="CI"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%2B-4b72bd" alt="Python 3.11+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-147d83" alt="MIT License"></a>
</p>

Waymark Mail 是一款终端邮件客户端，带有待办和机器人通知功能。
它将多个账户的邮件收在本地，方便阅读、搜索、回复，也能把邮件里的事情记成待办。

接入模型后，可以生成摘要，或直接提出“找出本周的发票”“把这封邮件记成待办”等需求。
涉及修改时会先请你确认。不配置模型，也可以正常收信、搜索和手动处理邮件。

## 功能

- **邮件**：保存常用筛选，查看往来，标记已读、收藏或归档，误删后从本地回收站恢复。
- **写信**：新建、回复、回复全部和转发，支持草稿、抄送、密送及附件。
- **待办**：分类、标签、可选截止时间，以及每日、每周、每月计划。可以只推迟本期，也可以暂停整个计划。
- **通知**：使用内置控制台，或连接聊天平台；按需选择通知预设、免打扰时段和接收目标。
- **文件与备份**：跨邮箱查找附件、批量下载，备份配置和本地数据。

界面支持中文和英文，邮件摘要跟随所选语言。内置 VS Code 深色、浅色和 Catppuccin
四套配色，也可以安装主题插件。

## 开始使用

以下步骤使用 **`dev` 分支**。需要 Python 3.11+、Git 和
[uv](https://docs.astral.sh/uv/)。

```bash
git clone --branch dev https://github.com/Kingcxp/waymark-mail.git
cd waymark-mail
uv sync --locked --all-packages --group dev
```

首次使用时复制一份示例配置：

| 终端 | 命令 |
| --- | --- |
| Linux / macOS | `cp configs/example.toml configs/local.toml` |
| Windows PowerShell | `Copy-Item configs/example.toml configs/local.toml` |

然后启动界面：

```bash
uv run --no-sync waymark tui -c configs/local.toml
```

在“邮箱与模型”中添加账户，选择仅接收新邮件或导入历史，再分别测试收信和发信。
模型和外部通知渠道可以稍后添加。若希望补收停用期间的邮件，请开启该账户的自动补收选项。

在邮件或待办列表旁输入需求，点击“智能操作”即可执行。“控制台”也支持命令交互，
可以先试试 `/waymark help`，或用 `@Waymark` 向助手提问。

日常命令保留 `--no-sync`，避免覆盖已安装的插件。需要同步依赖时再运行 `make sync`；
`make help` 只显示帮助。

## 后台运行

**F8** 返回终端，实例继续在后台运行；**Ctrl+Q** 停止实例。
也可以用命令启动、查看和重新连接后台实例：

```bash
uv run --no-sync waymark tui -c configs/local.toml --background --name inbox
uv run --no-sync waymark tui list
uv run --no-sync waymark tui attach inbox
uv run --no-sync waymark tui stop inbox
```

不需要界面时，使用 `waymark run -c configs/local.toml`。
远程使用时先配置服务端凭据，再运行 `waymark serve -c configs/local.toml`，
通过 `waymark tui --remote http://127.0.0.1:8800` 连接。
具体配置见[远程连接](docs/configuration/overview.md#远程与凭据)。

界面中按 **F1** 查看快捷键；命令行用 `waymark --help` 查看命令。
执行单条命令不会启动后台收信、提醒或备份任务。

## 数据与隐私

删除邮件默认移入本地回收站，保留 7 天。与原邮箱同步修改需要按账户主动开启。
完成待办会永久删除当期事项，周期计划则继续运行。

启用模型或通知渠道后，操作所需内容会发送给配置的服务。
插件会在程序所在环境中运行代码。配置、备份和导出的机器人插件包可能含有凭据，请妥善保存。

保留期限、自动备份和恢复方法见[数据与恢复](docs/configuration/overview.md#数据与恢复)。

## 从 MailFlow 升级

已有配置、SQLite 数据库、凭据和插件可以继续使用。
旧 `mailflow` 和 `/mailflow` 命令仍然有效，新示例统一使用 `waymark`。
Python 接口和插件标识保留原有的 `mailflow` 名称。

已有克隆可以更新远程地址，并添加新命令；这不会替换已安装的插件：

```bash
git remote set-url origin https://github.com/Kingcxp/waymark-mail.git
uv pip install --no-deps -e packages/waymark-mail
```

导入另一套配置时，在“设置 → 更多 → 导入旧配置”选择原 TOML 文件，查看预览后确认。
可以同时导入旧 SQLite 数据库，导入前会生成恢复备份。

插件仓库现名为 **[waymark-mail-repo](https://github.com/Kingcxp/waymark-mail-repo/tree/dev)**。
配置中保存的两个旧仓库地址会自动识别。

## 文档与贡献

| 文档 | 内容 |
| --- | --- |
| [使用说明](docs/configuration/overview.md#简体中文) | 邮箱、模型、通知、远程连接和常见问题 |
| [开发与贡献](docs/development/setup.md#简体中文) | 环境、检查、提交和发布 |
| [架构说明](docs/architecture/overview.md#简体中文) | 邮件处理、共享模型和运行流程 |
| [嵌入服务](docs/development/embedding.md#简体中文) | 在其他程序中使用核心服务 |
| [插件开发](docs/plugin-development/overview.md#简体中文) | 接口、能力声明和离线验证 |
| [更新记录](CHANGELOG.md) | 尚未发布的改动 |

欢迎通过 [Issues](https://github.com/Kingcxp/waymark-mail/issues) 反馈问题，说明版本、系统、
终端和复现步骤；附日志前请去除凭据和私人邮件。代码修改请提交到 `dev` 分支。

本项目使用 [MIT 许可](LICENSE)。
