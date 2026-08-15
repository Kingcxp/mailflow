# Replies

Replies go through an explicit two-step confirmation so a bot/TUI/CLI cannot
send mail by accident.

## State machine

`ReplyDraft.state`: `draft` → `prepared` → `sent`, or `cancelled` from
`draft`/`prepared`.

| Operation   | Effect                                                              |
| ----------- | ------------------------------------------------------------------- |
| `create_reply(mail_id)` | New draft for the mail's sender; body pre-filled with the suggested reply when the analysis has one. |
| `edit_draft(id, subject, body)` | Updates the draft; editing invalidates any prepared token (back to `draft`). |
| `prepare_reply(id)` | Issues a short-lived token (10 minutes) and moves the draft to `prepared`. |
| `confirm_reply(id, token)` | Validates token + expiry + state; **persists `sent` before sending**, then calls the matching source instance for the draft's account. |
| `cancel_reply(id)` | Marks the draft cancelled (sent drafts cannot be cancelled). |

## Safety properties

- **Token gate**: confirm without prepare, with a wrong token, or with an
  expired token raises `PermissionError`.
- **No double send**: the `sent` state (with the token consumed) is written
  to storage *before* the provider send, so a crash between send and save
  cannot lead to a re-send — the token is gone.
- **Failed send recovery**: if the provider raises, the draft reverts to
  `draft` **without a token**; the user must prepare again.
- **Source binding**: confirmation calls only the source instance of the
  draft's account (`sources[account_id].send_reply(...)`).
- **Editing after send** is rejected.

The fake source in `mailflow-testkit` records `sent_replies` so end-to-end
tests can assert the exact send call.
