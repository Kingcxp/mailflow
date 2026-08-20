# Mail source adapter

A mail source merges one provider into the MailFlow stream.

## Contract

```python
class MailSource(Protocol):
    async def run(self, emit: MailEmitter, stop_event: asyncio.Event) -> None:
        """Stream normalized mails into `emit` until `stop_event` is set."""

    async def send_reply(self, mail_id: str, draft: ReplyDraft) -> None:
        """Send a confirmed reply for this provider."""

    async def close(self) -> None:
        """Release provider resources."""
```

`MailEmitter = Callable[[MailMessage], Awaitable[None]]` — put each
normalized message into the bounded runtime queue.

## Registration

```python
def build_source(account: MailAccountConfig) -> MySource:
    # account.options carries provider-specific settings
    return MySource(account)


registrar.add_source("my-provider", build_source)
```

Configuration references the adapter by component id:

```toml
[[accounts]]
account_id = "main"
provider = "my-provider"
email = "me@example.com"
[accounts.options]   # provider-specific settings
```

## Normalization contract

- `MailMessage.message_id` — provider-stable id; `provider_message_id` for
  identity (`normalized_message_id()` falls back to a content digest).
- `date` and `received_at` — timezone-aware UTC datetimes.
- Keep the **original** `body_text` and `body_html`; analysis is separate.
- Set `account_id` (the emitting account) and `provider` (your plugin id).

## Reply contract

`send_reply(mail_id, draft)` is called by the service only after the
two-step confirmation. The draft carries the recipient, subject, body and
the account. Record calls if you are a fake (the testkit source appends to
`sent_replies` for E2E assertions).

## Optional: browsable history

Implement `fetch_history` and your source also satisfies
`HistoryCapableSource`, which unlocks the TUI **Mailboxes** history browser
(and `service.fetch_history`) for accounts using it:

```python
class MySource:
    async def fetch_history(self, limit: int = 50, offset: int = 0) -> list[MailMessage]:
        """Up to `limit` already-received messages, newest first, skipping `offset`."""
```

Rules that make the capability safe:

- **Never emit** from `fetch_history` — return the messages and let the caller
  decide. The host calls `service.process_mail(mail)` for the ones the user
  picked, which reuses the normal pipeline path (dedup, persistence,
  `mailflow.mail.processed`, notifiers).
- **Newest first**, and `offset` pages further back, so the UI can append.
- **Do not disturb the live stream.** Keep whatever incremental state `run()`
  uses untouched: the IMAP source pages over UIDs for history while leaving
  its poll water-mark alone.
- Normalize exactly like `run()` does — the same message must produce the same
  `normalized_message_id()`, otherwise browsing would create duplicates of
  mail you already stored.

The capability is discovered with `isinstance` (both protocols are
`runtime_checkable`), so omitting it is fine: the browser reports that the
source cannot list history instead of failing.

## Failure isolation

Raise from `run()` to mark the account `error` (captured in the snapshot);
other accounts and the pipeline keep running. Do not call `emit` after
`stop_event` is set.
