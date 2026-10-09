# MailFlow Agent and Contributor Guide

本文件是 MailFlow 主仓库的根级修改约定。它服务于贡献者、代码代理和后续 plan 模式；具体模块规则以源码、架构文档和测试为准。若本文件与实现不一致，先修正文档或明确变更范围，不要按猜测继续扩展。

## 1. 项目定位与边界

MailFlow 是统一多账户邮件收件箱：邮件源产生 provider-neutral 的 `MailMessage`，处理器链生成结构化分析，存储后端持久化邮件/回收站/草稿，通知器投递结果，CLI/TUI/聊天机器人复用同一个服务门面。

依赖方向必须保持向内：

```text
CLI / TUI / server / chatbot hosts
              ↓
mailflow-core: domain · config · service · pipeline · runtime · commands
              ↑
plugins/* + mailflow-bundled composition root
```

- `packages/mailflow-core` 是宿主无关核心。核心不得导入具体插件包、Typer、Textual 或具体聊天框架。
- `packages/mailflow-cli`、`packages/mailflow-tui`、`packages/mailflow-server` 是宿主/适配层；业务语义应进入 Core 的 service、pipeline、domain 或 contracts，而不是复制到宿主。
- `packages/mailflow-bundled` 是官方插件的组合根。官方插件通过静态导入注册，保证冻结构建不依赖 entry-point 元数据。
- `plugins/*` 是独立分发的具体适配器。配置引用的是注册的 component id，不是 Python 包名。
- `packages/mailflow-testkit` 提供 fake source、LLM、notifier 等测试组件。
- `../mailflow-repo` 是插件商城仓库，不属于本 workspace 的自动加载路径。它使用“类别目录/插件目录/plugin.json/源代码”的布局。

首先阅读：

- `README.md` / `README.zh-CN.md`：用户能力、命令和质量门禁。
- `docs/architecture/overview.md`：层次和公共服务边界。
- `docs/architecture/domain-and-mail.md`：领域模型、优先级、ActionItem、智能操作和去重。
- `docs/architecture/plugin-system.md`：Pluggy、注册、所有权和组件种类。
- `docs/architecture/pipeline.md`：排序、重试、超时、失败策略和重分析。
- `docs/development/tests.md`、`docs/development/quality.md`：测试分层和门禁。

## 2. 不可破坏的不变量

### 2.1 领域与优先级

- `Urgency` 只有 `ad`、`info`、`important`、`urgent` 四级；颜色和含义是跨 CLI、TUI、通知器的公开契约，不得在单个宿主中另定义一套。
- `manual_urgency` 是覆盖层。设置手动值不得改写 `auto_urgency`；清除手动值必须恢复自动分析结果。
- `effective_urgency` 才是面向用户和通知阈值的有效值。
- `MailMessage` 保留原始正文/HTML，分析结果存放在 `MailAnalysis`/`MailRecord`，不能把分析字段混入 provider-neutral 原文模型。
- 邮件身份必须使用 `normalized_message_id()` 的账户无关结果；跨账户的同一封转发邮件应去重。
- 历史浏览不得推进实时邮件源的水位，也不得从 `fetch_history` 直接 emit；用户选中的历史邮件必须通过 `service.process_mail()` 走正常去重、流水线、持久化、事件和通知路径。
- ActionItem 必须保留 `mail_id` 回链（用户创建 todo 除外），时间必须带时区；只对收件人必须完成的事项创建分析动作。
- 没有有效分析摘要时可以使用主题作为显示连续性回退，但必须标记为 fallback/失败状态，不能伪装成成功的 LLM 摘要。
- 智能操作的删除、日程和编辑都是 staged operation；模型结果本身不能直接写存储，必须经过一次用户确认。

### 2.2 回复安全

回复状态只能按 `draft → prepared → sent`，或从 `draft/prepared → cancelled` 转移。

- 发送必须经过 `prepare_reply` 产生的、未过期 token 和 `confirm_reply`。
- `confirm_reply` 必须先持久化 `sent` 并消费 token，再调用对应账户的 source；这样崩溃恢复不会重复发送。
- provider 发送失败时恢复为无 token 的 `draft`，用户必须重新 prepare。
- 编辑 draft 会使 prepared token 失效；已发送 draft 不可编辑/取消。
- 发送只能调用 draft 所属账户的 source 实例。

### 2.3 插件、注册与流水线

- Pluggy 只负责发现和注册；处理器顺序、优先级、重试、超时、failure policy、note trail 和 fallback summary 由 `mailflow.pipeline` 负责。
- 组件所有权在 `PluginRegistrar` 注册时盖章；运行时禁止“寻找第一个具备能力的插件”这类隐式选择。
- 重复 component id 必须被拒绝或按现有注册语义处理，不能静默改变配置指向。
- 一个插件的 info/register hook 失败应隔离并记录；不能让一个坏插件阻止整个服务启动。
- 禁用插件不能让启动因孤立配置项崩溃；应按现有行为跳过并警告。bundled 插件只能启用/禁用，不能按 marketplace 插件路径卸载。
- 处理器插件应优先考虑 `LLMEnhancer` 这一受限扩展点；只有确有新的流水线步骤时才实现完整 `MailProcessor`。
- processor 的执行顺序是 priority 升序；不得在插件中偷偷实现另一套排序、重试或超时。
- 每个 source 的实时故障必须隔离到账户；通知器故障不能让邮件处理失败；单个 processor 故障必须遵循 `continue`/`stop` 策略。

### 2.4 存储、配置与日志

- 删除/保留清理必须把完整记录移动到 trash；恢复返回相同记录和原始 `received_at`。
- trash 保留期按删除时间计算，不按邮件接收时间计算；首次删除时间不能被重复同步重置。
- 存储写入必须使用现有 StorageBackend 抽象；SQLite 查询参数化，并遵守其异步锁/WAL 约定。
- 配置修改必须经过整体校验后才持久化；`config set`/Settings editor 应保留 TOML 注释，并在局部路径缺失时按现有回写策略处理。
- LLM 列表顺序就是 fallback 路由：第一项是默认，后续项按顺序 fallback。不要让用户手工编辑由顺序推导的 `default`/`fallback`。
- 密钥只能通过 `${ENV_VAR}` 或 `api_key_env` 等环境变量入口提供。不得提交真实 mailbox 密码、LLM token、webhook secret 或其他凭据。
- 配置回写不得把解析后的环境变量密钥写成明文；敏感字段在 CLI、TUI、测试输出和文档示例中都必须脱敏。
- Core 不得调用 `logging.basicConfig()` 或重新配置 root logger；`propagate=False` 只允许作用于 `mailflow` 日志树。
- 配置阶段注册的 secret 必须进入 redaction filter；transport 错误在进入日志或持久化 `ProcessorNote` 前必须清洗 URL/query/token 等敏感信息。

### 2.5 国际化与用户界面

- Core command、CLI、TUI、notifier 中的用户可见文本必须通过 `service.t(...)`/`I18n.t(...)`；不要新增硬编码 UI 文案。
- `en` 是完整基线，`zh-CN` 必须保持 key parity；English pack 不得出现中文。
- 配置项新增或改名时同步维护 `config.desc.<key>` 的 en/zh-CN 描述，并按现有生成工具更新，不手工制造重复 dotted key。
- TUI 是 service 的薄客户端，不在 pane/modal 中复制领域规则、配置 schema 或持久化逻辑。
- 弹窗应有可见的 Back/Cancel 路径；Escape 可以是快捷方式，不能是唯一退出方式。

## 3. 按模块修改

### 修改 Core

1. 先确认公共服务 API、domain/contract 和事件是否已存在；宿主所需能力优先增加到 `MailFlowService`/contracts，而不是在 TUI 或 CLI 私有实现。
2. `start_service(...)` 是组合配置、日志、插件、存储、runtime、pipeline、事件的入口；生命周期必须能通过 `await service.stop()` 正常关闭。
3. 新公共 service 方法、事件、配置字段和 component kind 必须更新相关架构文档、测试和中英文用户文档。
4. Core 只能依赖其自身声明的通用依赖；具体 provider 放入 `plugins/*`。

### 修改插件

1. 插件应依赖 `mailflow-core`，使用 `mailflow.plugins` entry-point group；优先使用 `mailflow.plugin_api` 的声明式定义和现有 scaffold 生成的结构。
2. 注册 component factory、FormField、probe 时明确 component kind 和 component id；不得按包名或类型猜测能力。
3. source 必须规范化时区、身份、原始正文；notifier 只消费已计算的 `MailRecord`；LLM backend 必须合并配置并限制重试；storage 必须实现 trash/draft/preference 语义；gateway 生命周期由 `GatewayManager` 管理。
4. 错误文本不能泄露 URL query、token、密码或完整远端响应。对网络组件使用现有的边界清洗方式。
5. 插件测试应通过正常注册 hooks，使用 fake/monkeypatch transport，不调用真实 IMAP、SMTP、HTTP、LLM 或聊天平台。

### 修改 TUI/CLI/server

- 宿主只负责渲染、输入和生命周期；业务判断调用 service/public API。
- 新文案先加 locale key，再接入 `t()`；同时更新 en/zh-CN。
- TUI 异步工作放在现有 worker/lifecycle 模式中，避免阻塞事件循环；服务事件名称必须使用实际 `mailflow.*` 名称。
- 布局修改需考虑英文、中文、窄终端、按钮高度和 modal 的可退出性；不要在 UI 层复制配置字段类型判断。

### 修改配置/本地运行文件

- `configs/local.toml`、`data/`、`logs/` 和本地 token 不应提交。
- 配置 schema、默认值或迁移逻辑变更必须同步 `docs/configuration/overview.md`、配置描述和对应测试。
- 不要把 `data/mailflow.db`、插件安装产物、Nuitka `dist/` 或临时 smoke 文件纳入提交。

## 4. 插件商城 `../mailflow-repo` 协作

商城仓库的每个插件位于一个类别目录下，例如 `notifier/mailflow-notify-slack/`，至少包含 `plugin.json`、生成的 `README.md` 和可安装的 `pyproject.toml`/源代码。类别与 component kind 的对应关系见商城仓库 `docs/02-categories.md`。

涉及插件时必须明确属于以下哪种变更：

- **主仓库工作区插件**：修改 `plugins/<package>/`，同步主仓库 workspace 配置、bundled 静态注册、测试和文档。
- **商城副本**：修改 sibling checkout 中的 `<category>/<plugin-id>/` 目录，同步 `plugin.json` 与生成的 `README.md`，运行商城仓库提供的插件校验脚本；不要假设主仓库会自动加载 sibling checkout 的代码。
- **接口/契约变更**：若 Core contract、component kind、注册 API 或配置字段变化，同时检查两个仓库的插件指南、模板、现有插件和 validator；这是跨仓库变更，不得只改一个副本。

商城约定：

- `plugin.json.id` 必须等于插件目录名；`package` 必须等于 `pyproject.toml` 的 distribution name；`categories` 必须恰好一个；`source`、`readme` 和 entry point 必须可用。
- `plugin.json` 是商城详情文本的单一来源；修改后运行 `python tools/gen_plugin_readmes.py`，再运行 `python tools/gen_plugin_readmes.py --check`。
- 本地验证单个插件：在商城仓库根目录运行 `python tools/validate_plugin.py <category>/<plugin-id>`；全量验证：`python tools/validate_plugin.py --all`。
- 处理器还必须能在 validator 的 sample mail 上实际运行；source/notifier/gateway 的外部网络行为不应在 CI 中依赖真实服务。
- Marketplace 插件只能依赖已发布/当前可用的 `mailflow-core` 能力；不要依赖主仓库尚未发布的内部 API，除非本次变更明确同步发布策略。

## 5. 测试策略

测试按风险选择最小但真实的层级：

- `tests/unit/`：领域契约、配置、pipeline、service、命令、i18n、日志、表单和组件逻辑；无真实网络/外部服务。
- `tests/integration/`：官方插件和跨组件行为；在 transport boundary 使用 fake、monkeypatch 和 `tmp_path`。
- `tests/e2e/`：从 `start_service(...)` 启动完整流程，组件通过正常注册加载，并在 `finally` 中 `service.stop()`；TUI 使用 headless pilot 或实际 smoke。

新增测试必须覆盖可观察行为、边界、状态转换、优先级、失败路径或数据持久性，不要测试源代码文本、简单转发、mock echo、非空长度或单纯“不抛异常”。

修改前后至少运行直接相关测试；跨 Core/plugin/service 的行为再运行 `make check`。测试不得访问真实邮箱、SMTP、LLM、HTTP、聊天平台或提交真实凭据。

## 6. 质量与验证

开发环境：

```bash
uv sync --all-packages --group dev
```

常用验证：

```bash
uv run pytest tests/unit/<file>.py -q
uv run pytest tests/integration/<file>.py -q
uv run pytest tests/e2e/<file>.py -q
make lint
make format-check
make typecheck
make check
```

`make check` 是交付前默认代码门禁，包含 Ruff、format check、mypy strict、pyright strict 和 pytest。不要声称测试或门禁通过，除非本次实际执行并观察到结果。若只执行了聚焦测试，明确说明未执行全量门禁。

行为、公共 API、配置、事件、插件类别、用户命令变化仍应在同一变更中更新对应 `docs/`、README/CHANGELOG（按现有项目惯例）和 locale。

`docs/build-log/BUILD_LOG.md` 是历史记录，只有在确实执行了对应命令后才能追加；不能把推测结果写成已验证。

## 7. 进入 plan 模式时的工作协议

收到修改需求后，在正式写代码前按以下顺序与用户核对：

1. **范围**：列出用户明确要求、受影响包、可能涉及的 sibling marketplace 插件，以及明确不做的相邻需求。
2. **现状证据**：给出实际入口文件、调用方、公共 API、现有测试和相关架构文档；公共符号变更先查所有引用。
3. **方案**：按文件列出数据流、状态转换、兼容性/迁移、失败处理、权限/密钥风险和 i18n/doc/test 影响。
4. **验证**：为每个行为列出直接测试、集成/E2E 场景、必要的 CLI/TUI smoke，以及最终 `make check`。
5. **分步确认**：用户调整范围或方案后，重新整理受影响文件与验收标准；没有用户确认的 plan 不进入跨模块编辑。

计划必须区分“已由代码/文档确认”和“需要用户决定”的事项。以下情况需要主动提出选择：公开 API 是否允许破坏式改动、配置迁移策略、商城与主仓库版本发布顺序、是否新增 component kind、是否改变持久化语义。其余实现细节按现有模式选择最小、可维护方案。

## 8. 安全与工作区纪律

- 先读取相关源码和文档再编辑；不要凭文件名猜测 API。
- 使用现有抽象和命名；不要同时保留无迁移价值的旧路径、别名或重复实现。
- 不要覆盖、回滚或删除用户已有的无关改动；发现相关工作区改动时，先理解后协作。
- 删除数据、清空 trash、删除插件或改写配置属于破坏性操作；除非需求明确，不执行。
- 生成文件、临时数据库、截图、日志和 smoke 配置应放在临时路径并在验证后清理。
- 交付时报告：改动文件、行为结果、实际运行的验证命令、未运行的门禁和已知风险。
