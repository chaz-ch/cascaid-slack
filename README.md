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

**v0.2.0** -- pins + events + cleanup tooling all shipped. API may still change
as we migrate the two consumer projects. Once both are pinned to v1.0.0 we'll
keep semver compatibility.

## Install

Local editable install (current setup — both consumer projects are checked out
on the same machine):

```bash
# from a consumer project's directory:
uv add --editable ../cascaid-slack
```

## Quick start

```python
from cascaid_slack import (
    NotificationService,
    PinKey, upsert_pinned_message,
    send_slack_event,
)
from cascaid_slack.storage import (
    JsonFilePinStateStorage,
    JsonFileEventLogStorage,
)

# Plain text + file uploads
notifier = NotificationService()  # reads SLACK_BOT_TOKEN + SLACK_CHANNEL_ID from env
notifier.send_slack("Job completed successfully")
notifier.send_slack_file(content="row1\nrow2\n", filename="results.tsv")

# Rolling pinned dashboard -- updates an existing message instead of posting new ones
pin_storage = JsonFilePinStateStorage("./pins_state.json")
my_pins = PinKey.namespace("weekly_metrics")
upsert_pinned_message(
    notifier, pin_storage,
    pin_key=my_pins("revenue"),
    text="```\nThis week: $12,345\n```",
)

# Date-grouped event log -- channel reads like a journal with date headers
event_storage = JsonFileEventLogStorage("./events_state.json")
send_slack_event(notifier, event_storage, text="Job completed: 71 navigator tasks")
# -> Posts "*--- Tuesday, June 16, 2026 ---*" once per day, then your event under it

# Bulk cleanup -- delete pre-migration flood while protecting pinned messages
from cascaid_slack.cleanup import (
    CleanupRules, run_cleanup, write_audit_csv, build_delete_client,
)
from slack_sdk import WebClient
import os, re

rules = CleanupRules(
    autobot_user_id="U-MY-BOT",
    redundant_patterns={
        "old_stats": re.compile(r"Pending Navigator Tasks", re.I),
    },
)
result = run_cleanup(
    read_client=WebClient(token=os.environ["SLACK_BOT_TOKEN"]),
    delete_client=build_delete_client(
        os.environ.get("SLACK_USER_TOKEN") or os.environ["SLACK_BOT_TOKEN"]
    ),
    channel=os.environ["SLACK_CHANNEL_ID"],
    rules=rules,
    db_pinned_ts=load_my_pinned_ts(),   # set[str], from wherever you keep it
    execute=False,                       # dry-run; set True to actually delete
)
write_audit_csv(result.verdicts, "cleanup_preview.csv")
print(result.action_summary)  # Counter({'KEEP_HUMAN': 12, 'DELETE': 5043, ...})
```

## Storage protocols

The stateful helpers (rolling pins, date-grouped event logs) need to remember
a little bit between calls. Two Protocols cover the two patterns:

```python
class PinStateStorage(Protocol):
    def load_pin(self, pin_key: str) -> PinRecord | None: ...
    def save_pin(self, record: PinRecord) -> None: ...
    def delete_pin(self, pin_key: str) -> None: ...

class EventLogStorage(Protocol):
    def load_event_log(self, channel_id: str) -> EventLogState | None: ...
    def save_event_log(self, state: EventLogState) -> None: ...
```

Four ready-made implementations ship with the lib (in `cascaid_slack.storage`):

| Class | When to use |
|---|---|
| `JsonFilePinStateStorage(path)` | Tiny scripts / one-offs. Single-process only. |
| `JsonFileEventLogStorage(path)` | Same, for the event-log state. |
| `SqlAlchemyPinStateStorage(engine)` | Production projects already using SQLAlchemy. |
| `SqlAlchemyEventLogStorage(engine)` | Same, for the event-log state. |

Implement the Protocol yourself for anything more exotic (Redis, DynamoDB, etc).

## Env vars

| Var | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` | xoxb- token, required for all sends |
| `SLACK_CHANNEL_ID` | Default destination channel |
| `SLACK_PINS_CHANNEL_ID` | (Optional) Route pins to a different channel than other messages |
| `SLACK_USER_TOKEN` | (Optional) xoxp- token for cleanup tooling cross-bot delete |
