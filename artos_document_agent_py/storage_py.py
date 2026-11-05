"""Persist immutable source snapshots, draft revisions, and review decisions."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .domain_py import Draft, Record, Sections, Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_versions (
    study TEXT NOT NULL,
    document TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (study, document, version)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    study TEXT NOT NULL,
    label TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    study TEXT NOT NULL,
    section TEXT NOT NULL,
    number INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (study, section, number),
    UNIQUE (study, request_id)
);
CREATE TABLE IF NOT EXISTS reviews (
    study TEXT NOT NULL,
    section TEXT NOT NULL,
    revision INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'request_changes')),
    reviewer TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (study, section, revision),
    FOREIGN KEY (study, section, revision) REFERENCES revisions(study, section, number)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    study TEXT NOT NULL,
    event TEXT NOT NULL,
    created_at TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_by_study ON audit(study, id);
"""


class Missing(LookupError):
    """A requested snapshot, section, or revision does not exist."""


class Conflict(RuntimeError):
    """The operation conflicts with the current state of the study."""


class Review(Record):
    decision: Literal["approve", "request_changes"]
    reviewer: str
    note: str
    created_at: str


class Revision(Record):
    study: str
    section: Sections
    number: int
    snapshot_id: str
    request_id: str
    created_at: str
    draft: Draft
    review: Review | None = None


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(path, timeout=5)) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            # Reserve the writer lock before reading the current revision.
            # Competing requests then compare against the committed state.
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    @staticmethod
    def log(db: sqlite3.Connection, study: str, event: str, **details) -> None:
        db.execute(
            "INSERT INTO audit(study, event, created_at, details) VALUES (?, ?, ?, ?)",
            (study, event, timestamp(), json.dumps(details, sort_keys=True)),
        )

    def ingest(self, snapshot: Snapshot) -> Snapshot:
        with self.transaction(write=True) as db:
            exists = db.execute("SELECT 1 FROM snapshots WHERE id = ?", (snapshot.id,)).fetchone()
            if exists:
                return snapshot
            for source in snapshot.sources:
                identity = (snapshot.study, source.document_id, source.version)
                saved = db.execute(
                    "SELECT digest FROM source_versions WHERE study=? AND document=? AND version=?",
                    identity,
                ).fetchone()
                if saved and saved["digest"] != source.sha256:
                    raise Conflict(
                        f"{source.document_id} {source.version} already has different content. "
                        "Give the amended source a new version."
                    )
                if not saved:
                    db.execute(
                        "INSERT INTO source_versions VALUES (?, ?, ?, ?)",
                        (*identity, source.sha256),
                    )
            db.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?)",
                (snapshot.id, snapshot.study, snapshot.label, snapshot.model_dump_json()),
            )
            self.log(
                db,
                snapshot.study,
                "snapshot_ingested",
                snapshot_id=snapshot.id,
                label=snapshot.label,
            )
            return snapshot

    def snapshots(self) -> list[dict]:
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, study, label FROM snapshots ORDER BY study, label, id"
                )
            ]

    def snapshot(self, snapshot_id: str) -> Snapshot:
        with self.transaction() as db:
            row = db.execute("SELECT payload FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            raise Missing("Source snapshot not found.")
        return Snapshot.model_validate_json(row["payload"])

    @staticmethod
    def hydrate(db: sqlite3.Connection, row: sqlite3.Row) -> Revision:
        values = dict(row)
        review = db.execute(
            "SELECT decision, reviewer, note, created_at FROM reviews "
            "WHERE study=? AND section=? AND revision=?",
            (row["study"], row["section"], row["number"]),
        ).fetchone()
        values["draft"] = Draft.model_validate_json(values.pop("payload"))
        return Revision(**values, review=dict(review) if review else None)

    @classmethod
    def current_in(cls, db: sqlite3.Connection, study: str, section: str) -> Revision | None:
        row = db.execute(
            "SELECT * FROM revisions WHERE study=? AND section=? ORDER BY number DESC LIMIT 1",
            (study, section),
        ).fetchone()
        return cls.hydrate(db, row) if row else None

    def current(self, study: str, section: str) -> Revision | None:
        with self.transaction() as db:
            return self.current_in(db, study, section)

    def current_sections(self, study: str, sections: tuple[str, ...]) -> dict[str, Revision | None]:
        # Read all sections from one database snapshot for a coherent export.
        with self.transaction() as db:
            return {section: self.current_in(db, study, section) for section in sections}

    @classmethod
    def request_in(
        cls,
        db: sqlite3.Connection,
        snapshot: Snapshot,
        section: str,
        expected: int,
        request_id: str,
    ) -> Revision | None:
        row = db.execute(
            "SELECT * FROM revisions WHERE study=? AND request_id=?", (snapshot.study, request_id)
        ).fetchone()
        if row and (row["section"], row["snapshot_id"], row["number"] - 1) != (
            section,
            snapshot.id,
            expected,
        ):
            raise Conflict("The request ID has already been used for a different draft.")
        return cls.hydrate(db, row) if row else None

    def check_draft_request(
        self, snapshot: Snapshot, section: str, expected: int, request_id: str
    ) -> Revision | None:
        with self.transaction() as db:
            if replay := self.request_in(db, snapshot, section, expected, request_id):
                return replay
            self.require_current(db, snapshot.study, section, expected)
        return None

    @classmethod
    def require_current(
        cls, db: sqlite3.Connection, study: str, section: str, expected: int
    ) -> None:
        current = cls.current_in(db, study, section)
        actual = current.number if current else 0
        if actual != expected:
            raise Conflict(f"Section is now at revision {actual}. Reload before regenerating.")

    def save_draft(
        self, snapshot: Snapshot, section: str, expected: int, request_id: str, draft: Draft
    ) -> Revision:
        with self.transaction(write=True) as db:
            if replay := self.request_in(db, snapshot, section, expected, request_id):
                return replay
            self.require_current(db, snapshot.study, section, expected)
            created = timestamp()
            db.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.study,
                    section,
                    expected + 1,
                    snapshot.id,
                    request_id,
                    created,
                    draft.model_dump_json(),
                ),
            )
            self.log(
                db,
                snapshot.study,
                "draft_created",
                section=section,
                revision=expected + 1,
                snapshot_id=snapshot.id,
                blockers=len(draft.findings),
                generator=draft.generator,
            )
            return Revision(
                study=snapshot.study,
                section=section,
                number=expected + 1,
                snapshot_id=snapshot.id,
                request_id=request_id,
                created_at=created,
                draft=draft,
            )

    def review(
        self,
        study: str,
        section: str,
        number: int,
        decision: Literal["approve", "request_changes"],
        reviewer: str,
        note: str,
    ) -> Revision:
        with self.transaction(write=True) as db:
            current = self.current_in(db, study, section)
            if not current:
                raise Missing("No draft to review.")
            if current.number != number:
                raise Conflict(
                    f"Section is now at revision {current.number}. Reload before reviewing."
                )
            if current.review:
                if (current.review.decision, current.review.reviewer, current.review.note) == (
                    decision,
                    reviewer,
                    note,
                ):
                    return current
                raise Conflict("This revision already has a review decision.")
            if decision == "approve" and current.draft.findings:
                raise Conflict("Resolve the QC findings and regenerate before approving.")
            review = Review(decision=decision, reviewer=reviewer, note=note, created_at=timestamp())
            db.execute(
                "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?)",
                (study, section, number, decision, reviewer, note, review.created_at),
            )
            self.log(
                db,
                study,
                "review_recorded",
                section=section,
                revision=number,
                decision=decision,
                reviewer=reviewer,
            )
            return current.model_copy(update={"review": review})

    def history(self, study: str, section: str) -> list[Revision]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM revisions WHERE study=? AND section=? ORDER BY number DESC LIMIT 30",
                (study, section),
            ).fetchall()
            return [self.hydrate(db, row) for row in rows]

    def audit(self, study: str) -> list[dict]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id, event, created_at, details FROM audit WHERE study=? ORDER BY id DESC LIMIT 100",
                (study,),
            ).fetchall()
            return [dict(row) | {"details": json.loads(row["details"])} for row in rows]
