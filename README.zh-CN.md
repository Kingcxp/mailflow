# MailFlow

**在终端中管理邮件、后续待办和可选的智能助手。**

[English](README.md) · [配置说明](docs/configuration/overview.md#简体中文) ·
[参与贡献](docs/development/setup.md#简体中文) ·
[问题反馈](https://github.com/Kingcxp/mailflow/issues)

[![CI](https://github.com/Kingcxp/mailflow/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/Kingcxp/mailflow/actions/workflows/ci.yml?query=branch%3Adev)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)

MailFlow 将多个账户汇入本地收件箱，提供阅读整理、写信、周期待办和聊天通知。
你可以使用终端界面、共享命令，或连接自己部署的远程服务。
模型并非必需：未配置模型时仍可阅读邮件、普通搜索和使用规则处理。

## 主要功能

| 范围 | 能做什么 |
| --- | --- |
| 邮件 | 分析完成前即可阅读原文；按账户和日期筛选，保存动态更新的常用筛选，查看往来，收藏和归档 |
| 搜索与助手 | 搜索正文、附件名称和图片识别文本；在独立助手页提出需求，查看进度，确认后执行修改 |
| 写信 | 新建、回复、回复全部、转发，支持多收件人、抄送、密送和附件，发送前预览并确认 |
| 待办 | 可不设截止时间，支持分类、标签、每日／每周／每月计划，本期延期、暂停计划和推迟提醒 |
| 通知 | 连接已有机器人或推送插件，选择通知预设、免打扰和目标，查看并重试投递失败项 |
| 附件与恢复 | 集中查找和下载附件，在支持的聊天渠道收发文件，恢复回收站，手动或定时备份 |
| 多入口与扩展 | 本地和远程终端、命令行及已有机器人共用业务逻辑，可通过插件扩展邮件源、模型、处理、推送和存储 |

内置中文和英文。邮件总结等生成内容跟随界面语言；旧语言的分析结果可重新解析。

## 开始使用

以下步骤对应 **`dev` 分支**。需要 Python 3.11+、Git 和
[uv](https://docs.astral.sh/uv/)。持续集成覆盖 Windows 和 Linux。

```bash
git clone --branch dev https://github.com/Kingcxp/mailflow.git
cd mailflow
uv sync --locked --all-packages --group dev
```

首次使用时复制示例配置，再启动应用：

```bash
# Linux / macOS
cp configs/example.toml configs/local.toml
```

```powershell
# Windows PowerShell
Copy-Item configs/example.toml configs/local.toml
```

```bash
uv run --no-sync mailflow config-check -c configs/local.toml
uv run --no-sync mailflow tui -c configs/local.toml
```

初次同步后，日常命令使用 `--no-sync`，保留已安装的插件和可选依赖。
`make help` 和 `make clean` 不安装依赖，需要同步时主动运行 `make sync`。

进入界面后添加邮箱，选择仅接收新邮件或导入历史，分别检查收件与发件是否就绪。
需要总结和智能助手时再添加模型，需要外部推送时再连接通知渠道。
示例不含账户或模型凭据；本地配置不要公开，密钥建议通过环境变量提供。

若希望补收停用期间的邮件，请开启账户的自动补收选项。补收使用已保存的来源游标，
不会静默导入整个邮箱。详见[账户与启动规则](docs/configuration/overview.md#简体中文)。

## 运行方式

| 用途 | 命令 |
| --- | --- |
| 终端界面 | `uv run --no-sync mailflow tui -c configs/local.toml` |
| 后台终端实例 | `uv run --no-sync mailflow tui -c configs/local.toml --background --name inbox` |
| 查看／重新连接 | `uv run --no-sync mailflow tui list` / `uv run --no-sync mailflow tui attach inbox` |
| 前台服务 | `uv run --no-sync mailflow run -c configs/local.toml` |
| 需认证的远程服务 | `uv run --no-sync mailflow serve -c configs/local.toml` |
| 远程终端 | `uv run --no-sync mailflow tui --remote http://127.0.0.1:8800` |
| 交互命令行 | `uv run --no-sync mailflow shell -c configs/local.toml` |
| 执行单条命令 | `uv run --no-sync mailflow command "help" -c configs/local.toml` |

**F8 分离终端、保留后台运行；Ctrl+Q 停止实例。**
使用 `mailflow tui stop inbox` 可停止指定后台实例。
单条命令和交互命令行不会启动自动收件、提醒或备份循环。

远程使用前需配置服务端凭据。远程终端操作服务端数据，上传时读取客户端文件。
插件安装和进程管理在本地执行。

## 数据与操作规则

| 操作 | 实际行为 |
| --- | --- |
| 删除邮件 | 默认移入 MailFlow 回收站，在“邮件 → 更多”中恢复或永久清除本地记录 |
| 原邮箱联动 | 默认关闭，按账户开启；支持的已读／收藏／归档状态同步，原邮箱回收站删除与恢复需明确确认，不提供原邮箱永久清除 |
| 保留策略 | 默认长期保留邮件、回收站保留 7 天；自动邮件清理需主动开启，并保护重要、收藏和关联未完成待办的邮件 |
| 完成待办 | 永久移除当期，周期计划继续运行，不保留完成历史 |
| 导入与重新分析 | 默认安静执行；分析任务可终止，退出后不会自动恢复旧任务 |
| 助手修改 | 确认绑定发起人和会话；历史默认保留 30 天，执行任务和确认令牌仅临时有效 |
| 备份 | 包含配置和已有本地数据、文件，自动备份默认关闭；机器人仅允许在私聊中导出备份 |

启用模型后，所需邮件内容会发送给配置的模型服务；聊天通知会将选定内容发送到对应平台。
插件在宿主环境中执行 Python 代码，请仅安装可信来源。备份和导出的机器人插件包可能包含凭据，
不要上传到公开仓库。

## 文档与反馈

| 文档 | 内容 |
| --- | --- |
| [配置与使用](docs/configuration/overview.md#简体中文) | 邮箱、语言、模型、通知、远程连接、备份和常见问题 |
| [开发与贡献](docs/development/setup.md#简体中文) | 环境、检查、问题反馈、修改提交和打包 |
| [架构说明](docs/architecture/overview.md#简体中文) | 共享数据规则、处理流程和资源管理 |
| [嵌入服务](docs/development/embedding.md#简体中文) | 在其他程序中使用核心服务 |
| [插件开发](docs/plugin-development/overview.md#简体中文) | 核心接口、可选能力和离线验证 |
| [插件仓库](https://github.com/Kingcxp/mailflow-repo/tree/dev) | 可用插件、插件配置和市场发布 |
| [更新记录](CHANGELOG.md) / [发布版本](https://github.com/Kingcxp/mailflow/releases) | 尚未发布的变化和已发布产物 |

请通过 [GitHub Issues](https://github.com/Kingcxp/mailflow/issues) 反馈可复现问题，
说明版本、系统、终端、脱敏配置和复现步骤。不要上传凭据、私人邮件、数据库或备份。
自动检查使用模拟服务；真实平台登录、文件投递和终端操作还需要实机验证。

## 许可

MailFlow 使用 [MIT 许可](LICENSE)。第三方服务、依赖和网关运行环境遵循各自的许可与使用要求。
