"""Authoritative local SQLite store: previews, execution journals, audit.

This is the only source of truth for preview state, execution journals, and the
append-only audit log. It owns schema initialization/validation, transaction
boundaries, and bounded retention. It performs no HTTP, environment, or terminal
access, and never imports :mod:`fluentcrm`.

Preview state machine::

                         execute claim
                    +--------------------> executed
                    |
    active ---------+------ cancel ------> cancelled
                    |
                    +------ expiry ------> expired

``active`` is the only consumable preview state. ``executed``/``cancelled``/
``expired`` are terminal tombstones with a required terminal timestamp. The
``active`` -> ``executed`` transition and creation of the linked ``claimed``
execution journal happen in one transaction.

Execution-journal state machine::

    claimed ---- fresh-state validation ----> dispatching
       |                                         |
       +---- validation rejection ------------> rejected
                                                 |
                                                 +--> succeeded
                                                 +--> recovered
                                                 +--> rejected
                                                 +--> verification_failed
                                                 +--> outcome_unknown

``claimed`` and ``dispatching`` are nonterminal. A repeated execute may resume
only ``claimed``. A stranded ``dispatching`` is reconciled to terminal
``outcome_unknown`` and never re-writes remotely.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import RuntimeIdentity
from .models import CommandError, ErrorCategory

SCHEMA_VERSION = 1
PREVIEW_TTL = timedelta(minutes=10)
RETENTION = timedelta(days=90)
MAINTENANCE_BATCH = 100
BUSY_TIMEOUT_MS = 5000

PREVIEW_STATES = frozenset({"active", "executed", "cancelled", "expired"})
JOURNAL_STATES = frozenset(
    {"claimed", "dispatching", "succeeded", "recovered", "rejected",
     "verification_failed", "outcome_unknown"}
)
_JOURNAL_TERMINAL = frozenset(
    {"succeeded", "recovered", "rejected", "verification_failed", "outcome_unknown"}
)

# Terminal journal outcome -> the audit event that records it.
_FINALIZE_EVENTS: dict[str, str] = {
    "succeeded": "execution_succeeded",
    "recovered": "execution_recovered",
    "rejected": "execution_rejected",
    "verification_failed": "execution_verification_failed",
    "outcome_unknown": "execution_outcome_unknown",
}

# Every event type appended to the append-only audit log.
EVENT_TYPES = frozenset({
    "preview_created", "preview_noop", "preview_rejected", "preview_expired",
    "execution_claimed", "execution_dispatching", "execution_cancelled",
    "execution_rejected", "execution_succeeded", "execution_recovered",
    "execution_verification_failed", "execution_outcome_unknown",
    "reconciliation_present", "reconciliation_absent", "reconciliation_unknown",
})

# Expected schema shape, checked verbatim on every reopen. Column order matches
# the CREATE TABLE statements; any drift is a fail-closed local_state_invalid.
_EXPECTED_TABLES: dict[str, tuple[str, ...]] = {
    "preview": (
        "request_id", "operation", "contact_id", "email_masked", "email_hmac",
        "contact_status", "tag_id", "tag_slug", "pre_write_tag_ids", "state",
        "created_at", "expires_at", "terminal_at",
    ),
    "execution_journal": (
        "request_id", "state", "mutation_attempted", "created_at",
        "dispatched_at", "terminal_at", "outcome",
    ),
    "audit": (
        "seq", "ts", "event_type", "request_id", "actor", "package_version",
        "git_commit", "uv_lock_sha256", "allowlist_sha256", "operation",
        "contact_id", "email_masked", "email_hmac", "tag_id", "tag_slug",
        "pre_write_tag_ids", "http_status", "error_category", "verification_ts",
    ),
}
_EXPECTED_INDEXES = frozenset(
    {"idx_preview_state_expiry", "idx_journal_state_terminal", "idx_audit_request_seq"}
)

_DDL = """
CREATE TABLE preview (
    request_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    contact_id INTEGER NOT NULL,
    email_masked TEXT NOT NULL,
    email_hmac TEXT NOT NULL,
    contact_status TEXT NOT NULL,
    tag_id INTEGER NOT NULL,
    tag_slug TEXT NOT NULL,
    pre_write_tag_ids TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'executed', 'cancelled', 'expired')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    terminal_at TEXT,
    CHECK ((state = 'active') = (terminal_at IS NULL))
);
CREATE INDEX idx_preview_state_expiry ON preview(state, expires_at);

CREATE TABLE execution_journal (
    request_id TEXT PRIMARY KEY REFERENCES preview(request_id),
    state TEXT NOT NULL CHECK (state IN
        ('claimed', 'dispatching', 'succeeded', 'recovered', 'rejected',
         'verification_failed', 'outcome_unknown')),
    mutation_attempted INTEGER NOT NULL DEFAULT 0 CHECK (mutation_attempted IN (0, 1)),
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    terminal_at TEXT,
    outcome TEXT
);
CREATE INDEX idx_journal_state_terminal ON execution_journal(state, terminal_at);

CREATE TABLE audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    request_id TEXT,
    actor TEXT NOT NULL,
    package_version TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    uv_lock_sha256 TEXT NOT NULL,
    allowlist_sha256 TEXT NOT NULL,
    operation TEXT,
    contact_id INTEGER,
    email_masked TEXT,
    email_hmac TEXT,
    tag_id INTEGER,
    tag_slug TEXT,
    pre_write_tag_ids TEXT,
    http_status INTEGER,
    error_category TEXT,
    verification_ts TEXT
);
CREATE INDEX idx_audit_request_seq ON audit(request_id, seq);
"""

@dataclass(frozen=True)
class AuditFields:
    """Event-specific audit columns. Identity/actor are held by the store."""

    request_id: str | None = None
    operation: str | None = None
    contact_id: int | None = None
    email_masked: str | None = None
    email_hmac: str | None = None
    tag_id: int | None = None
    tag_slug: str | None = None
    pre_write_tag_ids: tuple[int, ...] | None = None
    http_status: int | None = None
    error_category: str | None = None
    verification_ts: str | None = None


@dataclass(frozen=True)
class PreviewRecord:
    """One row of the ``preview`` table."""

    request_id: str
    operation: str
    contact_id: int
    email_masked: str
    email_hmac: str
    contact_status: str
    tag_id: int
    tag_slug: str
    pre_write_tag_ids: tuple[int, ...]
    state: str
    created_at: str
    expires_at: str
    terminal_at: str | None


@dataclass(frozen=True)
class JournalRecord:
    """One row of the ``execution_journal`` table."""

    request_id: str
    state: str
    mutation_attempted: bool
    created_at: str
    dispatched_at: str | None
    terminal_at: str | None
    outcome: str | None


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of an execute claim: a fresh claim or a resumed ``claimed`` one."""

    preview: PreviewRecord
    journal: JournalRecord
    resumed: bool


def _invalid() -> CommandError:
    return CommandError(ErrorCategory.LOCAL_STATE_INVALID)


def _default_db_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "iranfluent-tag-operator" / "state.sqlite3"


class StateStore:
    """Transactional SQLite store. All writes go through short explicit txns.

    The connection runs in autocommit mode (``isolation_level=None``) so that
    ``BEGIN IMMEDIATE`` / ``BEGIN EXCLUSIVE`` bracket every write deterministically.
    Identity columns echoed into every audit row are fixed for the process and
    supplied once at construction.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        identity: RuntimeIdentity,
        actor: str,
    ) -> None:
        self._conn = connection
        self._identity = identity
        self._actor = actor

    @classmethod
    def connect(
        cls,
        *,
        identity: RuntimeIdentity,
        actor: str,
        path: Path | None = None,
    ) -> StateStore:
        """Open (creating if needed) the private state DB and validate its schema."""

        db_path = path if path is not None else _default_db_path()
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(db_path.parent, 0o700)
            existed = db_path.exists()
            conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000)
            if not existed:
                os.chmod(db_path, 0o600)
        except OSError:
            raise _invalid()
        conn.isolation_level = None
        store = cls(conn, identity=identity, actor=actor)
        store._apply_pragmas()
        store._validate_or_init_schema()
        return store

    def close(self) -> None:
        self._conn.close()

    def _apply_pragmas(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute("PRAGMA journal_mode = DELETE")
            cur.execute("PRAGMA synchronous = FULL")
            cur.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        except sqlite3.OperationalError:
            raise _invalid()
        finally:
            cur.close()

    def _user_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def _app_table_names(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {r[0] for r in rows}

    def _validate_or_init_schema(self) -> None:
        version = self._user_version()
        if version == SCHEMA_VERSION:
            self._validate_schema()
            return
        if version != 0:
            # No migration path exists; any other version is unsupported.
            raise _invalid()
        self._init_schema()

    def _init_schema(self) -> None:
        # Serialize first-init across processes: whoever wins the EXCLUSIVE lock
        # creates the schema; a loser that blocked on the lock re-reads the
        # version and validates instead of re-creating.
        try:
            self._conn.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            raise CommandError(ErrorCategory.LOCAL_STATE_BUSY)
        try:
            version = self._user_version()
            if version == SCHEMA_VERSION:
                self._conn.execute("ROLLBACK")
                self._validate_schema()
                return
            if version != 0 or self._app_table_names():
                # A partially initialized or unexpected database fails closed.
                raise _invalid()
            # ``executescript`` implicitly commits the EXCLUSIVE transaction
            # before running, so the version stamp rides inside the same script
            # (its trailing statement) and no manual COMMIT follows.
            self._conn.executescript(
                f"{_DDL}\nPRAGMA user_version = {SCHEMA_VERSION};\n"
            )
        except sqlite3.DatabaseError:
            self._rollback_quiet()
            raise _invalid()
        except BaseException:
            self._rollback_quiet()
            raise

    def _rollback_quiet(self) -> None:
        # After ``executescript`` commits, no transaction remains to roll back;
        # only issue ROLLBACK when one is actually open.
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    def _validate_schema(self) -> None:
        integrity = self._conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise _invalid()
        if self._app_table_names() != set(_EXPECTED_TABLES):
            raise _invalid()
        for table, columns in _EXPECTED_TABLES.items():
            info = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            if tuple(row[1] for row in info) != columns:
                raise _invalid()
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if {r[0] for r in rows} != _EXPECTED_INDEXES:
            raise _invalid()

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _dump_ids(ids: Sequence[int] | None) -> str | None:
        return None if ids is None else json.dumps([int(i) for i in ids])

    @staticmethod
    def _load_ids(text: str) -> tuple[int, ...]:
        return tuple(int(i) for i in json.loads(text))

    def _row_to_preview(self, row: tuple) -> PreviewRecord:
        return PreviewRecord(
            request_id=row[0], operation=row[1], contact_id=row[2],
            email_masked=row[3], email_hmac=row[4], contact_status=row[5],
            tag_id=row[6], tag_slug=row[7], pre_write_tag_ids=self._load_ids(row[8]),
            state=row[9], created_at=row[10], expires_at=row[11], terminal_at=row[12],
        )

    def _row_to_journal(self, row: tuple) -> JournalRecord:
        return JournalRecord(
            request_id=row[0], state=row[1], mutation_attempted=bool(row[2]),
            created_at=row[3], dispatched_at=row[4], terminal_at=row[5], outcome=row[6],
        )

    def probe_write_lock(self) -> None:
        """Fail closed before any remote work if the DB cannot be write-locked."""

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            raise CommandError(ErrorCategory.LOCAL_STATE_BUSY)

    def _insert_audit(
        self, cursor: sqlite3.Cursor, event_type: str, fields: AuditFields,
        now: datetime,
    ) -> None:
        if event_type not in EVENT_TYPES:
            raise _invalid()
        cursor.execute(
            "INSERT INTO audit (ts, event_type, request_id, actor, package_version, "
            "git_commit, uv_lock_sha256, allowlist_sha256, operation, contact_id, "
            "email_masked, email_hmac, tag_id, tag_slug, pre_write_tag_ids, "
            "http_status, error_category, verification_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._iso(now), event_type, fields.request_id, self._actor,
                self._identity.package_version, self._identity.git_commit,
                self._identity.uv_lock_sha256, self._identity.allowlist_sha256,
                fields.operation, fields.contact_id, fields.email_masked,
                fields.email_hmac, fields.tag_id, fields.tag_slug,
                self._dump_ids(fields.pre_write_tag_ids), fields.http_status,
                fields.error_category, fields.verification_ts,
            ),
        )

    def append_event(self, event_type: str, fields: AuditFields, now: datetime) -> None:
        """Append one standalone audit row in its own short transaction."""

        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            self._insert_audit(cur, event_type, fields, now)
            cur.execute("COMMIT")
        except sqlite3.OperationalError:
            self._rollback_quiet()
            raise CommandError(ErrorCategory.LOCAL_STATE_BUSY)
        except BaseException:
            self._rollback_quiet()
            raise
        finally:
            cur.close()

    def record_noop(self, fields: AuditFields, now: datetime) -> None:
        """Audit an already-attached preview no-op. Creates no consumable state."""

        self.append_event("preview_noop", fields, now)

    def record_rejection(
        self, event_type: str, fields: AuditFields, now: datetime,
    ) -> None:
        """Audit a rejected preview/execute attempt (no state transition)."""

        self.append_event(event_type, fields, now)

    def get_preview(self, request_id: str) -> PreviewRecord | None:
        row = self._conn.execute(
            "SELECT request_id, operation, contact_id, email_masked, email_hmac, "
            "contact_status, tag_id, tag_slug, pre_write_tag_ids, state, "
            "created_at, expires_at, terminal_at FROM preview WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return self._row_to_preview(row) if row is not None else None

    def get_journal(self, request_id: str) -> JournalRecord | None:
        row = self._conn.execute(
            "SELECT request_id, state, mutation_attempted, created_at, "
            "dispatched_at, terminal_at, outcome FROM execution_journal "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return self._row_to_journal(row) if row is not None else None

    def create_preview(
        self,
        *,
        request_id: str,
        operation: str,
        contact_id: int,
        email_masked: str,
        email_hmac: str,
        contact_status: str,
        tag_id: int,
        tag_slug: str,
        pre_write_tag_ids: Sequence[int],
        now: datetime,
    ) -> PreviewRecord:
        """Insert one active preview and its ``preview_created`` audit atomically."""

        created_at = self._iso(now)
        expires_at = self._iso(now + PREVIEW_TTL)
        ids = tuple(int(i) for i in pre_write_tag_ids)
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "INSERT INTO preview (request_id, operation, contact_id, "
                "email_masked, email_hmac, contact_status, tag_id, tag_slug, "
                "pre_write_tag_ids, state, created_at, expires_at, terminal_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'active',?,?,NULL)",
                (request_id, operation, contact_id, email_masked, email_hmac,
                 contact_status, tag_id, tag_slug, self._dump_ids(ids),
                 created_at, expires_at),
            )
            self._insert_audit(
                cur, "preview_created",
                AuditFields(
                    request_id=request_id, operation=operation, contact_id=contact_id,
                    email_masked=email_masked, email_hmac=email_hmac, tag_id=tag_id,
                    tag_slug=tag_slug, pre_write_tag_ids=ids,
                ),
                now,
            )
            cur.execute("COMMIT")
        except sqlite3.OperationalError:
            self._rollback_quiet()
            raise CommandError(ErrorCategory.LOCAL_STATE_BUSY)
        except BaseException:
            self._rollback_quiet()
            raise
        finally:
            cur.close()
        return PreviewRecord(
            request_id=request_id, operation=operation, contact_id=contact_id,
            email_masked=email_masked, email_hmac=email_hmac,
            contact_status=contact_status, tag_id=tag_id, tag_slug=tag_slug,
            pre_write_tag_ids=ids, state="active", created_at=created_at,
            expires_at=expires_at, terminal_at=None,
        )

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value)

    _PREVIEW_COLS = (
        "request_id, operation, contact_id, email_masked, email_hmac, "
        "contact_status, tag_id, tag_slug, pre_write_tag_ids, state, "
        "created_at, expires_at, terminal_at"
    )
    _JOURNAL_COLS = (
        "request_id, state, mutation_attempted, created_at, "
        "dispatched_at, terminal_at, outcome"
    )

    def _locked_preview(self, cur: sqlite3.Cursor, request_id: str) -> PreviewRecord | None:
        row = cur.execute(
            f"SELECT {self._PREVIEW_COLS} FROM preview WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return self._row_to_preview(row) if row is not None else None

    def _locked_journal(self, cur: sqlite3.Cursor, request_id: str) -> JournalRecord | None:
        row = cur.execute(
            f"SELECT {self._JOURNAL_COLS} FROM execution_journal WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return self._row_to_journal(row) if row is not None else None

    def _preview_fields(self, p: PreviewRecord, **extra: object) -> AuditFields:
        return AuditFields(
            request_id=p.request_id, operation=p.operation, contact_id=p.contact_id,
            email_masked=p.email_masked, email_hmac=p.email_hmac, tag_id=p.tag_id,
            tag_slug=p.tag_slug, pre_write_tag_ids=p.pre_write_tag_ids,
            **extra,  # type: ignore[arg-type]
        )

    @contextmanager
    def _txn(self):
        """Bracket a single write in ``BEGIN IMMEDIATE`` .. ``COMMIT``.

        A lock-acquisition failure surfaces as ``local_state_busy``; any other
        error rolls back and propagates. Callers that must persist an audit row
        for a rejection commit inside the block and raise afterwards.
        """

        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            yield cur
            cur.execute("COMMIT")
        except sqlite3.OperationalError:
            self._rollback_quiet()
            raise CommandError(ErrorCategory.LOCAL_STATE_BUSY)
        except BaseException:
            self._rollback_quiet()
            raise
        finally:
            cur.close()

    def cancel_preview(self, request_id: str, now: datetime) -> str:
        """Cancel or expire an active preview; return the outcome.

        Active and not yet expired -> ``cancelled`` (``execution_cancelled``).
        Active but past its TTL -> ``expired`` (``preview_expired``). A missing,
        already-terminal, or already-expired preview is a rejection: the reason
        is audited (``preview_rejected``) and the matching error is raised.
        """

        reject: ErrorCategory | None = None
        outcome = ""
        with self._txn() as cur:
            preview = self._locked_preview(cur, request_id)
            if preview is None:
                self._insert_audit(
                    cur, "preview_rejected",
                    AuditFields(request_id=request_id,
                                error_category=ErrorCategory.PREVIEW_MISSING.value),
                    now,
                )
                reject = ErrorCategory.PREVIEW_MISSING
            elif preview.state == "active" and now >= self._parse(preview.expires_at):
                cur.execute(
                    "UPDATE preview SET state='expired', terminal_at=? "
                    "WHERE request_id=? AND state='active'",
                    (self._iso(now), request_id),
                )
                self._insert_audit(cur, "preview_expired",
                                   self._preview_fields(preview), now)
                outcome = "expired"
            elif preview.state == "active":
                cur.execute(
                    "UPDATE preview SET state='cancelled', terminal_at=? "
                    "WHERE request_id=? AND state='active'",
                    (self._iso(now), request_id),
                )
                self._insert_audit(cur, "execution_cancelled",
                                   self._preview_fields(preview), now)
                outcome = "cancelled"
            else:
                reject = (ErrorCategory.PREVIEW_EXPIRED if preview.state == "expired"
                          else ErrorCategory.PREVIEW_CONSUMED)
                self._insert_audit(
                    cur, "preview_rejected",
                    self._preview_fields(preview, error_category=reject.value), now,
                )
        if reject is not None:
            raise CommandError(reject, request_id=request_id)
        return outcome

    def claim_preview(self, request_id: str, now: datetime) -> ClaimResult:
        """Claim an active preview for execution, or resume a stranded claim.

        Active and fresh -> transition the preview to ``executed`` and create a
        linked ``claimed`` journal in one transaction (``execution_claimed``);
        return a fresh claim. An ``executed`` preview whose journal is still
        ``claimed`` is resumable and returned as ``resumed=True``. Everything
        else is a rejection audited as ``execution_rejected``:
        ``dispatching`` -> outcome unknown, a terminal journal or ``cancelled``
        preview -> consumed, ``expired`` preview -> expired. An active but
        past-TTL preview is expired first, then rejected.
        """

        reject: ErrorCategory | None = None
        result: ClaimResult | None = None
        with self._txn() as cur:
            preview = self._locked_preview(cur, request_id)
            journal = self._locked_journal(cur, request_id)
            if preview is None:
                self._insert_audit(
                    cur, "execution_rejected",
                    AuditFields(request_id=request_id,
                                error_category=ErrorCategory.PREVIEW_MISSING.value),
                    now,
                )
                reject = ErrorCategory.PREVIEW_MISSING
            elif preview.state == "active" and now >= self._parse(preview.expires_at):
                cur.execute(
                    "UPDATE preview SET state='expired', terminal_at=? "
                    "WHERE request_id=? AND state='active'",
                    (self._iso(now), request_id),
                )
                self._insert_audit(cur, "preview_expired",
                                   self._preview_fields(preview), now)
                reject = ErrorCategory.PREVIEW_EXPIRED
            elif preview.state == "active":
                stamp = self._iso(now)
                cur.execute(
                    "UPDATE preview SET state='executed', terminal_at=? "
                    "WHERE request_id=? AND state='active'",
                    (stamp, request_id),
                )
                cur.execute(
                    "INSERT INTO execution_journal (request_id, state, "
                    "mutation_attempted, created_at, dispatched_at, terminal_at, "
                    "outcome) VALUES (?, 'claimed', 0, ?, NULL, NULL, NULL)",
                    (request_id, stamp),
                )
                self._insert_audit(cur, "execution_claimed",
                                   self._preview_fields(preview), now)
                result = ClaimResult(
                    preview=replace(preview, state="executed", terminal_at=stamp),
                    journal=JournalRecord(
                        request_id=request_id, state="claimed",
                        mutation_attempted=False, created_at=stamp,
                        dispatched_at=None, terminal_at=None, outcome=None,
                    ),
                    resumed=False,
                )
            elif preview.state == "executed":
                if journal is None:
                    # An executed preview must have a journal; absence is corruption.
                    raise _invalid()
                if journal.state == "claimed":
                    result = ClaimResult(preview=preview, journal=journal, resumed=True)
                elif journal.state == "dispatching":
                    reject = ErrorCategory.EXECUTION_OUTCOME_UNKNOWN
                    self._insert_audit(
                        cur, "execution_rejected",
                        self._preview_fields(preview, error_category=reject.value), now,
                    )
                else:
                    reject = ErrorCategory.PREVIEW_CONSUMED
                    self._insert_audit(
                        cur, "execution_rejected",
                        self._preview_fields(preview, error_category=reject.value), now,
                    )
            else:
                reject = (ErrorCategory.PREVIEW_EXPIRED if preview.state == "expired"
                          else ErrorCategory.PREVIEW_CONSUMED)
                self._insert_audit(
                    cur, "execution_rejected",
                    self._preview_fields(preview, error_category=reject.value), now,
                )
        if reject is not None:
            raise CommandError(reject, request_id=request_id)
        assert result is not None  # exactly one of reject/result is set above
        return result

    def mark_dispatching(self, request_id: str, now: datetime) -> None:
        """Move a ``claimed`` journal to ``dispatching`` just before the write.

        Sets ``mutation_attempted`` so a later crash is reconciled as an unknown
        outcome rather than assumed un-attempted. Emits ``execution_dispatching``.
        Only a ``claimed`` journal is a valid precondition.
        """

        with self._txn() as cur:
            journal = self._locked_journal(cur, request_id)
            if journal is None or journal.state != "claimed":
                raise _invalid()
            preview = self._locked_preview(cur, request_id)
            cur.execute(
                "UPDATE execution_journal SET state='dispatching', "
                "mutation_attempted=1, dispatched_at=? "
                "WHERE request_id=? AND state='claimed'",
                (self._iso(now), request_id),
            )
            fields = (self._preview_fields(preview)
                      if preview is not None
                      else AuditFields(request_id=request_id))
            self._insert_audit(cur, "execution_dispatching", fields, now)

    def finalize_journal(
        self,
        request_id: str,
        outcome: str,
        now: datetime,
        *,
        http_status: int | None = None,
        verification_ts: str | None = None,
    ) -> None:
        """Move a ``dispatching`` (or, for ``rejected``, ``claimed``) journal to
        its terminal outcome and append the matching audit event.

        ``outcome`` is one of ``succeeded``, ``recovered``, ``rejected``,
        ``verification_failed``, ``outcome_unknown``. ``rejected`` may finalize a
        journal that never left ``claimed`` (validation rejection before any
        write); every other outcome requires ``dispatching``.
        """

        event = _FINALIZE_EVENTS.get(outcome)
        if event is None:
            raise _invalid()
        allowed = ("claimed", "dispatching") if outcome == "rejected" else ("dispatching",)
        with self._txn() as cur:
            journal = self._locked_journal(cur, request_id)
            if journal is None or journal.state not in allowed:
                raise _invalid()
            cur.execute(
                "UPDATE execution_journal SET state=?, terminal_at=?, outcome=? "
                "WHERE request_id=? AND state=?",
                (outcome, self._iso(now), outcome, request_id, journal.state),
            )
            preview = self._locked_preview(cur, request_id)
            base = (self._preview_fields(preview)
                    if preview is not None
                    else AuditFields(request_id=request_id))
            self._insert_audit(
                cur, event,
                replace(base, http_status=http_status, verification_ts=verification_ts),
                now,
            )

    def reconcile_dispatching(self, request_id: str, now: datetime) -> JournalRecord:
        """Reconcile a stranded ``dispatching`` journal to ``outcome_unknown``.

        Never re-writes remotely: a stranded dispatch may already have taken
        effect, so the local record is closed as unknown and the operator must
        reconcile out of band. Emits ``execution_outcome_unknown``. Any other
        journal state is a corruption/precondition failure.
        """

        with self._txn() as cur:
            journal = self._locked_journal(cur, request_id)
            if journal is None or journal.state != "dispatching":
                raise _invalid()
            cur.execute(
                "UPDATE execution_journal SET state='outcome_unknown', "
                "terminal_at=?, outcome='outcome_unknown' "
                "WHERE request_id=? AND state='dispatching'",
                (self._iso(now), request_id),
            )
            preview = self._locked_preview(cur, request_id)
            fields = (self._preview_fields(preview)
                      if preview is not None
                      else AuditFields(request_id=request_id))
            self._insert_audit(cur, "execution_outcome_unknown", fields, now)
            updated = self._locked_journal(cur, request_id)
            assert updated is not None  # just updated in this txn
            return updated

    def record_reconciliation(
        self,
        request_id: str,
        tag_present: bool | None,
        now: datetime,
    ) -> None:
        """Append a reconcile-check audit event without any state transition.

        ``tag_present`` True/False/None maps to ``reconciliation_present`` /
        ``reconciliation_absent`` / ``reconciliation_unknown``.
        """

        event = ("reconciliation_present" if tag_present is True
                 else "reconciliation_absent" if tag_present is False
                 else "reconciliation_unknown")
        preview = self.get_preview(request_id)
        fields = (self._preview_fields(preview)
                  if preview is not None
                  else AuditFields(request_id=request_id))
        self.append_event(event, replace(fields, verification_ts=self._iso(now)), now)

    def run_maintenance(self, now: datetime) -> int:
        """Delete up to ``MAINTENANCE_BATCH`` expired terminal previews, oldest first.

        Best-effort: only terminal previews (``executed``/``cancelled``/
        ``expired``) whose ``terminal_at`` is older than the retention window are
        eligible, and only when their journal (if any) is terminal too. Their
        journal rows go with them; audit rows are never deleted. Returns the
        number of previews deleted. A lock failure is swallowed (returns 0) so
        maintenance never blocks the operation.
        """

        cutoff = self._iso(now - RETENTION)
        try:
            with self._txn() as cur:
                rows = cur.execute(
                    "SELECT p.request_id FROM preview p "
                    "LEFT JOIN execution_journal j ON j.request_id = p.request_id "
                    "WHERE p.state IN ('executed','cancelled','expired') "
                    "AND p.terminal_at IS NOT NULL AND p.terminal_at < ? "
                    "AND (j.request_id IS NULL OR j.state IN "
                    "('succeeded','recovered','rejected','verification_failed',"
                    "'outcome_unknown')) "
                    "ORDER BY p.terminal_at ASC LIMIT ?",
                    (cutoff, MAINTENANCE_BATCH),
                ).fetchall()
                ids = [r[0] for r in rows]
                for request_id in ids:
                    cur.execute(
                        "DELETE FROM execution_journal WHERE request_id = ?",
                        (request_id,),
                    )
                    cur.execute("DELETE FROM preview WHERE request_id = ?", (request_id,))
        except CommandError:
            # Retention is best-effort; a busy or invalid store never blocks work.
            return 0
        return len(ids)













