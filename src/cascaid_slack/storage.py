"""Ready-made storage implementations for cascaid_slack stateful helpers.

This module provides four shipping-ready implementations of the storage
protocols in ``_state``:

    PinStateStorage:
        * JsonFilePinStateStorage       -- file-backed, atomic write
        * SqlAlchemyPinStateStorage     -- one row per pin_key

    EventLogStorage:
        * JsonFileEventLogStorage       -- file-backed, atomic write
        * SqlAlchemyEventLogStorage     -- one row per channel_id

Both JSON-backed classes share an internal _JsonFileBackend helper so we
only have one atomic-write code path. Both SQLAlchemy-backed classes use
DELETE+INSERT for portability across Postgres and SQLite.

Pick the one that fits your project, or implement the Protocol yourself
for anything more exotic (Redis, DynamoDB, Notion API, whatever).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from ._state import EventLogState, PinRecord


class _JsonFileBackend:
    """Atomic-write JSON file backend shared by the JSON-storage classes.

    Loads on every read (cheap at our scale -- dozens of records, not millions)
    and writes via tmp-file + os.replace so a crash mid-write can't corrupt
    the file. A threading.Lock serialises writes within a single process;
    multi-process safety needs the SQLAlchemy backends instead.
    """

    def __init__(self, path: str | os.PathLike):
        """Initialize backend at ``path``. Parent dirs created on first save."""
        self._path = Path(path)
        self._lock = threading.Lock()

    def read(self) -> dict[str, dict[str, Any]]:
        """Return the entire file contents as a dict, or {} if absent."""
        if not self._path.exists():
            return {}
        with self._path.open("r", encoding="utf-8") as f:
            data: dict[str, dict[str, Any]] = json.load(f)
            return data

    def write(self, data: dict[str, dict[str, Any]]) -> None:
        """Atomically replace the file with ``data`` serialised as JSON."""
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(self._path)

    def mutate(self, mutator) -> None:
        """Read-mutate-write under the lock so concurrent threads can't tear writes."""
        with self._lock:
            data = self.read()
            mutator(data)
            self.write(data)


class JsonFilePinStateStorage:
    """File-backed PinStateStorage for tiny scripts and one-off reports.

    State lives in a single JSON file at ``path``. NOT safe for concurrent
    processes -- use SqlAlchemyPinStateStorage for anything that runs from
    multiple workers. Within a single process, writes are thread-safe.
    """

    def __init__(self, path: str | os.PathLike):
        """Initialize storage at the given JSON file path."""
        self._backend = _JsonFileBackend(path)

    def load_pin(self, pin_key: str) -> Optional[PinRecord]:
        """Read the record for ``pin_key`` from the JSON file."""
        row = self._backend.read().get(pin_key)
        if row is None:
            return None
        return PinRecord(
            pin_key=pin_key,
            channel_id=row["channel_id"],
            message_ts=row["message_ts"],
            last_text_hash=row.get("last_text_hash"),
        )

    def save_pin(self, record: PinRecord) -> None:
        """Persist ``record``, overwriting any prior entry under the same key."""

        def _set(data: dict[str, dict[str, Any]]) -> None:
            data[record.pin_key] = {
                "channel_id": record.channel_id,
                "message_ts": record.message_ts,
                "last_text_hash": record.last_text_hash,
            }

        self._backend.mutate(_set)

    def delete_pin(self, pin_key: str) -> None:
        """Drop the entry for ``pin_key`` if present."""
        self._backend.mutate(lambda data: data.pop(pin_key, None))


class JsonFileEventLogStorage:
    """File-backed EventLogStorage for tiny scripts and one-off reports.

    State lives in a single JSON file (separate from the pins file so the
    two domains don't collide). Same atomicity + threading caveats as
    JsonFilePinStateStorage.
    """

    def __init__(self, path: str | os.PathLike):
        """Initialize storage at the given JSON file path."""
        self._backend = _JsonFileBackend(path)

    def load_event_log(self, channel_id: str) -> Optional[EventLogState]:
        """Read the row for ``channel_id`` or return None."""
        row = self._backend.read().get(channel_id)
        if row is None:
            return None
        return EventLogState(
            channel_id=channel_id,
            last_header_date=row.get("last_header_date"),
            last_header_ts=row.get("last_header_ts"),
        )

    def save_event_log(self, state: EventLogState) -> None:
        """Persist ``state``, overwriting any prior row for the same channel_id."""

        def _set(data: dict[str, dict[str, Any]]) -> None:
            data[state.channel_id] = {
                "last_header_date": state.last_header_date,
                "last_header_ts": state.last_header_ts,
            }

        self._backend.mutate(_set)


class SqlAlchemyPinStateStorage:
    """PinStateStorage backed by a single SQLAlchemy table.

    Schema the consumer should run as a migration::

        CREATE TABLE slack_pinned_messages (
            pin_key         VARCHAR(64)  PRIMARY KEY,
            channel_id      VARCHAR(64)  NOT NULL,
            message_ts      VARCHAR(64)  NOT NULL,
            last_text_hash  VARCHAR(64),
            last_updated_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
        );

    Works on Postgres and SQLite via DELETE+INSERT (no dialect-specific
    UPSERT). If your DB supports MERGE / ON CONFLICT and you want that,
    subclass and override save_pin.
    """

    def __init__(self, engine: Any, table_name: str = "slack_pinned_messages"):
        """Wrap a SQLAlchemy Engine. ``table_name`` lets you avoid collisions."""
        from sqlalchemy import text  # lazy: keeps sqlalchemy as an optional dep

        self._engine = engine
        self._table = table_name
        self._text = text

    def load_pin(self, pin_key: str) -> Optional[PinRecord]:
        """Read the row for ``pin_key`` or return None."""
        # nosec: pin_key is parameterised; table_name comes from constructor (trusted).
        sql = self._text(
            f"SELECT pin_key, channel_id, message_ts, last_text_hash "  # nosec B608
            f"FROM {self._table} WHERE pin_key = :pin_key"
        )
        with self._engine.connect() as conn:
            row = conn.execute(sql, {"pin_key": pin_key}).mappings().first()
        if row is None:
            return None
        return PinRecord(
            pin_key=row["pin_key"],
            channel_id=row["channel_id"],
            message_ts=row["message_ts"],
            last_text_hash=row.get("last_text_hash"),
        )

    def save_pin(self, record: PinRecord) -> None:
        """DELETE+INSERT for portability across Postgres + SQLite."""
        del_sql = self._text(
            f"DELETE FROM {self._table} WHERE pin_key = :pin_key"  # nosec B608
        )
        ins_sql = self._text(
            f"INSERT INTO {self._table} "  # nosec B608
            f"(pin_key, channel_id, message_ts, last_text_hash) "
            f"VALUES (:pin_key, :channel_id, :message_ts, :hash)"
        )
        with self._engine.begin() as conn:
            conn.execute(del_sql, {"pin_key": record.pin_key})
            conn.execute(
                ins_sql,
                {
                    "pin_key": record.pin_key,
                    "channel_id": record.channel_id,
                    "message_ts": record.message_ts,
                    "hash": record.last_text_hash,
                },
            )

    def delete_pin(self, pin_key: str) -> None:
        """Drop the row for ``pin_key``."""
        sql = self._text(
            f"DELETE FROM {self._table} WHERE pin_key = :pin_key"  # nosec B608
        )
        with self._engine.begin() as conn:
            conn.execute(sql, {"pin_key": pin_key})


class SqlAlchemyEventLogStorage:
    """EventLogStorage backed by a single SQLAlchemy table.

    Schema the consumer should run as a migration::

        CREATE TABLE slack_event_date_headers (
            channel_id        VARCHAR(64) PRIMARY KEY,
            last_header_date  VARCHAR(10),  -- ISO YYYY-MM-DD
            last_header_ts    VARCHAR(64),
            last_updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """

    def __init__(self, engine: Any, table_name: str = "slack_event_date_headers"):
        """Wrap a SQLAlchemy Engine. ``table_name`` lets you avoid collisions."""
        from sqlalchemy import text  # lazy: keeps sqlalchemy as an optional dep

        self._engine = engine
        self._table = table_name
        self._text = text

    def load_event_log(self, channel_id: str) -> Optional[EventLogState]:
        """Read the row for ``channel_id`` or return None."""
        # nosec: channel_id is parameterised; table_name comes from constructor.
        sql = self._text(
            f"SELECT channel_id, last_header_date, last_header_ts "  # nosec B608
            f"FROM {self._table} WHERE channel_id = :channel_id"
        )
        with self._engine.connect() as conn:
            row = conn.execute(sql, {"channel_id": channel_id}).mappings().first()
        if row is None:
            return None
        return EventLogState(
            channel_id=row["channel_id"],
            last_header_date=row.get("last_header_date"),
            last_header_ts=row.get("last_header_ts"),
        )

    def save_event_log(self, state: EventLogState) -> None:
        """DELETE+INSERT so it works on Postgres + SQLite without dialect upsert."""
        del_sql = self._text(
            f"DELETE FROM {self._table} WHERE channel_id = :channel_id"  # nosec B608
        )
        ins_sql = self._text(
            f"INSERT INTO {self._table} "  # nosec B608
            f"(channel_id, last_header_date, last_header_ts) "
            f"VALUES (:channel_id, :last_header_date, :last_header_ts)"
        )
        with self._engine.begin() as conn:
            conn.execute(del_sql, {"channel_id": state.channel_id})
            conn.execute(
                ins_sql,
                {
                    "channel_id": state.channel_id,
                    "last_header_date": state.last_header_date,
                    "last_header_ts": state.last_header_ts,
                },
            )
