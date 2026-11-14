# Development and contributing

[Home](../../README.md) · [简体中文](#简体中文) · [Architecture](../architecture/overview.md)

## Environment

Use Python 3.11+, Git and [uv](https://docs.astral.sh/uv/). Follow the
[quickstart](../../README.md#quickstart) to clone `dev`; use the matching `dev`
branch in the [plugin repository](https://github.com/Kingcxp/mailflow-repo/tree/dev)
when changing shared implementations.

```bash
uv sync --locked --all-packages --group dev
```

GNU Make is a convenience, not a runtime requirement. `make sync` runs the
command above and is the only Make target that synchronizes dependencies.
Other targets use the existing environment's Python directly; `help` and
`clean` use system Python and work before dependency installation. A missing
environment produces setup instructions rather than an automatic installation.
Use `VENV=...` to select another environment, for example
`make test VENV=.venv312`. The default is `UV_PROJECT_ENVIRONMENT` when set,
otherwise `.venv`; `PYTHON=...` selects the system Python for Make's helpers.

On Windows, the same Python commands work in PowerShell without Make.
Use fake sources and temporary databases for development; connect actual
accounts only for deliberate manual testing. Never include local credentials
or user data in a patch.

The run/export Make targets use the ignored `configs/development.toml`.
Copy the example there before using them, use `CONFIG=configs/local.toml`,
or use an explicit CLI `-c` path.
The README uses `configs/local.toml` for ordinary first use.

## Checks

Run relevant tests first, then the checks affected by the change. Shared
contract/runtime changes require the full gate. CI covers Windows and Linux on
Python 3.11 and 3.12; it uses simulated external services.

| Make target | Equivalent / purpose |
| --- | --- |
| `make test-unit` | `python -m pytest -q tests/unit` |
| `make test-integration` | `python -m pytest -q tests/integration` |
| `make test-e2e` | `python -m pytest -q tests/e2e` |
| `make test` | `python -m pytest -q` |
| `make coverage` | `python -m pytest --cov --cov-report=term-missing --cov-report=html --cov-report=xml` |
| `make validate-plugins` | `python -B -m mailflow_testkit.plugin_smoke --root plugins --source` |
| `make lint` | `python -m ruff check .` |
| `make format-check` / `make format` | `python -m ruff format --check .` / omit `--check` to apply |
| `make mypy` | `python -m mypy packages plugins tools` |
| `make pyright` | `python -m pyright --pythonpath .venv/bin/python` (Windows: `.venv/Scripts/python.exe`) |
| `make check` | Tests, plugin validation, lint, format check and both type checkers |
| `make build` | `python tools/build_all.py` |
| `make clean` | System Python runs `tools/clean.py`; removes development output, not user data |

In the equivalents above, `python` means the selected environment's interpreter
(`.venv/bin/python` or `.venv/Scripts/python.exe`), except for cleanup.
After the initial sync, `uv run --no-sync ...` is another way to use installed
tools without replacing plugins or optional dependencies. `--frozen` alone
only protects the lockfile and still allows environment synchronization.
An explicit `make sync` restores locked workspace sources; it can replace a
manually installed same-name plugin and remove extra packages.

Use `make help` for all targets. Coverage output goes to `build/reports` and
wheels to `dist/wheels`; both are ignored. Data-modifying `clean-*` targets are
not quality checks and must not run against someone else's configuration.
For a build using only cached build dependencies, pass `--offline` explicitly
to `tools/build_all.py`; it is not the default of `make build`.

## Test boundaries

| Directory | What belongs here |
| --- | --- |
| `tests/unit` | Isolated domain, component and transport-contract behavior |
| `tests/integration` | Real component composition, SQLite, ASGI and headless terminal interactions |
| `tests/e2e` | Actual supported CLI processes with temporary configuration and storage |

Test observable behavior, such as selection surviving refresh, failed-target
retry or confirmation preventing duplicate sends. Reuse fakes from
`mailflow-testkit`. Do not assert private call order, incidental wording, widget
existence or a fixed number of tests. A chain of fake components is not a real
end-to-end test. Automated checks must not log into mailboxes/models, install
marketplace packages or launch real chat runtimes.

## Contributing

Report bugs through [Issues](https://github.com/Kingcxp/mailflow/issues), including
the revision, OS/terminal, minimal reproduction, expected/actual behavior and
sanitized diagnostics. For a larger feature, describe the user workflow before
starting implementation. Never attach mail, passwords, tokens or backups.
For a security issue, use private reporting when available; do not post secrets
or exploit details in a public issue.

Target pull requests at `dev`. Keep one coherent change per commit, using the
existing convention, for example `fix(tui): retain attachment selections on failure`.
Explain the user-visible change, compatibility impact and checks run. Use the
shared service/command logic for local, remote and bot entry points. Update
English and Chinese messages and relevant existing documentation together.
Preserve existing configuration, SQLite data and mail identities.

If a plugin is shared with `mailflow-repo`, update and validate both copies.
Marketplace README content is generated from `plugin.json`; see the
[plugin contribution guide](https://github.com/Kingcxp/mailflow-repo/blob/dev/docs/00-getting-started.md).
Do not commit virtual environments, reports, caches, private configurations or
generated bot packages. Documentation-only changes need link, command/example
and generated-content checks rather than new business tests.

## Packaging and releases

Workspace packages have individual `pyproject.toml` files.
[tools/build_all.py](../../tools/build_all.py) defines the wheel build list;
keep it aligned with workspace members and bundled registrations. Core does not
depend on a specific UI or provider.

```bash
make build
make exe-standalone
# Smoke-test the standalone executable before building onefile.
make exe-onefile
```

Executable builds use Nuitka. Compiler downloads require `--download-tools`;
Windows/Linux release jobs explicitly enable that option. Frozen builds use
explicit bundled imports: extra Python plugins must be included at build time.

Export a configured instance without starting its mail runtime:

```bash
uv run --no-sync mailflow export --framework nonebot --output dist/nonebot_plugin_mailflow -c configs/local.toml
uv run --no-sync mailflow export --framework astrbot --output dist/astrbot_plugin_mailflow -c configs/local.toml
```

The generated framework host owns startup/shutdown and permissions. Install it
in that framework's environment. Configure administrators in the exported
MailFlow notifier binding as described by its generated README; framework-wide
administrator flags are not imported automatically. Its copied
configuration can contain credentials; keep the output private.

The [release workflow](../../.github/workflows/release.yml) runs on `v*` tags,
checks the tag against the core version and extracts notes from
[CHANGELOG.md](../../CHANGELOG.md). It builds wheels and Windows/Linux executables
and publishes a prerelease. A green CI run does not establish real platform
login or delivery compatibility; record those manual checks separately in the
pull request or release notes, not as generated report files in the repository.

## 简体中文

[返回首页](../../README.zh-CN.md) · [English](#development-and-contributing)

使用 Python 3.11+、Git 和 uv，在 `dev` 分支运行
`uv sync --locked --all-packages --group dev`。修改共享插件时，插件仓库也使用 `dev`。
Make 是便捷入口，Windows 可直接用指定环境的 Python 执行上表命令，不必安装 Make。
只有 `make sync` 同步依赖；日常命令直接使用现有环境，帮助和清理只需要系统 Python。
环境缺失时提示安装，不会擅自创建或重装。默认使用 `.venv`，也支持
`UV_PROJECT_ENVIRONMENT` 或 `make test VENV=.venv312`；`PYTHON=...` 指定辅助脚本的系统 Python。
初次同步后，普通 uv 命令使用 `uv run --no-sync ...`，避免覆盖本地插件和可选依赖。
`--frozen` 只锁定文件，不禁止同步；主动执行 `make sync` 会恢复锁定来源，可能替换同名插件或移除额外包。
日常测试使用假邮件源、临时配置和数据库，不接触真实用户数据。

README 使用 `configs/local.toml`；Make 的运行和导出目标使用
`configs/development.toml`，需要事先从示例复制，也可传入 `CONFIG=configs/local.toml`，
或改用带明确 `-c` 的命令。

先验证相关行为，再运行受影响的静态检查；共享接口与运行时修改执行完整检查。
CI 覆盖 Windows/Linux 和 Python 3.11/3.12。测试分为组件隔离、真实组件组合、
实际命令行进程三层，分别放在 unit、integration、e2e。
复用测试工具包，不连接真实邮箱、模型、市场或聊天进程。
测试应证明用户行为，不锁定私有调用顺序、文案、控件存在或测试数量。

问题反馈包含版本、系统、终端、复现步骤、预期与实际结果及脱敏日志；不要附带邮件、
密码或备份。较大需求先描述用户流程。安全问题优先私下报告，不公开凭据或利用细节。
修改提交到 `dev`，每个提交保持独立，沿用 `fix(tui): ...` 等格式。
说明行为变化、兼容性和验证结果，中英文提示及已有说明同步更新。
本地、远程和机器人复用业务逻辑，不建立相互偏离的实现。

共享插件同时更新两个仓库，插件 README 修改元数据后统一生成。
不要提交环境、缓存、报告、个人配置或含密钥的导出包。仅改文档时验证链接、命令、
配置示例和生成结果，不为文档额外堆业务测试。

构建命令见上表；冻结程序先构建并试运行 standalone，再构建 onefile。
额外插件需要纳入构建，工具下载需显式开启。NoneBot/AstrBot 导出不启动邮件运行时，
导出内容可能包含凭据；管理员按导出说明配置匹配的 MailFlow 通知器，框架管理员不会自动导入。
发布工作流由 `v*` 标签触发，检查核心版本并读取更新记录，
生成安装产物后发布预览版本。真实登录、文件投递和终端效果仍需单独实测。

`make clean` 保留用户数据；其他 `clean-*` 目标可能修改数据，不能当作开发检查运行。
