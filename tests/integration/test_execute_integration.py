"""Integration tests for T6 execute against the real on-disk ``StateStore``.

These prove the claim -> dispatch -> finalize journal state machine and its
audit trail interlock with real SQLite: exhaustive per-branch matrix coverage
lives in ``tests/unit/test_execute.py``. The fake client records POST attempts
and answers revalidation reads (``retryable=True``) separately from the scripted
single-shot verification reads (``retryable=False``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from iranfluent_tag_operator.application import Application
from iranfluent_tag_operator.config import RuntimeIdentity
from iranfluent_tag_operator.fluentcrm import ContactRecord, TagRecord
from iranfluent_tag_operator.models import (
    CommandError,
    ErrorCategory,
    ExecuteCommand,
    ExecuteResponse,
    PreviewCommand,
    TagDefinition,
)
from iranfluent_tag_operator.state import StateStore

UTC = timezone.utc
NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
IDENT = RuntimeIdentity(
    package_version="0.1.0", git_commit="a" * 40,
    uv_lock_sha256="b" * 64, allowlist_sha256="c" * 64,
)
TAG = TagDefinition(
    key="vocab_b1", id=269, title="set_vocab_B1", slug="set_vocab_b1",
    purpose="B1 vocabulary cohort", risk_note="none",
    business_reviewed_at="2026-07-30T00:00:00Z",
    business_review_expires_at="2026-08-29T00:00:00Z",
    business_reviewer="ops", known_downstream_effects=(), enabled=True,
)
LIVE_TAG = TagRecord(id=269, title="set_vocab_B1", slug="set_vocab_b1")


def _contact(tag_ids, *, status="subscribed"):
    return ContactRecord(id=7, email="Dana@Example.COM", full_name="Dana",
                         status=status, tag_ids=tag_ids)

class FakeClient:
    def __init__(self, *, preview_contact, reval, write=None, reads=()):
        self._preview_contact, self._reval, self._write = preview_contact, reval, write
        self._reads = list(reads)
        self.posts = 0

    def fetch_contact_by_email(self, email, *, deadline):
        return self._preview_contact

    def fetch_tag(self, tag_id, *, deadline):
        return LIVE_TAG

    def fetch_contact_by_id(self, contact_id, *, deadline, retryable=True):
        if retryable:
            return self._reval
        item = self._reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def attach_tag(self, contact_id, tag_id, *, deadline):
        self.posts += 1
        if self._write is not None:
            raise self._write


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = StateStore.connect(identity=IDENT, actor="tool/operator", path=tmp_path / "state.sqlite3")
    yield s
    s.close()


def _app(store, client):
    return Application(
        client=client, store=store, tag_definition=TAG, audit_hmac_key="key",
        now=lambda: NOW, monotonic=lambda: 100.0, sleep=lambda _: None,
        new_request_id=lambda: "req-int",
    )


def _events(store):
    return [r[0] for r in store._conn.execute("SELECT event_type FROM audit ORDER BY seq")]


def _run(store, client):
    """Create a real preview, then execute against it."""

    app = _app(store, client)
    app.preview(PreviewCommand(email="dana@example.com", tag_key="vocab_b1"))
    return app.execute(ExecuteCommand(request_id="req-int"))


def test_execute_succeeds_end_to_end(store):
    client = FakeClient(
        preview_contact=_contact((1, 2)), reval=_contact((1, 2)),
        write=None, reads=[_contact((1, 2, 269))],
    )
    resp = _run(store, client)
    assert isinstance(resp, ExecuteResponse) and resp.outcome == "succeeded"
    assert client.posts == 1
    journal = store.get_journal("req-int")
    assert (journal.state, journal.outcome, journal.mutation_attempted) == (
        "succeeded", "succeeded", True
    )
    assert _events(store)[-3:] == [
        "execution_claimed", "execution_dispatching", "execution_succeeded"
    ]


def test_execute_recovers_after_ambiguous_write(store):
    client = FakeClient(
        preview_contact=_contact((1, 2)), reval=_contact((1, 2)),
        write=CommandError(ErrorCategory.REMOTE_SERVER_ERROR, http_status=503),
        reads=[_contact((1, 2, 269))],
    )
    resp = _run(store, client)
    assert resp.outcome == "recovered" and client.posts == 1
    assert store.get_journal("req-int").outcome == "recovered"
    assert "execution_recovered" in _events(store)


def test_execute_stale_preview_rejects_before_dispatch(store):
    client = FakeClient(
        preview_contact=_contact((1, 2)), reval=_contact((1, 2, 99)), write=None,
    )
    with pytest.raises(CommandError) as exc:
        _run(store, client)
    assert exc.value.category is ErrorCategory.STALE_PREVIEW
    assert client.posts == 0
    journal = store.get_journal("req-int")
    assert (journal.state, journal.mutation_attempted) == ("rejected", False)
    assert _events(store)[-1] == "execution_rejected"

