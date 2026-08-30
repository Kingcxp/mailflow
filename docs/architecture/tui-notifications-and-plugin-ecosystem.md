# TUI — Notifications tab, plugin ecosystem & UX roadmap

This document is the approved architecture plan for the next phase of
MailFlow. It records the decisions confirmed with the maintainer, the target
design for each subsystem, and the phased implementation plan. Items marked
`planned:` do not exist yet — they are the target state this plan moves
towards. The plan supersedes the rework notes in `bot-login.md` (which
describes the current gateway auto-provisioning flow that this plan folds
into the Notifications tab).

## Goals

The TUI is the terminal-facing surface of MailFlow. It should be:

1. **Fully localized** — a one-click language switch in the Settings tab
   (already exists: `service.set_language` + `language.changed` event);
   every UI string follows the active language, except the original mail
   body, which is never translated.
2. **Plugin-driven** — every extension point is a plugin installed from the
   marketplace (`mailflow-repo`): mail sources, mail processors, LLM
   backends, LLM enhancers, notifiers, storage backends and bot exporters.
   New plugin categories are added as the ecosystem grows; the scaffold
   wizard and documentation make authoring cheap.
3. **Operable in one place** — the Notifications tab manages *all*
   notifiers: add/remove/enable/disable, urgency thresholds, connection
   tests with live status, and auto-connect on startup. Gateway
   auto-deployment (NapCat, WeChaty, OpenWeChat) remains reachable from here.

## Confirmed decisions

| # | Area | Decision |
|---|------|----------|
| 1 | Scope | Write this architecture plan first, then implement in phases (each phase committed and verified separately). |
| 2 | Bots tab | Rename to **Notifications**; manage *all* notifier instances, not just IM providers. |
| 3 | Multi-account | Keep **one instance = one account**; document NapCat multi-account support as a later round. |
| 4 | Startup | Auto-connect every enabled notifier on TUI start; failures show "offline + reason", never block startup. |
| 5 | Categories | Redesign the plugin category system (including template & login-form support). |
| 6 | New plugins | Gmail/Outlook OAuth mail sources; DingTalk/Feishu/WeCom/Slack/Discord/ServerChan notifiers; DeepSeek/Qwen/Zhipu/Moonshot LLM backends; filter/auto-archive/rule processors. |
| 7 | Login forms | Plugins declare their form fields in **Python** (a field-description object) at registration. |
| 8 | Logs tab | Default WARNING+ERROR, grouped by source, expandable to INFO; positioned after Settings. |
| 9 | Languages | Keep en + zh-CN built-in; complete missing translations. |
| 10 | Layout | All dynamic pages (Runtime/Market/Notifications/Settings) get scrollable containers. |

---

## 1. Notifications tab (replaces "平台登录" / Bots tab)

`planned:` `mailflow_tui/notifications.py` (renamed from `bots.py`).

### Scope

The tab lists **every configured notifier** (provider, instance id, enabled,
minimum urgency, target count, live connection status). This includes:
- chat-platform notifiers backed by a gateway (NapCat/onebot, WeChaty,
  OpenWeChat, OpenClaw)
- plain delivery notifiers (console, telegram, webhook, ntfy, smtp, ...)

### Table columns

| Column | Content |
| ------ | ------- |
| name | `notifier_id` |
| provider | localized provider label (`tui.bots_provider_<id>`) |
| enabled | on/off toggle (editable in place) |
| urgency | `minimum_urgency` (`ad < info < important < urgent`) |
| targets | count of `options.targets` (chat subscriptions + manual) |
| status | live: `online` / `offline: <reason>` / `starting` / `gateway: <state>` |

### Actions

- **Add / Edit / Delete** — the existing `EntryFormScreen("notifiers")`
  flow; provider dropdown now lists *all* registered notifier providers
  (not just IM), ordered: console → QQ (onebot, napcat) → WeChat (wechaty,
  wechaty-manual, openwechat, openclaw) → other notifier plugins.
- **Enable / Disable** — toggles `notifier.enabled` in place (hot-applies
  via `reload_runtime`).
- **Test** — runs the connection probe (`_BotStatusProbe`, now generalized
  to every provider) and shows the result in the status cell.
- **Check all** — concurrent bounded probes (already implemented with
  `asyncio.Semaphore(4)`).
- **Deploy** — for gateway-backed providers, opens the guided setup
  (`GatewayGuideModal`) to auto-install/start NapCat/WeChaty/OpenWeChat and
  drive the QR login; the resulting notifier entry is saved as today.

### Live status & auto-connect

`planned:`

- On mount, the tab runs a background worker that probes every **enabled**
  notifier once (bounded concurrency, short timeouts) and fills the status
  column. Failures are shown inline as `offline: <reason>` — no modal, no
  startup blocking.
- A periodic refresh (e.g. every 30–60s) re-probes gateways only when the
  tab is visible, so the status stays current without hammering the network.
- Chat-platform gateways keep their existing supervised lifecycle
  (`GatewayManager`): instances that were running at shutdown resume on
  startup, bounded to 2 concurrent launches (the OOM guard).

### Multi-account stance

**One instance = one account** for the auto-deployable platforms. The
Notifications "Add" form always starts a fresh independent instance (own
data dir, own port). NapCat's ability to host multiple accounts in one
process is a documented follow-up; users who need it today configure it
manually (the manual `onebot` notifier id remains available).

---

## 2. Plugin category redesign

`planned:` update `ComponentKind`, `plugin_template.CATEGORIES`, the repo
`index.json`/category docs, and the TUI Market/Runtime category filters.

### Target category list

| Category | ComponentKind | Owns | Scaffold template |
| -------- | ------------- | ---- | ----------------- |
| `mail_source` | `MAIL_SOURCE` | mailbox providers (login + poll + reply) | yes |
| `processor` | `MAIL_PROCESSOR` | classify / filter / archive / rules | yes |
| `llm_backend` | `LLM_BACKEND` | chat-completions transports | yes |
| `llm_enhancer` | `LLM_ENHANCER` | bounded LLM analysis customization | yes |
| `notifier` | `NOTIFIER` | delivery channels (chat bots, IM, webhook, mail) | yes |
| `gateway` | `GATEWAY_PROVISIONER` | **new**: gateway auto-deploy (NapCat, WeChaty, OpenWeChat) | yes |
| `storage` | `STORAGE` | persistence backends | yes |
| `bot_exporter` | `BOT_EXPORTER` | export to chatbot frameworks | yes |
| `template` | — | *not a plugin category*; the scaffold wizard itself | — |

Notes:

- `gateway` is split out of `notifier` so a chat platform can ship its
  notifier and its provisioner independently (the OneBot notifier plugin
  already registers both — the category is a marketplace organization, not a
  code change to `ComponentKind.GATEWAY_PROVISIONER`).
- `template` stays a built-in capability of the TUI/`plugin_template`, not a
  category.
- Every category's plugins may additionally declare **form fields** (see
  §4) so custom login/option forms render automatically.

---

## 3. Plugin scaffold wizard

Already implemented in `mailflow_tui/scaffold.py` + `plugin_template`.
Planned refinements:

- Add templates for any newly introduced category (currently
  `mail_source/processor/llm_backend/llm_enhancer/notifier/storage/
  bot_exporter`; add `gateway`).
- The generated template includes a `form_fields` stub (empty list) so
  plugin authors can opt into custom login forms from the first scaffold.
- The wizard already supports: file-tree folder pick, "create subfolder"
  checkbox + name input, category select, plugin-id input. Keep this shape.

---

## 4. Plugin-declared login / option forms

`planned:` `mailflow/forms.py` (core), consumed by `EntryFormScreen`.

### Field model

A plugin registers, per component, an optional ordered list of form fields.
Each field is a small dataclass:

```python
@dataclass(frozen=True)
class FormField:
    id: str  # option key (landed in config options / top-level)
    label_key: str  # i18n key suffix, e.g. "token" -> tui.extras_token
    kind: str  # "string" | "password" | "number" | "list" | "select" | "bool"
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()  # for "select"
    secret: bool = False  # password rendering + never logged
    into_options: bool = True  # False -> top-level config column
    description_key: str = ""  # optional longer help
```

Registration: the plugin's `mailflow_register` may pass a
`registrar.add_form_fields(component_kind, component_id, fields)` (new
registry hook) so the TUI can render the form for that provider without the
core knowing the fields. The core stays host-agnostic: `FormField` is a pure
data type in core; only the TUI renders it.

### TUI rendering

`EntryFormScreen._extras_for` becomes data-driven: instead of the hardcoded
`_Extra` tuples per provider, it looks up the provider's registered
`FormField` list (falling back to the current hardcoded extras where no
plugin declares fields, so nothing breaks). Existing `_Extra` kinds map 1:1
(`choice` → `select`, `lines` → `list`, `password` → `password`, `number`
→ `number`).

### Test connection

Each provider may also register an optional `probe` callable
(`registrar.add_probe(kind, component_id, probe)`) used by the "Test"
button and the Notifications status probe. Built-in probes (onebot
`get_login_info`, wechaty/openwechat `/health`) become plugins' own probes.

---

## 5. Startup auto-connect (Notifications tab)

Covered in §1. The service exposes a single bounded probe entry point:

`planned:` `MailFlowService.probe_notifier(notifier_id) -> str` and
`probe_all_enabled() -> dict[notifier_id, str]`, delegating to the
registered probes. The TUI calls these from a background worker; the result
map feeds the status column.

---

## 6. Logs tab rework

`planned:` `mailflow_tui/app.py` LogsPane + `runner.py` handler.

- **Position**: move the Logs tab after Settings in the tab order.
- **Default view**: WARNING and ERROR only, grouped by logger source
  (collapsible per-source groups). INFO and DEBUG hidden by default.
- **Expandable**: a toggle switches to "show INFO too"; DEBUG stays a
  separate opt-in (file logging covers it).
- **Filter**: a search box filters lines by substring.
- The `TuiLogHandler` keeps queueing formatted lines; the pane filters on
  display. Secret redaction is unchanged (already applied at the handler).

---

## 7. Language completion

- Keep `en` + `zh-CN` built-in (`mailflow/locale/`).
- Audit every UI string path used by the TUI/CLI/commands; add any missing
  keys to both packs (parity test enforces it).
- No new built-in language this round; external packs via
  `[i18n] extra_dirs` remain the path for other languages.

---

## 8. Scrollable layout

All dynamic pages get a `ScrollableContainer` around their content so any
terminal height works:

- **Notifications** table + actions
- **Market** list + detail
- **Runtime** plugin table + adapters/accounts/LLMs sections
- **Settings** cards area (already scrollable in the sidebar layout; verify
  the card column scrolls independently)

---

## 9. New plugin backlog (mailflow-repo)

Confirmed additions, one folder per plugin under its category:

| Category | Plugins |
| -------- | ------- |
| `mail_source` | mailflow-mail-gmail (OAuth), mailflow-mail-outlook (OAuth) |
| `notifier` | mailflow-notify-dingtalk, mailflow-notify-feishu, mailflow-notify-wecom, mailflow-notify-slack, mailflow-notify-discord, mailflow-notify-serverchan |
| `llm_backend` | mailflow-llm-deepseek, mailflow-llm-qwen, mailflow-llm-zhipu, mailflow-llm-moonshot (OpenAI-compatible presets) |
| `processor` | mailflow-processor-filter (rules: sender/subject/keyword), mailflow-processor-archive (auto-archive), mailflow-processor-rules (rule templates) |

---

## 10. Phased implementation plan

Each phase is a separate commit with its own verification; the plan is
frozen for review before Phase 1 starts.

- **Phase 0 — Foundation**: scrollable containers on all dynamic pages;
  Logs tab moved after Settings with WARNING/ERROR default + source grouping
  + expandable INFO; language parity audit (fill missing keys). *Verify:*
  e2e tests + manual resize.
- **Phase 1 — Notifications tab**: rename Bots → Notifications; include all
  notifiers; enable/disable in place; urgency editing; live status via
  `probe_all_enabled` on mount + periodic refresh; keep gateway guided
  setup entry. *Verify:* e2e + manual.
- **Phase 2 — Plugin forms**: `mailflow/forms.py` + registry hook +
  `EntryFormScreen` data-driven rendering + probe registration; migrate the
  built-in providers' extras to FormField. *Verify:* unit + e2e.
- **Phase 3 — Categories & templates**: `gateway` category in repo docs;
  gateway scaffold template; update Market/Runtime category filters and the
  repo `index.json` + docs. *Verify:* docs gate + validate_plugin.
- **Phase 4 — New plugins**: author the confirmed backlog (Gmail/Outlook
  sources, 6 notifiers, 4 LLM presets, 3 processors) in `mailflow-repo`,
  each with plugin.json (en + zh-CN descriptions/readmes). *Verify:*
  `validate_plugin.py` per plugin.
- **Phase 5 — Docs**: update `tui.md`, `bot-login.md` (supersede), plugin
  docs in `mailflow-repo`, README en/zh-CN, CHANGELOG. *Verify:* docs gate.

## Risks & open items

- Renaming the tab touches e2e selectors/tests (tab id, pane class) — keep
  `tab-bots` → `tab-notifications` mapping explicit in tests.
- `gateway` as a category is a marketplace/docs reorganization; the code
  `ComponentKind.GATEWAY_PROVISIONER` already exists — no core change
  needed, only the repo index and scaffold template.
- OAuth mail sources (Gmail/Outlook) need real credential flows; they are
  the most complex backlog items and may ship after the simpler ones.
- NapCat multi-account hosting is explicitly deferred; the "one instance =
  one account" stance is documented in the Notifications help line.

## See also

- `tui.md` — current TUI structure (updated per phase)
- `bot-login.md` — gateway auto-provisioning flow (superseded by §1)
- `plugin-system.md` — plugin hooks and registry
- `../development/packaging.md` — data/ layout, gateways under data/
