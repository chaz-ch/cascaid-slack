"""Ready-made PinStateStorage implementations for common cases.

Pick the one that fits your project, or implement the Protocol yourself for
anything more exotic (Redis, DynamoDB, Notion API, whatever).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from ._state import PinRecord


class JsonFilePinStateStorage:
    """File-backed PinStateStorage for tiny scripts and one-off reports.

    State lives in a single JSON file at ``path``. Loads on every call (cheap
    for the expected dozen-pins-max scale) and writes atomically via tmp+rename
    so a crash mid-write can't corrupt the file.

    NOT safe for concurrent processes -- use SqlAlchemyPinStateStorage for
    anything that runs from multiple workers. Within a single process, a
    threading.Lock serializes writes.
    """

    def __init__(self, path: str | os.PathLike):
        """Initialize storage with the given JSON file path."""
        self._path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        with self._path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        # Atomic write: tmp file in the same dir, then rename. rename is
        # atomic on POSIX, and "same filesystem" is guaranteed since the
        # tmp file is a sibling.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(self._path)

    def load_pin(self, pin_key: str) -> Optional[PinRecord]:
        """Read the record for ``pin_key`` from the JSON file."""
        with self._lock:
            data = self._read()
        row = data.get(pin_key)
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
        with self._lock:
            data = self._read()
            data[record.pin_key] = {
                "channel_id": record.channel_id,
                "message_ts": record.message_ts,
                "last_text_hash": record.last_text_hash,
            }
            self._write(data)

    def delete_pin(self, pin_key: str) -> None:
        """Drop the entry for ``pin_key`` if present."""
        with self._lock:
            data = self._read()
            data.pop(pin_key, None)
            self._write(data)


class SqlAlchemyPinStateStorage:
    """PinStateStorage backed by a single SQLAlchemy table.

    The table schema is intentionally minimal -- we don't track create/update
    timestamps because the consumer's own audit log usually covers that. If
    you want them, add columns in your migration; this class will leave them
    alone.

    Schema (DDL the consumer should run as a migration):

        CREATE TABLE slack_pinned_messages (
            pin_key         VARCHAR(64)  PRIMARY KEY,
            channel_id      VARCHAR(64)  NOT NULL,
            message_ts      VARCHAR(64)  NOT NULL,
            last_text_hash  VARCHAR(64),
            last_updated_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
        );

    Works on Postgres and SQLite via the DELETE+INSERT idiom (no dialect-
    specific UPSERT). If your DB supports MERGE / ON CONFLICT and you want
    that, subclass and override save_pin.
    """

    def __init__(self, engine: Any, table_name: str = "slack_pinned_messages"):
        """Wrap a SQLAlchemy Engine. ``table_name`` lets you avoid collisions."""
        from sqlalchemy import text  # imported lazily so the dep stays optional

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
