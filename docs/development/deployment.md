# Deployment

MailFlow runs identically on **Windows**, **Linux** and **macOS**: the only
prerequisite is Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). Every
command below works on all three platforms.

## 1. Install the toolchain

```bash
# Python 3.11+ — use your platform's package manager or python.org:
#   Windows: https://www.python.org/downloads/windows/  (tick "Add to PATH")
#   Linux:   sudo apt install python3.11 python3.11-venv   (Debian/Ubuntu)
#   macOS:   brew install python@3.11

# uv — the script installs it only when missing (idempotent):
curl -LsSf https://astral.sh/uv/install.sh | sh          # Linux / macOS
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
```

## 2. Get and set up the project

```bash
git clone https://github.com/Kingcxp/mailflow.git
cd mailflow
uv sync --all-packages --group dev      # one lockfile for every package
```

## 3. Configure

Copy the example and fill in your mailboxes and LLM endpoint:

```bash
cp configs/example.toml configs/local.toml
export YOUR_TOKEN=your-token            # ${VAR} placeholders expand at load
```

See [configuration/overview.md](../configuration/overview.md) for every
option (`mailflow config list` shows them interactively too).

## 4. Run

```bash
uv run mailflow run -c configs/local.toml    # headless service (sources, pipeline, reminders)
uv run mailflow tui -c configs/local.toml    # Textual terminal UI
uv run mailflow shell -c configs/local.toml  # interactive command shell
```

The TUI, the shell and the CLI `command` subcommand all talk to the *same*
configured instance (shared storage + config), so you can manage a running
service from the terminal at home and from chat on the phone — the command
surface is identical everywhere.

## 5. Install plugins

```bash
uv run mailflow plugin repo add mailflow-repo https://github.com/Kingcxp/mailflow-repo
uv run mailflow plugin market list
uv run mailflow plugin install mailflow-notify-telegram
uv run mailflow plugin install ./path/to/local/plugin      # folder or batch of folders
```

The TUI Market tab offers the same operations, including a file-tree
installer (Market → 从文件夹安装) and an export wizard (Market → Export).
`update check|now|status|auto on|off` manage versions; with
`general.auto_update = true` (default) MailFlow checks daily and applies
releases and plugin updates automatically.

## 6. Export a chatbot-framework plugin

```bash
uv run mailflow export --framework nonebot --output dist/nonebot_plugin_mailflow -c configs/local.toml
uv run mailflow export --framework astrbot --output dist/astrbot_plugin_mailflow -c configs/local.toml
# or: make bot-plugin-nonebot / make bot-plugin-astrbot
```

The generated plugin embeds the resolved config and the full chat command
surface (`mailflow ...` on NoneBot, `/mailflow ...` on AstrBot). Install it
into the bot host:

- **NoneBot2** — `pip install ./dist/nonebot_plugin_mailflow`, add
  `nonebot-plugin-mailflow` to `NONEBOT_PLUGINS` (or `load_plugin`); see the
  [NoneBot docs](https://nonebot.dev/docs/) for project setup.
- **AstrBot** — copy `dist/astrbot_plugin_mailflow/` into AstrBot's
  `plugins/` directory (or install via the dashboard); see the
  [AstrBot docs](https://docs.astrbot.app/) for plugin management.

Both plugins boot the MailFlow service with the bot and dispatch prefixed
chat messages to the shared command router; long replies and the daily
digest are paginated into separate messages.

## 7. Verify

```bash
make check        # lint + format + mypy + pyright + pytest + docs gate
make doctor -c configs/local.toml   # registrations and configuration summary
```

## Troubleshooting

- `uv` not found after the install script — reopen the terminal so the PATH
  refresh applies (or run the script with `exec $SHELL`).
- Windows Defender/App Control blocking uv or the frozen executable — add
  the project/venv folder to the allowed apps list.
- Timezone oddities — set `general.timezone` to your IANA zone; all
  reminder/digest/cleanup schedules use it.
