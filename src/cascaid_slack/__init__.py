"""cascaid-slack: shared Slack notification patterns built on slack_sdk.

Public API:

    # Sending text + files
    from cascaid_slack import NotificationService

    # Rolling pinned dashboard
    from cascaid_slack import PinKey, upsert_pinned_message

    # Storage protocols + ready-made implementations
    from cascaid_slack import PinRecord, PinStateStorage
    from cascaid_slack.storage import (
        JsonFilePinStateStorage,
        SqlAlchemyPinStateStorage,
    )

Anything else (the underscore-prefixed modules, internal helpers) is private
and may break without warning. If you need access to it, file a ticket so we
can promote it to the public API properly.
"""

from ._state import EventLogState, EventLogStorage, PinRecord, PinStateStorage
from .notifier import NotificationService
from .pins import PinKey, upsert_pinned_message

__all__ = [
    "EventLogState",
    "EventLogStorage",
    "NotificationService",
    "PinKey",
    "PinRecord",
    "PinStateStorage",
    "upsert_pinned_message",
]

__version__ = "0.1.0"
