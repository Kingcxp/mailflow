# Configuration

Copy [configs/example.toml](../../configs/example.toml) to `configs/local.toml`,
then configure accounts and models in the TUI or edit the copy. Local
configuration, databases and logs are ignored by Git. The complete field schema
is [config.py](../../packages/mailflow-core/src/mailflow/config.py); plugin
options are described by the selected plugin's form and marketplace page.

```bash
uv run mailflow config-check -c configs/local.toml
uv run mailflow tui -c configs/local.toml
uv run mailflow shell -c configs/local.toml
```

Without a configuration path, built-in defaults are used. Persisting settings
requires a writable configuration file.

## Configuration groups

| Group | Purpose |
| --- | --- |
| `[general]` | Display/summary language, timezone, processing workers, queue, retention, reminders, hourly summaries, OCR and updates |
| `[logging]` | Console, rotating file and optional JSONL output |
| `[plugins]` | Enabled allowlist, disabled blocklist and marketplace repositories |
| `[[accounts]]` | Unique account ID, source provider, address and source options |
| `[[llms]]` | Named backend, endpoint, model, credentials and request options |
| `[[processors]]` | Processing order, model binding, fallback and failure policy |
| `[[notifiers]]` | Notification provider, targets and urgency threshold |
| `[server]` | Bind address, port and authentication |
| `[storage]` | Storage provider and database path |
| `[i18n]` | Additional JSON language-pack directories |

`provider` names a registered component, such as `imap` or `sqlite`, rather than
its installable Python distribution. Keep account IDs stable: they are part of
mail identity. For models, list order determines the primary and fallback
chain. Moving or removing a model updates those bindings; no usable model means
rule processing remains available.

## Defaults and optional features

| Setting | Default behavior |
| --- | --- |
| Language / timezone | English / UTC; the example file uses Asia/Shanghai |
| Mail / trash retention | Mail kept indefinitely (`mail_retention_days = 0`); trash kept 7 days; enabled cleanup runs at 04:00 local time |
| Workers / queue | 2 / 500 |
| Reminder scan | Every 60 seconds; early reminder 2 days before at 08:00, plus the due-day reminder |
| Storage | SQLite at `data/mailflow.db` |
| Server | `127.0.0.1:8800`; credentials must be configured before serving |
| Automatic updates, hourly summaries, image OCR | Disabled |

OCR additionally needs `rapidocr-onnxruntime` installed in the application's
Python environment. Set `general.ocr_images = true` to extract searchable text
from supported mail images. Stored OCR text remains readable when OCR is off.
`general.summary_language` overrides the display language for generated analysis;
when empty, it follows the selected language.

Plugin installations and enable/disable changes can require a restart; follow
the operation's result message. Runtime replacement stops the previous account,
processor, notifier and gateway tasks before starting new ones.

Account setup offers new mail only, recent history or all history. History imports
and reanalysis do not send notifications unless requested. Notification kinds and
channels are independent; tasks can mute or snooze reminders. The daily digest has
its own optional hour/minute settings, falling back to existing reminder settings.

## Credentials and saving

Use `api_key_env`, `password_env`, or a whole-string `${ENV_VAR}` value where
supported. An unset referenced variable fails configuration loading;
`prefix-${VAR}-suffix` stays literal. Passwords and tokens are masked in settings
and logs, and saving preserves environment-variable references.

The TUI and shared `config` command use the same settings model. In the shell,
`config list`, `config get <key>` and `config set <key> <value>` inspect or change
options; `help config` gives the current syntax. Failed saves leave the previous
configuration active and retain form input for correction.

## Remote access and chat notifications

Configure `[server]` with `username` and `password_env` (or `password`), then run:

```bash
uv run mailflow serve -c configs/local.toml
uv run mailflow tui --remote http://127.0.0.1:8800
```

The remote login screen asks for credentials. The remote client has no local
mail service; it shows the server's mail, tasks and reply state. Smart actions and
mail correction chat run on the server. Plugin installation, process management and backup/restore
run locally. Remembered remote passwords use the operating system credential store;
backups contain local configuration and data. For access beyond localhost, terminate HTTPS at your
existing proxy and restrict network access to the service.

For chat delivery, use the notifier's guided setup to select an instance,
configure targets and administrators, test delivery and complete login where
required. Separate instances have separate gateway state. Bot-framework export
is available through the TUI or `mailflow export`; see
[development](../development/setup.md#packaging-and-export).

## Languages

English (`en`) and Simplified Chinese (`zh-CN`) are built in. Use the TUI language
selector or `lang set <code>` in the shell; the selection is restored from
storage at startup. Set `[i18n].extra_dirs` to load additional data-only JSON
packs using the format shown in [translations](../../translations).
Missing external translations fall back to English. New user-visible messages
must use the translation component and provide both built-in languages.
