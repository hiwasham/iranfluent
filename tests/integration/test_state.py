"""Integration tests for the SQLite ``StateStore`` (T2).

These exercise real on-disk SQLite: schema init/validation, permissions, the
preview and execution-journal state machines, append-only audit monotonicity,
bounded retention, and every fail-closed branch (busy locks, corruption,
partial init). No FluentCRM or network access occurs.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from iranfluent_tag_operator.config import RuntimeIdentity
from iranfluent_tag_operator.models import CommandError, ErrorCategory
from iranfluent_tag_operator.state import (
    MAINTENANCE_BATCH,
    PREVIEW_TTL,
    RETENTION,
    AuditFields,
    ClaimResult,
    StateStore,
    _default_db_path,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
PAST_TTL = NOW + PREVIEW_TTL + timedelta(minutes=1)
IDENT = RuntimeIdentity(
    package_version="0.1.0", git_commit="a" * 40,
    uv_lock_sha256="b" * 64, allowlist_sha256="c" * 64,
)


def _connect(path: Path) -> StateStore:
    return StateStore.connect(identity=IDENT, actor="tool/operator", path=path)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = _connect(tmp_path / "state.sqlite3")
    yield s
    s.close()


def _create(store: StateStore, now: datetime = NOW, request_id: str = "req-1"):
    return store.create_preview(
        request_id=request_id, operation="add_tag", contact_id=7,
        email_masked="a***@example.com", email_hmac="hmac", contact_status="subscribed",
        tag_id=269, tag_slug="set_vocab_b1", pre_write_tag_ids=(1, 2), now=now,
    )


def _events(store: StateStore) -> list[tuple[int, str]]:
    return [
        (r[0], r[1])
        for r in store._conn.execute("SELECT seq, event_type FROM audit ORDER BY seq")
    ]


@contextmanager
def _exclusive_lock(path: Path):
    lock = sqlite3.connect(str(path))
    lock.isolation_level = None
    lock.execute("BEGIN EXCLUSIVE")
    try:
        yield
    finally:
        lock.execute("ROLLBACK")
        lock.close()


class _NoScriptExec(sqlite3.Connection):
    def executescript(self, sql):  # type: ignore[override]
        raise sqlite3.DatabaseError("boom")


class _RuntimeScriptExec(sqlite3.Connection):
    def executescript(self, sql):  # type: ignore[override]
        raise RuntimeError("boom")


class _BeginExclusiveBusy(sqlite3.Connection):
    def execute(self, sql, *a):  # type: ignore[override]
        if sql == "BEGIN EXCLUSIVE":
            raise sqlite3.OperationalError("locked")
        return super().execute(sql, *a)


class _BadIntegrity(sqlite3.Connection):
    def execute(self, sql, *a):  # type: ignore[override]
        if sql == "PRAGMA integrity_check":
            class _R:
                def fetchone(self):
                    return ("malformed",)
            return _R()
        return super().execute(sql, *a)


def _manual(path: Path, factory) -> StateStore:
    conn = sqlite3.connect(str(path), factory=factory)
    conn.isolation_level = None
    return StateStore(conn, identity=IDENT, actor="tool/operator")


# --- schema init, permissions, fail-closed startup ---

def test_connect_creates_schema_and_sets_permissions(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "state.sqlite3"
    s = _connect(path)
    assert s._user_version() == 1
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    s.close()


def test_connect_reopen_validates_existing_schema(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    _connect(path).close()
    s = _connect(path)  # existed -> validate path, no chmod
    assert s._user_version() == 1
    s.close()


class _FakeCur:
    def execute(self, *a):
        raise sqlite3.OperationalError("locked")

    def close(self):
        pass


class _PragmaBusy(sqlite3.Connection):
    def cursor(self, *a, **k):  # type: ignore[override]
        return _FakeCur()


def _open(tmp_path: Path, name: str = "state.sqlite3") -> tuple[StateStore, Path]:
    path = tmp_path / name
    return _connect(path), path


def test_connect_oserror_is_local_state_invalid(tmp_path: Path) -> None:
    (tmp_path / "afile").write_text("x")
    with pytest.raises(CommandError) as e:
        _connect(tmp_path / "afile" / "state.sqlite3")  # parent is a file
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_apply_pragmas_operational_error_is_invalid(tmp_path: Path) -> None:
    store = _manual(tmp_path / "s.sqlite3", _PragmaBusy)
    with pytest.raises(CommandError) as e:
        store._apply_pragmas()
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID
    store.close()


def test_init_schema_databaseerror_rolls_back_and_invalid(tmp_path: Path) -> None:
    store = _manual(tmp_path / "s.sqlite3", _NoScriptExec)
    with pytest.raises(CommandError) as e:
        store._init_schema()
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID
    store.close()


def test_init_schema_unexpected_error_rolls_back_and_reraises(tmp_path: Path) -> None:
    store = _manual(tmp_path / "s.sqlite3", _RuntimeScriptExec)
    with pytest.raises(RuntimeError):
        store._init_schema()
    store.close()


def test_init_schema_begin_exclusive_busy(tmp_path: Path) -> None:
    store = _manual(tmp_path / "s.sqlite3", _BeginExclusiveBusy)
    with pytest.raises(CommandError) as e:
        store._init_schema()
    assert e.value.category is ErrorCategory.LOCAL_STATE_BUSY
    store.close()


def _raw(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.isolation_level = None
    c.execute("PRAGMA foreign_keys=OFF")
    return c


def test_init_schema_reopen_loser_validates_and_returns(tmp_path: Path) -> None:
    store, _ = _open(tmp_path)
    store._init_schema()  # version already 1 -> loser branch validates, no-op
    assert store._user_version() == 1
    store.close()


def test_connect_unsupported_user_version_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    c = _raw(path)
    c.execute("PRAGMA user_version=2")
    c.close()
    with pytest.raises(CommandError) as e:
        _connect(path)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_init_schema_partial_init_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    c = _raw(path)
    c.execute("CREATE TABLE preview (request_id TEXT)")  # version stays 0
    c.close()
    with pytest.raises(CommandError) as e:
        _connect(path)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_validate_schema_integrity_failure_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    _connect(path).close()
    store = _manual(path, _BadIntegrity)
    with pytest.raises(CommandError) as e:
        store._validate_schema()
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID
    store.close()


def test_validate_schema_missing_table_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    _connect(path).close()
    c = _raw(path)
    c.execute("DROP TABLE audit")
    c.close()
    with pytest.raises(CommandError) as e:
        _connect(path)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_validate_schema_column_mismatch_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    _connect(path).close()
    c = _raw(path)
    c.execute("ALTER TABLE preview ADD COLUMN sneaky TEXT")
    c.close()
    with pytest.raises(CommandError) as e:
        _connect(path)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_validate_schema_index_mismatch_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    _connect(path).close()
    c = _raw(path)
    c.execute("DROP INDEX idx_audit_request_seq")
    c.close()
    with pytest.raises(CommandError) as e:
        _connect(path)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


@contextmanager
def _force_busy(store: StateStore, path: Path):
    # Shrink the store's busy_timeout so a competing EXCLUSIVE lock trips
    # SQLITE_BUSY immediately instead of blocking for five seconds.
    store._conn.execute("PRAGMA busy_timeout=1")
    with _exclusive_lock(path):
        yield


# --- write-lock probe, audit append, preview creation ---

def test_probe_write_lock_ok(store: StateStore) -> None:
    store.probe_write_lock()  # no raise on a free database


def test_probe_write_lock_busy(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    with _force_busy(store, path):
        with pytest.raises(CommandError) as e:
            store.probe_write_lock()
    assert e.value.category is ErrorCategory.LOCAL_STATE_BUSY
    store.close()


def test_append_event_bad_type_is_invalid(store: StateStore) -> None:
    with pytest.raises(CommandError) as e:
        store.append_event("not_a_real_event", AuditFields(request_id="req-1"), NOW)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_append_event_busy(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    with _force_busy(store, path):
        with pytest.raises(CommandError) as e:
            store.record_noop(AuditFields(request_id="req-1"), NOW)
    assert e.value.category is ErrorCategory.LOCAL_STATE_BUSY
    store.close()


def test_record_noop_and_rejection_write_audit(store: StateStore) -> None:
    store.record_noop(AuditFields(request_id="req-1"), NOW)
    store.record_rejection("preview_rejected", AuditFields(request_id="req-1"), NOW)
    assert _events(store) == [(1, "preview_noop"), (2, "preview_rejected")]


def test_create_preview_writes_row_and_audit(store: StateStore) -> None:
    preview = _create(store)
    assert preview.state == "active"
    assert preview.pre_write_tag_ids == (1, 2)
    assert store.get_preview("req-1").request_id == "req-1"
    assert _events(store) == [(1, "preview_created")]


def test_create_preview_busy(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    with _force_busy(store, path):
        with pytest.raises(CommandError) as e:
            _create(store)
    assert e.value.category is ErrorCategory.LOCAL_STATE_BUSY
    store.close()


def test_create_preview_duplicate_id_reraises(store: StateStore) -> None:
    _create(store)
    with pytest.raises(sqlite3.IntegrityError):
        _create(store)  # same request_id -> PRIMARY KEY collision


def test_get_preview_and_journal_missing_return_none(store: StateStore) -> None:
    assert store.get_preview("nope") is None
    assert store.get_journal("nope") is None


def _delete_journal(path: Path, request_id: str) -> None:
    c = _raw(path)
    c.execute("DELETE FROM execution_journal WHERE request_id=?", (request_id,))
    c.close()


def _delete_preview(path: Path, request_id: str) -> None:
    c = _raw(path)  # FK off so the orphaned journal survives
    c.execute("DELETE FROM preview WHERE request_id=?", (request_id,))
    c.close()


# --- cancel_preview state machine ---

def test_cancel_active_fresh_is_cancelled(store: StateStore) -> None:
    _create(store)
    assert store.cancel_preview("req-1", NOW) == "cancelled"
    assert store.get_preview("req-1").state == "cancelled"
    assert _events(store)[-1][1] == "execution_cancelled"


def test_cancel_active_past_ttl_is_expired(store: StateStore) -> None:
    _create(store)
    assert store.cancel_preview("req-1", PAST_TTL) == "expired"
    assert store.get_preview("req-1").state == "expired"
    assert _events(store)[-1][1] == "preview_expired"


def test_cancel_missing_raises_preview_missing(store: StateStore) -> None:
    with pytest.raises(CommandError) as e:
        store.cancel_preview("ghost", NOW)
    assert e.value.category is ErrorCategory.PREVIEW_MISSING
    assert _events(store)[-1][1] == "preview_rejected"


def test_cancel_already_expired_raises_preview_expired(store: StateStore) -> None:
    _create(store)
    store.cancel_preview("req-1", PAST_TTL)  # -> expired
    with pytest.raises(CommandError) as e:
        store.cancel_preview("req-1", PAST_TTL)
    assert e.value.category is ErrorCategory.PREVIEW_EXPIRED


def test_cancel_already_terminal_raises_preview_consumed(store: StateStore) -> None:
    _create(store)
    store.cancel_preview("req-1", NOW)  # -> cancelled
    with pytest.raises(CommandError) as e:
        store.cancel_preview("req-1", NOW)
    assert e.value.category is ErrorCategory.PREVIEW_CONSUMED


# --- claim_preview state machine ---

def test_claim_active_fresh_executes_and_journals(store: StateStore) -> None:
    _create(store)
    result = store.claim_preview("req-1", NOW)
    assert result.resumed is False
    assert result.preview.state == "executed"
    assert result.journal.state == "claimed"
    assert store.get_journal("req-1").state == "claimed"
    assert _events(store)[-1][1] == "execution_claimed"


def test_claim_missing_raises_preview_missing(store: StateStore) -> None:
    with pytest.raises(CommandError) as e:
        store.claim_preview("ghost", NOW)
    assert e.value.category is ErrorCategory.PREVIEW_MISSING


def test_claim_active_past_ttl_expires_and_rejects(store: StateStore) -> None:
    _create(store)
    with pytest.raises(CommandError) as e:
        store.claim_preview("req-1", PAST_TTL)
    assert e.value.category is ErrorCategory.PREVIEW_EXPIRED
    assert store.get_preview("req-1").state == "expired"


def test_claim_resumes_stranded_claimed(store: StateStore) -> None:
    _create(store)
    store.claim_preview("req-1", NOW)
    resumed = store.claim_preview("req-1", NOW)
    assert resumed.resumed is True
    assert resumed.journal.state == "claimed"


def test_claim_executed_without_journal_is_invalid(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    _create(store)
    store.claim_preview("req-1", NOW)
    _delete_journal(path, "req-1")  # executed preview, journal gone -> corruption
    with pytest.raises(CommandError) as e:
        store.claim_preview("req-1", NOW)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID
    store.close()


def test_claim_dispatching_is_outcome_unknown(store: StateStore) -> None:
    _create(store)
    store.claim_preview("req-1", NOW)
    store.mark_dispatching("req-1", NOW)
    with pytest.raises(CommandError) as e:
        store.claim_preview("req-1", NOW)
    assert e.value.category is ErrorCategory.EXECUTION_OUTCOME_UNKNOWN


def test_claim_terminal_journal_is_consumed(store: StateStore) -> None:
    _create(store)
    store.claim_preview("req-1", NOW)
    store.mark_dispatching("req-1", NOW)
    store.finalize_journal("req-1", "succeeded", NOW)
    with pytest.raises(CommandError) as e:
        store.claim_preview("req-1", NOW)
    assert e.value.category is ErrorCategory.PREVIEW_CONSUMED


def test_claim_expired_preview_rejects_expired(store: StateStore) -> None:
    _create(store)
    store.cancel_preview("req-1", PAST_TTL)  # -> expired
    with pytest.raises(CommandError) as e:
        store.claim_preview("req-1", NOW)
    assert e.value.category is ErrorCategory.PREVIEW_EXPIRED


def test_claim_cancelled_preview_rejects_consumed(store: StateStore) -> None:
    _create(store)
    store.cancel_preview("req-1", NOW)  # -> cancelled
    with pytest.raises(CommandError) as e:
        store.claim_preview("req-1", NOW)
    assert e.value.category is ErrorCategory.PREVIEW_CONSUMED


def _last_audit(store: StateStore) -> tuple:
    return store._conn.execute(
        "SELECT event_type, http_status, verification_ts FROM audit "
        "ORDER BY seq DESC LIMIT 1"
    ).fetchone()


# --- mark_dispatching ---

def test_mark_dispatching_sets_state_and_flag(store: StateStore) -> None:
    _create(store)
    store.claim_preview("req-1", NOW)
    store.mark_dispatching("req-1", NOW)
    journal = store.get_journal("req-1")
    assert journal.state == "dispatching"
    assert journal.mutation_attempted is True
    assert _events(store)[-1][1] == "execution_dispatching"


def test_mark_dispatching_missing_journal_is_invalid(store: StateStore) -> None:
    with pytest.raises(CommandError) as e:
        store.mark_dispatching("ghost", NOW)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_mark_dispatching_loser_race_is_outcome_unknown(store: StateStore) -> None:
    _create(store)
    store.claim_preview("req-1", NOW)
    store.mark_dispatching("req-1", NOW)
    with pytest.raises(CommandError) as e:
        store.mark_dispatching("req-1", NOW)  # already dispatching (race loser)
    assert e.value.category is ErrorCategory.EXECUTION_OUTCOME_UNKNOWN
    assert e.value.mutation_attempted is True
    assert _events(store)[-1][1] == "execution_rejected"


def test_mark_dispatching_orphan_journal_uses_fallback(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    _create(store)
    store.claim_preview("req-1", NOW)
    _delete_preview(path, "req-1")
    store.mark_dispatching("req-1", NOW)
    assert store.get_journal("req-1").state == "dispatching"
    store.close()


def test_mark_dispatching_loser_race_orphan_preview_uses_fallback(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    _create(store)
    store.claim_preview("req-1", NOW)
    store.mark_dispatching("req-1", NOW)
    _delete_preview(path, "req-1")  # loser race with the preview row already gone
    with pytest.raises(CommandError) as e:
        store.mark_dispatching("req-1", NOW)
    assert e.value.category is ErrorCategory.EXECUTION_OUTCOME_UNKNOWN
    assert _events(store)[-1][1] == "execution_rejected"
    store.close()


# --- finalize_journal ---

def test_finalize_unknown_outcome_is_invalid(store: StateStore) -> None:
    with pytest.raises(CommandError) as e:
        store.finalize_journal("req-1", "not_a_real_outcome", NOW)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_finalize_missing_journal_is_invalid(store: StateStore) -> None:
    with pytest.raises(CommandError) as e:
        store.finalize_journal("ghost", "succeeded", NOW)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_finalize_succeeded_requires_dispatching(store: StateStore) -> None:
    _create(store)
    store.claim_preview("req-1", NOW)  # journal still 'claimed'
    with pytest.raises(CommandError) as e:
        store.finalize_journal("req-1", "succeeded", NOW)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_finalize_rejected_allowed_from_claimed(store: StateStore) -> None:
    _create(store)
    store.claim_preview("req-1", NOW)
    store.finalize_journal("req-1", "rejected", NOW)
    assert store.get_journal("req-1").state == "rejected"
    assert _events(store)[-1][1] == "execution_rejected"


def test_finalize_succeeded_carries_status_and_verification(store: StateStore) -> None:
    _create(store)
    store.claim_preview("req-1", NOW)
    store.mark_dispatching("req-1", NOW)
    store.finalize_journal("req-1", "succeeded", NOW, http_status=200,
                           verification_ts="2026-09-01T12:05:00+00:00")
    journal = store.get_journal("req-1")
    assert journal.state == "succeeded" and journal.outcome == "succeeded"
    event, http_status, verification_ts = _last_audit(store)
    assert event == "execution_succeeded"
    assert http_status == 200
    assert verification_ts == "2026-09-01T12:05:00+00:00"


def test_finalize_orphan_journal_uses_fallback(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    _create(store)
    store.claim_preview("req-1", NOW)
    store.mark_dispatching("req-1", NOW)
    _delete_preview(path, "req-1")
    store.finalize_journal("req-1", "succeeded", NOW)
    assert store.get_journal("req-1").state == "succeeded"
    store.close()


# --- reconcile_dispatching ---

def test_reconcile_dispatching_to_outcome_unknown(store: StateStore) -> None:
    _create(store)
    store.claim_preview("req-1", NOW)
    store.mark_dispatching("req-1", NOW)
    updated = store.reconcile_dispatching("req-1", NOW)
    assert updated.state == "outcome_unknown" and updated.outcome == "outcome_unknown"
    assert _events(store)[-1][1] == "execution_outcome_unknown"


def test_reconcile_missing_journal_is_invalid(store: StateStore) -> None:
    with pytest.raises(CommandError) as e:
        store.reconcile_dispatching("ghost", NOW)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_reconcile_wrong_state_is_invalid(store: StateStore) -> None:
    _create(store)
    store.claim_preview("req-1", NOW)  # 'claimed', not 'dispatching'
    with pytest.raises(CommandError) as e:
        store.reconcile_dispatching("req-1", NOW)
    assert e.value.category is ErrorCategory.LOCAL_STATE_INVALID


def test_reconcile_orphan_journal_uses_fallback(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    _create(store)
    store.claim_preview("req-1", NOW)
    store.mark_dispatching("req-1", NOW)
    _delete_preview(path, "req-1")
    updated = store.reconcile_dispatching("req-1", NOW)
    assert updated.state == "outcome_unknown"
    store.close()


# --- record_reconciliation ---

def test_record_reconciliation_present_absent_unknown(store: StateStore) -> None:
    _create(store)  # preview present -> _preview_fields branch
    store.record_reconciliation("req-1", True, NOW)
    store.record_reconciliation("req-1", False, NOW)
    store.record_reconciliation("nope", None, NOW)  # preview missing -> fallback
    tail = [e[1] for e in _events(store)[-3:]]
    assert tail == ["reconciliation_present", "reconciliation_absent",
                    "reconciliation_unknown"]


OLD = NOW - RETENTION - timedelta(days=1)  # comfortably past the retention cutoff


def _terminal_preview(store: StateStore, request_id: str, now: datetime) -> None:
    """Create then cancel a preview so it becomes a terminal (cancelled) row."""
    _create(store, now=now, request_id=request_id)
    store.cancel_preview(request_id, now)


# --- run_maintenance (bounded retention) ---

def test_maintenance_deletes_old_terminal_preview(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    _terminal_preview(store, "old", OLD)
    assert store.run_maintenance(NOW) == 1
    assert store.get_preview("old") is None
    store.close()


def test_maintenance_keeps_recent_terminal_preview(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    _terminal_preview(store, "fresh", NOW)  # terminal_at within retention
    assert store.run_maintenance(NOW) == 0
    assert store.get_preview("fresh") is not None
    store.close()


def test_maintenance_keeps_active_preview(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    _create(store, now=OLD, request_id="active")  # never cancelled -> active
    assert store.run_maintenance(NOW) == 0
    assert store.get_preview("active").state == "active"
    store.close()


def test_maintenance_keeps_executed_with_nonterminal_journal(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    _create(store, now=OLD, request_id="claimed")
    store.claim_preview("claimed", OLD)  # preview executed (old terminal_at), journal 'claimed'
    assert store.run_maintenance(NOW) == 0  # journal not terminal -> excluded
    assert store.get_preview("claimed") is not None
    assert store.get_journal("claimed").state == "claimed"
    store.close()


def test_maintenance_busy_returns_zero(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    _terminal_preview(store, "old", OLD)
    with _force_busy(store, path):
        assert store.run_maintenance(NOW) == 0
    assert store.get_preview("old") is not None  # nothing deleted
    store.close()


def test_maintenance_batch_caps_at_100_oldest_first(tmp_path: Path) -> None:
    store, path = _open(tmp_path)
    total = MAINTENANCE_BATCH + 1  # 101 eligible terminal previews
    for i in range(total):
        _terminal_preview(store, f"r{i:03d}", OLD + timedelta(minutes=i))
    assert store.run_maintenance(NOW) == MAINTENANCE_BATCH  # exactly 100 deleted
    survivors = [r[0] for r in store._conn.execute("SELECT request_id FROM preview")]
    # Oldest-first deletion leaves the single newest terminal_at (i == total - 1).
    assert survivors == [f"r{total - 1:03d}"]
    store.close()


# --- append-only audit monotonicity ---

def test_audit_seq_is_contiguous_and_strictly_increasing(store: StateStore) -> None:
    _create(store)                       # preview_created
    store.record_noop(AuditFields(request_id="req-1"), NOW)  # preview_noop
    store.claim_preview("req-1", NOW)    # execution_claimed
    store.mark_dispatching("req-1", NOW)  # execution_dispatching
    store.finalize_journal("req-1", "succeeded", NOW, http_status=200)  # execution_succeeded
    store.record_reconciliation("req-1", True, NOW)  # reconciliation_present
    seqs = [seq for seq, _ in _events(store)]
    assert seqs == list(range(1, len(seqs) + 1))  # 1..N contiguous, strictly increasing


# --- concurrent claim: single fresh winner ---

def test_concurrent_claim_yields_single_fresh_winner(tmp_path: Path) -> None:
    store_a, path = _open(tmp_path)
    _create(store_a, request_id="race")
    store_b = _connect(path)  # second connection to the same file
    try:
        first = store_a.claim_preview("race", NOW)
        second = store_b.claim_preview("race", NOW)
        results = [first, second]
        # Exactly one claim executed the preview (resumed=False); the other resumed.
        assert sum(1 for r in results if not r.resumed) == 1
        assert sum(1 for r in results if r.resumed) == 1
        assert all(isinstance(r, ClaimResult) for r in results)
    finally:
        store_b.close()
        store_a.close()


# __APPEND_8__


def test_default_db_path_honors_xdg_state_home(monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert _default_db_path() == Path(
        "/xdg/state/iranfluent-tag-operator/state.sqlite3"
    )


def test_default_db_path_falls_back_to_home_local_state(monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/op")))
    assert _default_db_path() == Path(
        "/home/op/.local/state/iranfluent-tag-operator/state.sqlite3"
    )
