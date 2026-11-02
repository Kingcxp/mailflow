# Changelog

Released changes are summarized here. Detailed implementation history is in Git.

## [Unreleased]

### Changed

- Unified smart query around general mail and task tools with live progress; removed topic-specific discovery, import and review workflows.

- Unified plugin contracts, external discovery, dependency installation and offline validation across hosts and the plugin repository.
- Aligned mail identity, source progress, attachment/OCR handling and reanalysis with account-scoped, retryable processing.
- Unified task visibility, reminders, reply confirmation, permissions and local/remote data conversion.
- Added batch mail/task actions, recoverable mail deletion, draft continuation and cached search with reconnect feedback.
- Consolidated documentation around configuration, development, service embedding and the core plugin contracts.

### Fixed

- Preserve manual mail state, valid previous analysis and stable action identities during retries and reanalysis.
- Keep failed notification deliveries retryable and avoid repeating successful destinations.
- Stop old runtime, gateway and bridge resources during reload and shutdown.
- Preserve smart-query ordering, progress and incomplete results; ground relative dates in the configured timezone.
- Isolate mail content from system instructions and discard malformed correction proposals.
- Include every mail in hourly summaries, apply the configured language and retain completed briefings during delivery retries.
- Cancel queries and progress tasks when input changes or the interface closes.

## [0.1.0]

Initial framework baseline:

- Host-independent core with configuration, pipeline, persistence, reminders and shared commands.
- CLI and Textual terminal hosts, bundled plugins and recoverable SQLite storage.
- Optional model analysis with summaries, reply drafts and timed action items.
- Plugin discovery, internationalization, workspace checks and wheel/executable tooling.
