# Bot platform login (auto-provisioned gateways)
> **Superseded.** This page described the gateway auto-provisioning flow
> (form-driven install + QR login for NapCat / WeChaty / OpenWeChat) before
> the Notifications tab existed. That flow is now a feature of the
> Notifications tab, managed by `NotificationsPane` in
> `mailflow_tui/notifications.py`; the approved architecture plan is
> [tui-notifications-and-plugin-ecosystem.md](tui-notifications-and-plugin-ecosystem.md),
> and the current UI behavior is documented in
> [tui.md](tui.md) → "Notifications tab". The sections below are kept for
> historical reference.

How MailFlow sets up IM chat bot platforms (QQ via OneBot/NapCat, WeChat via
WeChaty pad protocol) with as little manual work as possible: the user picks
a platform in a form, and MailFlow installs, starts and configures the
gateway — including driving the QR login inside the TUI.

Status: implemented (round 1 + fixes). NapCat (via the `napcat` provisioner
in mailflow-notify-onebot) and WeChaty (via the `wechaty` provisioner in
mailflow-notify-wechaty) are auto-installed, launched and supervised by the
GatewayManager. openwechat (via the `openwechat` provisioner in
mailflow-notify-openwechat) is a third gateway: a Go bridge built on
install that logs into WeChat with a plain QR scan — no platform token.
The Bots tab's Add button opens the standard notifier form: the provider
dropdown lists `napcat` (auto-deploy, labeled "QQ (OneBot / NapCat)") as
a separate choice from the manual `onebot` notifier id, so self-hosted
users keep the endpoint form. Choosing a gateway-backed provider and
pressing Next opens the guided setup — a bordered dialog with a live
timestamped/level-tagged log pane (scrollable) and the QR login inside
the frame, buttons outside. openclaw-weixin remains manual. NapCat's
version is resolved from the latest GitHub release at install time (no
more pinned, possibly-404 URLs); download errors carry the URL and HTTP
status.

## Goals

1. **Auto-provision**: adding a platform must not require the user to
   install Node packages, download binaries or hand-edit configs.
   - NapCat (OneBot v11 HTTP): auto-download + install + launch on first
     use; scan the QR inside the TUI.
   - WeChaty gateway: auto-install the WeChaty package and the gateway
     bridge, run it as a managed child process, surface the QR in the
     TUI. With a pad-protocol token configured the pad puppet is used
     (paid service); without one the free web-protocol puppet
     (wechat4u) is tried — Tencent shut that protocol down, so it may
     never emit a QR, but some accounts still work.
   - openwechat gateway: scan-to-login with no platform token. The
     provisioner requires a Go toolchain (reports the exact apt command
     when missing), builds the bridge, and the bridge serves the QR as a
     PNG plus a hot-reload session (no re-scan on restart).
2. **Second instance**: when the platform is already installed, "Add"
     starts *another* independent instance (own data dir, own HTTP port)
     instead of reusing the first — one account per instance by default,
     because both NapCat and WeChaty gateways are single-session processes.
3. **Non-blocking**: installs and gateway startup run in workers; the TUI
   stays responsive. A failing gateway never blocks other platforms.
4. **Marketplace stays open**: onebot/wechaty are bundled by default, but
   other chat-platform plugins remain installable from the marketplace
   (a notifier plugin + a gateway provisioner plugin per platform).

## Architecture

```
TUI Bots tab (form: basics → Next → provider guide)
        │  mailflow.gateway (new core module, host-agnostic)
        ▼
GatewayProvisioner registry (new component kind or notifier extension)
        ├── onebot  → NapCat provisioner (download/install/start/QR)
        └── wechaty → WeChaty gateway provisioner (npm install/start/QR)
        ▼
managed child processes (subprocess, owned by the runtime)
        │  state persisted in storage preferences (installed, port, pid dir)
        ▼
existing notifiers (mailflow-notify-onebot / mailflow-notify-wechaty)
```

- `mailflow.gateway` is a new core module: a `GatewayProvisioner` protocol
  plus a registry of provisioners (registered by plugins, like every other
  component). Core stays host-agnostic — provisioners are plugins.
- Provisioners are declared alongside notifiers: a plugin may register both
  a `NOTIFIER` component (how MailFlow *sends* alerts) and a
  `GATEWAY_PROVISIONER` component (how the platform's bot process is
  *obtained and run*).
- The TUI drives provisioners through the service facade; the service owns
  the managed child processes and their lifecycle (start on boot, stop on
  shutdown, restart on crash with backoff).

## Managed gateway lifecycle

```
Add form (basics) → Next → provider guide
  1. detect: provisioner.is_available()  (installed? running? which port?)
  2. install (first use only): provisioner.install() in a worker
       - NapCat: on Windows download the release zip → unpack under
         <data>/gateways/napcat-<instance>/ → launch with the boot exe;
         on Linux download the official AppImage (QQ + NapCat bundled,
         only xvfb + fuse needed) into the instance dir and launch with
         `xvfb-run ./…AppImage --no-sandbox`
       - WeChaty: `npm install wechaty` + gateway bridge under
         <data>/gateways/wechaty-<instance>/ → launch with `node`
       - openwechat: `go build` the bridge under
         <data>/gateways/openwechat-<instance>/ → launch the binary
  3. start: provisioner.start(instance) → managed child process
       - per-instance state dir + HTTP port (3001, 3002, ... or a fixed
         base port + instance offset; stored in preferences)
       - stdout/stderr routed to the MailFlow log (never printed raw to
         the TUI)
  4. QR: provisioner.qr_url()/qr_image() → TUI renders it (existing
     `_ascii_qr` machinery) and polls until the session is online
  5. save: notifier config entry (http_url/gateway_url, token, targets)
     with the actual endpoint; user fills targets afterwards
```

Crash handling: the runtime's gateway supervisor restarts a dead child with
backoff (like the pipeline's retry policy). `stop()` terminates every
managed child (graceful SIGTERM, then kill). On startup, instances that
were running at shutdown are resumed automatically (the supervisor starts
them once even when the status probe reports stopped).

Chat-command wiring survives app restarts: the onebot event bridge (the
local listener NapCat's `httpClients` push message events to) lives in the
MailFlow process, not in the gateway child. While a gateway reports
`running` the supervisor calls the provisioner's `ensure_bridge` hook on
every poll cycle, so the bridge is recreated after a restart even though
`start()` is never called again — chat commands keep answering. The
WeChaty and OpenWeChat bridges forward incoming text messages to the same
local `mailflow.bot_server` endpoint (via `MAILFLOW_BOT_URL`) and send the
reply back, so all three gateway platforms have a working chat-command
path; the OpenWeChat bridge gained this forwarding (previously it only
offered `/health`, `/qr` and `/send`).

NapCat's OneBot config is per-account: once a QQ number is logged in,
`onebot11_<uin>.json` takes precedence over the default `onebot11.json`.
The provisioner therefore writes both files, and after login / bridge
(re)creation re-syncs the per-account file with the current `httpClients`
bridge URL (the QQ number is probed from `get_login_info`; a stale
per-account file — e.g. written by an older MailFlow or the NapCat WebUI —
silently dropped the bridge entry, so message events were never pushed and
chat commands went unanswered). When the re-sync repaired a stale file the
NapCat child is restarted once so the corrected config actually loads
(NapCat reads config only at startup; the QR/session is hot-reloaded so
login persists). The event bridge logs every received message and reply at
INFO level, making a broken chain diagnosable from the MailFlow log alone.

Linux host libraries: the bundled NapCat AppImage contains the full QQ NT
+ Electron runtime, which needs the usual desktop shared libraries
(`libnss3`, `libgbm`, `libasound`, GTK, ...). A minimal container without
them fails at load with a cryptic `major.node: cannot open shared object
file`. The installer checks these via `ldconfig -p` and logs the exact
`apt-get install` command as a warning when any look absent (the ldconfig
cache can lag a fresh install, so this never blocks deployment).

Self-healing installs: when the NapCat child dies with loader/preload
fingerprints (`major.node`, `cannot open shared object file`, `preload]
failed`, `Trace/breakpoint trap`) — a corrupt or incomplete environment —
the provisioner deletes the instance's data dir and reinstalls NapCat
automatically, then starts it once more. A second failure is reported as
an error (one repair attempt per start; no infinite loop). Clearing
`data/gateways` manually is no longer required to recover from a broken
deployment.

Missing payload: if the gateway's data directory was deleted while the app
was stopped (or the install was moved), `start()` raises
`GatewayNotInstalledError`; the supervisor then marks the instance `error`
and stops retrying — an endless restart loop would hide the problem. The
Notifier tab surfaces this as an explicit "installation missing — re-run
setup" hint in the status column instead of a generic unreachable.

Stale processes: `start()` reuses a running gateway only when MailFlow
itself spawned the child and it is still alive. A port occupied by an
unmanaged process — typically a stale NapCat left over after the gateway
data dir was cleared — raises a clear error instead of being silently
"reused"; reusing it would have reported the old session as logged in
while the new instance never started (the phantom "scan complete" seen
after cleanup). The cached QQ number is also dropped whenever the login
probe stops seeing a session, so logout is reflected in the UI.

## Chat commands (subscriptions & operations)

MailFlow bots answer chat commands in groups and private chats. The
prefix is configurable (`general.command_prefix`, default `/`). Only
users listed in the notifier's `admins` option (QQ number / wxid, one
per item in the bot form's list editor) may run commands; others get a
permission-denied reply.

```
/mailflow help                       - section-grouped help (multi-message)
/mailflow example                    - sample notification of every type
/mailflow subscribe                  - receive mail notifications in this chat
/mailflow unsubscribe                - stop notifications in this chat
/mailflow status                     - bot status + subscribed chat count
/mailflow mail list [--query q]      - list recent mail
/mailflow mail show <id>             - one mail with summary/feedback
/mailflow mail urgency <id> <level>  - override urgency (info/important/critical)
/mailflow feedback <id> <reason>     - teach the classifier (reject mail)
/mailflow reply create <mail_id>     - draft a reply
/mailflow reply prepare <draft_id>   - get the confirm token
/mailflow reply confirm <id> <token> - send (token-gated, no double-send)
/mailflow reply cancel <draft_id>    - drop the draft
/mailflow action add <summary> --due "YYYY-MM-DD HH:MM" [--type t] [--notes n]
/mailflow action list                - schedule entries
/mailflow action done <item_id>      - complete a schedule entry
/mailflow runtime status             - pipeline status
/mailflow lang get | set <code>      - display language
```

Every MailFlow command lives in the `mailflow` namespace so other bots in
the same group never collide: a bare `/help` or `/mail` is not executed —
the reply teaches the corrected form instead. `help`, `example` and
`status` need no admin; subscriptions require the chat context and an
admin; all other commands delegate to the same router the TUI/CLI use.

`help` and `example` return a list of message chunks. The OneBot bridge
sends them as one merged-forward message (node list); platforms without
forward support fall back to plain segments paced a few seconds apart.
Oversized chunks are cut with a localized `…(truncated)` marker instead of
being silently clipped by the platform.

Subscribing adds the chat (group id / contact id) to the notifier's
targets and persists it (`gateway.sub.<provider>.<instance>` in the
preferences store), so notifications arrive after a restart too.

Notifiers deliver mail at `minimum_urgency` or above — the default is
`info`, so nothing (ads included) is silently dropped; raise it in the
bot form to filter noise. Reminder and daily-digest log lines follow
`general.language`.

## NapCat OneBot HTTP endpoint

NapCat v4.5.3+ loads `config/onebot11.json` as its default OneBot
config. The provisioner writes this file (with the full `httpServers`
shape: name/enable/port/host) both into the instance dir and the
standard NapCat per-user config dir
(`~/.config/QQ/NapCat/config/`), so the OneBot HTTP server listens on
the instance port after login — this is what the guide's connectivity
check and the notifier talk to.

## TUI flow (reworked form)

Current: `EntryFormScreen` shows provider dropdown + all provider fields at
once; "Next" only auto-fills a detected endpoint.

Target (matches the user's request):

```
Add platform
  [Step 1 — basics]  name/id input,  [Next]
  [Step 2 — provider]  provider dropdown (onebot | wechaty | openclaw |
                       installed marketplace plugins)  [Next]
  [Step 3 — guide]  progress status: detecting → installing (first use)
                    → starting → QR (NapCat/WeChaty) → done
```

- Step 2 lists every registered `GATEWAY_PROVISIONER` plus providers that
  are notifier-only (openclaw: no gateway, manual config — same as today).
- Step 3 is a modal with a status line and a cancel button; every network
  or process operation runs in an exclusive worker. The QR step reuses
  `NapCatQrModal`'s polling loop, generalized for any provisioner.
- The provider's own option fields (token, targets) are filled *after* the
  gateway is up — the user is guided, not confronted with fields they do
  not understand yet.

## Persistence

- `data/gateways/<provider>-<instance>/` — per-instance install dir, logs,
  QR state.
- Preferences (`storage.set_preference`):
  - `gateway.<provider>.<instance>.status` — installed/starting/running/
    error
  - `gateway.<provider>.<instance>.port` — assigned HTTP port
  - `gateway.<provider>.<instance>.pid` — managed child pid (for
    stale-pid detection on boot)
- On boot, the runtime restarts instances whose status was `running` at
  shutdown (unless `gateway.<provider>.<instance>.autostart = false`).

## Security and constraints

- Gateway installs download from pinned, versioned URLs (NapCat GitHub
  releases; WeChaty via npm with a locked version). The first run records
  the resolved checksum; a mismatch aborts with a clear error.
- Managed processes run with the MailFlow user's permissions, never with
  elevated rights; no credentials are passed on the command line (env vars
  or config files with 0600 perms only).
- WeChat pad-protocol tokens are secrets: stored like every other secret
  (never logged, never written back in plaintext from `${ENV_VAR}`).
- A provisioner install failure leaves the instance dir removable; the Bots
  tab shows the failing step instead of a silent half-configuration.

## Open questions (next rounds)

- Node.js detection currently requires a system `node >= 18` (checked by
  the provisioners). Bundling a portable Node is the more robust option on
  Windows and remains open.
- NapCat download mirror for China networks (GitHub releases are slow
  there).
- Whether openclaw-weixin gets a provisioner later (out of scope for the
  first round; stays manual).
- WeChaty pad-protocol tokens are user-provided (paid service); the bridge
  reports login errors through `/qr` and `/health`.

## See also

- `tui.md` — the Bots tab today (probe-only)
- `plugin-development/notifier.md` — notifier plugins
- `../development/packaging.md` — data/ layout and `make clean` (gateways
  live under data/, so `make clean` keeps them)
