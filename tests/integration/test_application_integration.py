"""Integration tests for application orchestration (T5) against the real store.

These wire the real on-disk ``StateStore`` to a deterministic fake FluentCRM
client, proving the preview/no-op/cancel/reconcile journeys drive real audit
rows and preview state. Exhaustive per-branch coverage lives in
``tests/unit/test_application.py``; here we confirm the adapters interlock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from iranfluent_tag_operator.application import Application
from iranfluent_tag_operator.config import RuntimeIdentity
from iranfluent_tag_operator.fluentcrm import ContactRecord, TagRecord
from iranfluent_tag_operator.models import (
    AlreadyAttachedResponse,
    CancelCommand,
    CommandError,
    ErrorCategory,
    PreviewCommand,
    PreviewResponse,
    ReconcileCommand,
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


class FakeClient:
    def __init__(self, *, contact, tag=TagRecord(269, "set_vocab_B1", "set_vocab_b1")):
        self._contact, self._tag = contact, tag

    def fetch_contact_by_email(self, email, *, deadline):
        return self._contact

    def fetch_tag(self, tag_id, *, deadline):
        return self._tag

    def fetch_contact_by_id(self, contact_id, *, deadline):
        return self._contact


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = StateStore.connect(identity=IDENT, actor="tool/operator", path=tmp_path / "state.sqlite3")
    yield s
    s.close()


def _app(store, contact, *, request_id="req-int"):
    return Application(
        client=FakeClient(contact=contact), store=store, tag_definition=TAG,
        audit_hmac_key="key", now=lambda: NOW, monotonic=lambda: 100.0,
        new_request_id=lambda: request_id,
    )


def _event_types(store):
    return [r[0] for r in store._conn.execute("SELECT event_type FROM audit ORDER BY seq")]


def test_preview_success_persists_active_preview(store):
    contact = ContactRecord(id=7, email="Dana@Example.COM", full_name="Dana", status="subscribed", tag_ids=(1, 2))
    resp = _app(store, contact).preview(PreviewCommand(email="dana@example.com", tag_key="vocab_b1"))
    assert isinstance(resp, PreviewResponse) and resp.request_id == "req-int"
    persisted = store.get_preview("req-int")
    assert persisted is not None and persisted.contact_id == 7
    assert "preview_created" in _event_types(store)


def test_preview_already_attached_persists_noop(store):
    contact = ContactRecord(id=7, email="dana@example.com", full_name="Dana", status="subscribed", tag_ids=(269,))
    resp = _app(store, contact).preview(PreviewCommand(email="dana@example.com", tag_key="vocab_b1"))
    assert isinstance(resp, AlreadyAttachedResponse)
    assert store.get_preview("req-int") is None
    assert _event_types(store) == ["preview_noop"]


def test_preview_rejection_persists_preview_rejected(store):
    with pytest.raises(CommandError) as exc:
        _app(store, None).preview(PreviewCommand(email="ghost@example.com", tag_key="vocab_b1"))
    assert exc.value.category is ErrorCategory.CONTACT_NOT_FOUND
    assert _event_types(store) == ["preview_rejected"]


def test_cancel_active_preview_returns_cancelled(store):
    contact = ContactRecord(id=7, email="dana@example.com", full_name="Dana", status="subscribed", tag_ids=(1,))
    app = _app(store, contact)
    app.preview(PreviewCommand(email="dana@example.com", tag_key="vocab_b1"))
    resp = app.cancel(CancelCommand(request_id="req-int"))
    assert resp.outcome == "cancelled"


def test_reconcile_unknown_request_is_unknown(store):
    contact = ContactRecord(id=7, email="dana@example.com", full_name="Dana", status="subscribed", tag_ids=(1,))
    resp = _app(store, contact).reconcile(ReconcileCommand(request_id="never-existed"))
    assert resp.outcome == "unknown" and resp.tag_present is None
