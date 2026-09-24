"""Unit tests for application orchestration (T5): preview, cancel, reconcile.

Deterministic fakes stand in for the FluentCRM client and the state store so
every policy branch, rejection path, and audit call is exercised without HTTP,
SQLite, or a clock. Fresh-state integration against the real ``StateStore`` and
a fake client lives in ``tests/integration/test_application_integration.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from iranfluent_tag_operator.application import Application, _mask_email
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

UTC = timezone.utc
NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
STALE_NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)

TAG = TagDefinition(
    key="vocab_b1", id=269, title="set_vocab_B1", slug="set_vocab_b1",
    purpose="B1 vocabulary cohort", risk_note="none",
    business_reviewed_at="2026-07-30T00:00:00Z",
    business_review_expires_at="2026-08-29T00:00:00Z",
    business_reviewer="ops", known_downstream_effects=(), enabled=True,
)

CONTACT = ContactRecord(id=7, email="Dana@Example.COM", full_name="Dana", status="subscribed", tag_ids=(1, 2))
LIVE_TAG = TagRecord(id=269, title="set_vocab_B1", slug="set_vocab_b1")


class FakeClient:
    def __init__(self, *, contact=CONTACT, tag=LIVE_TAG, by_id=CONTACT):
        self._contact, self._tag, self._by_id = contact, tag, by_id
        self.deadlines: list[float] = []

    def fetch_contact_by_email(self, email, *, deadline):
        self.deadlines.append(deadline)
        if isinstance(self._contact, Exception):
            raise self._contact
        return self._contact

    def fetch_tag(self, tag_id, *, deadline):
        if isinstance(self._tag, Exception):
            raise self._tag
        return self._tag

    def fetch_contact_by_id(self, contact_id, *, deadline):
        if isinstance(self._by_id, Exception):
            raise self._by_id
        return self._by_id


class FakeStore:
    def __init__(self, *, journal=None, preview=None, cancel="cancelled"):
        self._journal, self._preview, self._cancel = journal, preview, cancel
        self.rejections: list = []
        self.noops: list = []
        self.created: list = []
        self.reconciliations: list = []
        self.closed_dispatching: list = []

    def record_rejection(self, event_type, fields, now):
        self.rejections.append((event_type, fields))

    def record_noop(self, fields, now):
        self.noops.append(fields)

    def create_preview(self, **kw):
        self.created.append(kw)
        return SimpleNamespace(request_id="req-new", expires_at="2026-08-15T12:10:00+00:00")

    def cancel_preview(self, request_id, now):
        if isinstance(self._cancel, Exception):
            raise self._cancel
        return self._cancel

    def get_journal(self, request_id):
        return self._journal

    def get_preview(self, request_id):
        return self._preview

    def reconcile_dispatching(self, request_id, now):
        self.closed_dispatching.append(request_id)

    def record_reconciliation(self, request_id, tag_present, now):
        self.reconciliations.append((request_id, tag_present))


def _app(client, store, *, now=NOW):
    return Application(
        client=client, store=store, tag_definition=TAG, audit_hmac_key="key",
        now=lambda: now, monotonic=lambda: 100.0, new_request_id=lambda: "req-new",
    )


# --- preview: success + no-op -------------------------------------------------

def test_preview_success_creates_active_preview():
    store = FakeStore()
    contact = ContactRecord(id=7, email="Dana@Example.COM", full_name="Dana", status="subscribed", tag_ids=(1, 2))
    resp = _app(FakeClient(contact=contact, by_id=contact), store).preview(
        PreviewCommand(email="  Dana@Example.COM  ", tag_key="vocab_b1")
    )
    assert isinstance(resp, PreviewResponse)
    assert resp.request_id == "req-new"
    assert resp.contact.masked_email == "d***@example.com"
    assert resp.current_tag_ids == (1, 2)
    assert resp.expires_at == "2026-08-15T12:10:00+00:00"
    assert store.created and store.created[0]["email_masked"] == "d***@example.com"
    assert store.created[0]["pre_write_tag_ids"] == (1, 2)
    assert not store.noops and not store.rejections


def test_preview_already_attached_records_noop():
    store = FakeStore()
    attached = ContactRecord(id=7, email="dana@example.com", full_name="Dana", status="subscribed", tag_ids=(269, 1))
    resp = _app(FakeClient(contact=attached), store).preview(
        PreviewCommand(email="dana@example.com", tag_key="vocab_b1")
    )
    assert isinstance(resp, AlreadyAttachedResponse)
    assert resp.current_tag_ids == (269, 1)
    assert len(store.noops) == 1 and store.noops[0].tag_id == 269
    assert not store.created


# --- preview: rejection paths (each audited as preview_rejected) --------------

@pytest.mark.parametrize("email", ["   ", "da na@example.com"])
def test_preview_invalid_email(email):
    store = FakeStore()
    with pytest.raises(CommandError) as exc:
        _app(FakeClient(), store).preview(PreviewCommand(email=email, tag_key="vocab_b1"))
    assert exc.value.category is ErrorCategory.INVALID_EMAIL
    (event, fields), = store.rejections
    assert event == "preview_rejected"
    assert fields.contact_id is None and fields.tag_id is None


def test_preview_tag_key_not_allowed():
    store = FakeStore()
    with pytest.raises(CommandError) as exc:
        _app(FakeClient(), store).preview(PreviewCommand(email="dana@example.com", tag_key="other"))
    assert exc.value.category is ErrorCategory.TAG_NOT_ALLOWED
    assert store.rejections[0][1].tag_id is None


def test_preview_tag_disabled():
    store = FakeStore()
    app = Application(
        client=FakeClient(), store=store,
        tag_definition=TagDefinition(**{**TAG.__dict__, "enabled": False}),
        audit_hmac_key="key", now=lambda: NOW, monotonic=lambda: 100.0,
        new_request_id=lambda: "req-new",
    )
    with pytest.raises(CommandError) as exc:
        app.preview(PreviewCommand(email="dana@example.com", tag_key="vocab_b1"))
    assert exc.value.category is ErrorCategory.TAG_NOT_ALLOWED


def test_preview_business_approval_stale():
    store = FakeStore()
    with pytest.raises(CommandError) as exc:
        _app(FakeClient(), store, now=STALE_NOW).preview(
            PreviewCommand(email="dana@example.com", tag_key="vocab_b1")
        )
    assert exc.value.category is ErrorCategory.BUSINESS_APPROVAL_STALE


def test_preview_contact_not_found():
    store = FakeStore()
    with pytest.raises(CommandError) as exc:
        _app(FakeClient(contact=None), store).preview(
            PreviewCommand(email="dana@example.com", tag_key="vocab_b1")
        )
    assert exc.value.category is ErrorCategory.CONTACT_NOT_FOUND


def test_preview_contact_ambiguous_propagates_and_audits():
    store = FakeStore()
    err = CommandError(ErrorCategory.CONTACT_AMBIGUOUS)
    with pytest.raises(CommandError) as exc:
        _app(FakeClient(contact=err), store).preview(
            PreviewCommand(email="dana@example.com", tag_key="vocab_b1")
        )
    assert exc.value.category is ErrorCategory.CONTACT_AMBIGUOUS
    assert store.rejections[0][0] == "preview_rejected"


def test_preview_tag_missing_is_definition_mismatch():
    store = FakeStore()
    with pytest.raises(CommandError) as exc:
        _app(FakeClient(tag=None), store).preview(
            PreviewCommand(email="dana@example.com", tag_key="vocab_b1")
        )
    assert exc.value.category is ErrorCategory.TAG_DEFINITION_MISMATCH
    # contact resolved before the tag check, so its audit fields are populated
    assert store.rejections[0][1].contact_id == 7
    assert store.rejections[0][1].email_masked == "d***@example.com"


@pytest.mark.parametrize("live", [
    TagRecord(id=270, title="set_vocab_B1", slug="set_vocab_b1"),
    TagRecord(id=269, title="renamed", slug="set_vocab_b1"),
    TagRecord(id=269, title="set_vocab_B1", slug="renamed"),
])
def test_preview_tag_field_mismatch_is_definition_mismatch(live):
    store = FakeStore()
    with pytest.raises(CommandError) as exc:
        _app(FakeClient(tag=live), store).preview(
            PreviewCommand(email="dana@example.com", tag_key="vocab_b1")
        )
    assert exc.value.category is ErrorCategory.TAG_DEFINITION_MISMATCH


def test_preview_contact_status_rejected():
    store = FakeStore()
    unsubscribed = ContactRecord(id=7, email="dana@example.com", full_name="Dana", status="unsubscribed", tag_ids=(1,))
    with pytest.raises(CommandError) as exc:
        _app(FakeClient(contact=unsubscribed), store).preview(
            PreviewCommand(email="dana@example.com", tag_key="vocab_b1")
        )
    assert exc.value.category is ErrorCategory.CONTACT_STATUS_REJECTED


# --- cancel -------------------------------------------------------------------

def test_cancel_returns_store_outcome():
    resp = _app(FakeClient(), FakeStore(cancel="expired")).cancel(CancelCommand(request_id="req-1"))
    assert resp.request_id == "req-1" and resp.outcome == "expired"


def test_cancel_propagates_store_rejection():
    store = FakeStore(cancel=CommandError(ErrorCategory.PREVIEW_MISSING))
    with pytest.raises(CommandError) as exc:
        _app(FakeClient(), store).cancel(CancelCommand(request_id="missing"))
    assert exc.value.category is ErrorCategory.PREVIEW_MISSING


# --- reconcile ----------------------------------------------------------------

def test_reconcile_no_journal_is_unknown():
    store = FakeStore(journal=None)
    resp = _app(FakeClient(), store).reconcile(ReconcileCommand(request_id="req-1"))
    assert resp.outcome == "unknown" and resp.tag_present is None
    assert store.reconciliations == [("req-1", None)]
    assert not store.closed_dispatching


def test_reconcile_dispatching_present_closes_journal():
    store = FakeStore(
        journal=SimpleNamespace(state="dispatching"),
        preview=SimpleNamespace(contact_id=7, tag_id=269),
    )
    tagged = ContactRecord(id=7, email="dana@example.com", full_name="Dana", status="subscribed", tag_ids=(269, 1))
    resp = _app(FakeClient(by_id=tagged), store).reconcile(ReconcileCommand(request_id="req-1"))
    assert resp.outcome == "present" and resp.tag_present is True
    assert store.closed_dispatching == ["req-1"]
    assert store.reconciliations == [("req-1", True)]


def test_reconcile_terminal_absent_does_not_close():
    store = FakeStore(
        journal=SimpleNamespace(state="succeeded"),
        preview=SimpleNamespace(contact_id=7, tag_id=269),
    )
    untagged = ContactRecord(id=7, email="dana@example.com", full_name="Dana", status="subscribed", tag_ids=(1, 2))
    resp = _app(FakeClient(by_id=untagged), store).reconcile(ReconcileCommand(request_id="req-1"))
    assert resp.outcome == "absent" and resp.tag_present is False
    assert not store.closed_dispatching


def test_reconcile_unreadable_contact_is_unknown():
    store = FakeStore(
        journal=SimpleNamespace(state="succeeded"),
        preview=SimpleNamespace(contact_id=7, tag_id=269),
    )
    err = CommandError(ErrorCategory.REMOTE_TRANSPORT_FAILED)
    resp = _app(FakeClient(by_id=err), store).reconcile(ReconcileCommand(request_id="req-1"))
    assert resp.outcome == "unknown" and resp.tag_present is None
    assert store.reconciliations == [("req-1", None)]


def test_reconcile_missing_preview_skips_live_read():
    store = FakeStore(journal=SimpleNamespace(state="succeeded"), preview=None)
    resp = _app(FakeClient(by_id=CommandError(ErrorCategory.REMOTE_TRANSPORT_FAILED)), store).reconcile(
        ReconcileCommand(request_id="req-1")
    )
    assert resp.outcome == "unknown" and resp.tag_present is None


# --- helper -------------------------------------------------------------------

def test_mask_email_first_codepoint_and_domain():
    assert _mask_email("dana@example.com") == "d***@example.com"
