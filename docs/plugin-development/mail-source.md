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

## Failure isolation

Raise from `run()` to mark the account `error` (captured in the snapshot);
other accounts and the pipeline keep running. Do not call `emit` after
`stop_event` is set.
