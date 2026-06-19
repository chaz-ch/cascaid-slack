# cascaid-slack

Internal Slack notification patterns shared across Cascaid Python projects.

This package extracts the homegrown Slack code that was duplicated (and
drifting) between `automatic-charting` and `minimal_reporting`. It builds on
the official [`slack_sdk`](https://github.com/slackapi/python-slack-sdk) for
transport (rate limits, file uploads, response parsing) and adds higher-level
patterns we kept reinventing:

- **`NotificationService`** — post text + upload files via Bot Token
- **`upsert_pinned_message`** — single rolling pinned message per "report kind",
  with hash-skip and self-heal on Slack-side deletion. Avoids channel flood.
- **`send_slack_event`** — date-grouped event log (one date header per day,
  events under it as threaded or numbered replies)
- **`cleanup`** — bulk-delete tooling with JSONL archive, file-bytes download,
  dual-token support for cross-bot delete, pattern-based incremental sweeps

## Status

**v0.1 — extraction in progress.** API may change as we migrate the two consumer
projects. Once both are pinned to v1.0.0 we'll keep semver compatibility.

## Install

Local editable install (current setup — both consumer projects are checked out
on the same machine):

```bash
# from a consumer project's directory:
uv add --editable ../cascaid-slack
```

## Quick start

```python
from cascaid_slack import NotificationService, PinKey, upsert_pinned_message

# Plain text + file uploads
notifier = NotificationService()  # reads SLACK_BOT_TOKEN + SLACK_CHANNEL_ID from env
notifier.send_slack("Job completed successfully")
notifier.send_slack_file(content="row1\nrow2\n", filename="results.tsv")

# Rolling pinned dashboard -- updates an existing message instead of posting new ones
my_pins = PinKey.namespace("weekly_metrics")
upsert_pinned_message(
    notifier,
    my_storage,                 # implements the PinStateStorage Protocol
    pin_key=my_pins("revenue"),
    text="```\nThis week: $12,345\n```",
)
```

## Storage protocol

`upsert_pinned_message` needs to remember "for pin_key X, the Slack ts is Y in
channel Z, and last text hash was H." Implement the `PinStateStorage` Protocol
however you like — Postgres, SQLite, Redis, even a JSON file for tiny scripts.
The shape:

```python
class PinStateStorage(Protocol):
    def load_pin(self, pin_key: str) -> PinRecord | None: ...
    def save_pin(self, record: PinRecord) -> None: ...
    def delete_pin(self, pin_key: str) -> None: ...
```

Two ready-made implementations ship with the lib:
- `SqlAlchemyPinStateStorage(engine)` — for projects using SQLAlchemy
- `JsonFilePinStateStorage(path)` — for tiny scripts / one-offs

## Env vars

| Var | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` | xoxb- token, required for all sends |
| `SLACK_CHANNEL_ID` | Default destination channel |
| `SLACK_PINS_CHANNEL_ID` | (Optional) Route pins to a different channel than other messages |
| `SLACK_USER_TOKEN` | (Optional) xoxp- token for cleanup tooling cross-bot delete |
