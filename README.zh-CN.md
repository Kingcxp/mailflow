# MailFlow

[English](README.md)

MailFlow 将多个邮件账户汇入同一个收件箱，提供本地存储、可选的模型分析、
待办提醒、确认后发送的回复和聊天通知。可通过终端界面、命令行或远程服务使用。

## 开始使用

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/) 和 Git。

```bash
git clone https://github.com/Kingcxp/mailflow.git
cd mailflow
uv sync --locked --all-packages --group dev
cp configs/example.toml configs/local.toml
uv run mailflow tui -c configs/local.toml
```

启动后在界面中添加账户和模型，也可以直接编辑复制的 TOML 文件。密钥建议通过
环境变量提供，`configs/local.toml` 不会被 Git 跟踪。模型并非必需，未配置时仍可使用规则处理。

## 已有功能

| 范围 | 功能 |
|---|---|
| 邮件 | 多账户、历史邮件浏览、正文／附件名／图片识别文本搜索、手动优先级和重新分析 |
| 批量操作 | 点选、Shift 范围选择、Ctrl+A 选择可见结果、清理过期邮件、撤销上一次删除 |
| 待办 | 邮件事项、自定义待办、研讨会审核、提醒和摘要 |
| 回复 | 继续编辑已保存草稿，准备并确认后发送，失败后保留内容供重试 |
| 通知 | 配置聊天平台实例和接收目标，导出 NoneBot 或 AstrBot 插件 |
| 插件 | 安装、启用或禁用邮件源、模型后端、处理器、通知器、存储和网关 |
| 语言 | 内置中文和英文，可加载额外的 JSON 语言包 |

删除邮件会移入回收站。默认保留邮件 30 天、回收站内容 7 天，在配置时区的
凌晨 04:00 清理。自动更新默认关闭。

智能指令提出的修改需要确认。智能查询使用邮件节选，长邮件中未进入节选的内容可能漏检。
远程终端支持邮件、待办和回复；智能指令、对话纠正及本地插件和进程管理目前仅在本地模式提供。

## 其他入口

```bash
uv run mailflow shell -c configs/local.toml
uv run mailflow command "help" -c configs/local.toml
uv run mailflow serve -c configs/local.toml
uv run mailflow tui --remote http://127.0.0.1:8800 -c configs/local.toml
```

远程使用前需配置服务端凭据。命令及参数以 `help` 输出为准。
账户、模型、图片文字识别、网关和远程连接设置见[配置说明](docs/configuration/overview.md)。

## 文档入口

| 用途 | 文档 |
|---|---|
| 配置和使用 | [配置说明](docs/configuration/overview.md) |
| 开发、测试和构建 | [开发说明](docs/development/setup.md) |
| 数据规则和处理流程 | [架构说明](docs/architecture/overview.md) |
| 将服务嵌入其他程序 | [服务嵌入](docs/development/embedding.md) |
| 编写插件 | [插件接口](docs/plugin-development/overview.md) |
| 查找或发布插件 | [插件仓库](https://github.com/Kingcxp/mailflow-repo) |
| 版本变化 | [更新记录](CHANGELOG.md) |

开发命令统一从 `make help` 查看。核心不依赖界面和具体服务商：`packages/` 放核心与宿主，
`plugins/` 放内置插件，`configs/` 和 `translations/` 放配置与语言包示例。

许可：MIT。
