# Notifier

A notifier delivers an **already-computed** analysis to a channel. It never
classifies mail itself.

## Contract

```python
class Notifier(Protocol):
    async def notify(self, record: MailRecord) -> None: ...
```

`record` carries the full mail, `analysis`, and `effective_urgency`
(manual override wins). The runtime applies the `minimum_urgency` threshold
before calling you — a notifier only sees records at or above its threshold.

## Registration

```python
def build_notifier(config: NotifierConfig) -> MyNotifier:
    return MyNotifier(config.options)


registrar.add_notifier("my-channel", build_notifier)
```

```toml
[[notifiers]]
notifier_id = "my-channel"
provider = "my-channel"
enabled = true
minimum_urgency = "important"     # ad | info | important | urgent
[notifiers.options]               # channel settings
```

## Rules for authors

- Keep `notify` fast and failure-tolerant: a notifier exception is logged
  by the runtime and never fails mail processing.
- Use the computed `record.summary` / `record.analysis`; do not re-analyze.
- Never log credentials.
