"""SQLite is the source of truth for reports, receiver receipts, and reminders."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    key TEXT PRIMARY KEY,
    job TEXT NOT NULL,
    slot TEXT NOT NULL,
    payload TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    UNIQUE (job, slot)
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(job, delivered, slot);
CREATE TABLE IF NOT EXISTS reminders (
    key TEXT PRIMARY KEY,
    due_at TEXT NOT NULL,
    text TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reminder_due ON reminders(delivered, due_at);
"""
INBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""


def utc_text(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


@contextmanager
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    # Each operation owns its connection, including when called from asyncio.to_thread.
    # Commit/rollback happens before close. SQLite coordinates concurrent processes.
    with closing(sqlite3.connect(path, timeout=5)) as database, database:
        database.row_factory = sqlite3.Row
        yield database


@dataclass(frozen=True)
class ReportRecord:
    key: str
    job: str
    slot: str
    payload: str
    delivered: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ReportRecord":
        return cls(row["key"], row["job"], row["slot"], row["payload"], bool(row["delivered"]))


@dataclass(frozen=True)
class Reminder:
    key: str
    due_at: str
    text: str


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with connection(path) as database:
            database.executescript(SCHEMA)

    def get(self, job: str, slot: str) -> ReportRecord | None:
        with connection(self.path) as database:
            row = database.execute(
                "SELECT * FROM outbox WHERE job = ? AND slot = ?", (job, slot)
            ).fetchone()
        return ReportRecord.from_row(row) if row else None

    def stage(self, record: ReportRecord) -> ReportRecord:
        # The report and its delivery key enter the outbox in one transaction.
        # Concurrent runs may both draft; only the first payload is authoritative.
        with connection(self.path) as database:
            database.execute(
                "INSERT INTO outbox(key, job, slot, payload) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(job, slot) DO NOTHING",
                (record.key, record.job, record.slot, record.payload),
            )
            row = database.execute(
                "SELECT * FROM outbox WHERE job = ? AND slot = ?", (record.job, record.slot)
            ).fetchone()
            return ReportRecord.from_row(row)

    def pending(self, job: str, limit: int = 20) -> list[ReportRecord]:
        with connection(self.path) as database:
            rows = database.execute(
                "SELECT * FROM outbox WHERE job = ? AND delivered = 0 ORDER BY slot LIMIT ?",
                (job, limit),
            ).fetchall()
        return [ReportRecord.from_row(row) for row in rows]

    def acknowledge(self, key: str) -> None:
        with connection(self.path) as database:
            database.execute("UPDATE outbox SET delivered = 1 WHERE key = ?", (key,))

    def add_reminder(self, reminder: Reminder) -> None:
        with connection(self.path) as database:
            database.execute(
                "INSERT INTO reminders(key, due_at, text) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (reminder.key, reminder.due_at, reminder.text),
            )
            row = database.execute(
                "SELECT due_at, text FROM reminders WHERE key = ?", (reminder.key,)
            ).fetchone()
            if tuple(row) != (reminder.due_at, reminder.text):
                raise ValueError("this reminder ID already has different content")

    def due(self, now: datetime, limit: int = 20) -> list[Reminder]:
        with connection(self.path) as database:
            rows = database.execute(
                "SELECT key, due_at, text FROM reminders "
                "WHERE delivered = 0 AND due_at <= ? ORDER BY due_at, key LIMIT ?",
                (utc_text(now), limit),
            ).fetchall()
        return [Reminder(**dict(row)) for row in rows]

    def acknowledge_reminder(self, key: str) -> None:
        with connection(self.path) as database:
            database.execute("UPDATE reminders SET delivered = 1 WHERE key = ?", (key,))


class Inbox:
    """Simulate a receiver on a separate database with an idempotent write API."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with connection(path) as database:
            database.executescript(INBOX_SCHEMA)

    def deliver(self, key: str, payload: str) -> bool:
        with connection(self.path) as database:
            inserted = database.execute(
                "INSERT INTO inbox(key, payload, received_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (key, payload, utc_text(datetime.now(UTC))),
            ).rowcount
            if not inserted:
                original = database.execute(
                    "SELECT payload FROM inbox WHERE key = ?", (key,)
                ).fetchone()
                if original["payload"] != payload:
                    raise ValueError("delivery key was reused with different content")
            return bool(inserted)

    def recent(self, limit: int = 20) -> list[dict]:
        with connection(self.path) as database:
            rows = database.execute(
                "SELECT * FROM inbox ORDER BY received_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
