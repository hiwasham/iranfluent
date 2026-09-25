"""Unit tests for T6 execute orchestration and the verification matrix.

Deterministic fakes replace the FluentCRM client and the state store. The fake
client records POST attempts so every path asserts the mutation is dispatched at
most once, and its verification reads are ``retryable``-aware: revalidation reads
(default ``retryable=True``) are answered separately from post-dispatch reads
(``retryable=False``), which are scripted per test to drive each matrix branch.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from iranfluent_tag_operator.application import Application
from iranfluent_tag_operator.fluentcrm import ContactRecord, TagRecord
from iranfluent_tag_operator.models import (
    CommandError,
    ErrorCategory,
    ExecuteCommand,
    ExecuteResponse,
    TagDefinition,
)
from iranfluent_tag_operator.state import PreviewRecord

UTC = timezone.utc
NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
STALE_NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
CMD = ExecuteCommand(request_id="req-1")

TAG = TagDefinition(
    key="vocab_b1", id=269, title="set_vocab_B1", slug="set_vocab_b1",
    purpose="B1 vocabulary cohort", risk_note="none",
    business_reviewed_at="2026-07-30T00:00:00Z",
    business_review_expires_at="2026-08-29T00:00:00Z",
    business_reviewer="ops", known_downstream_effects=(), enabled=True,
)
LIVE_TAG = TagRecord(id=269, title="set_vocab_B1", slug="set_vocab_b1")

PREVIEW = PreviewRecord(
    request_id="req-1", operation="add_tag", contact_id=7,
    email_masked="d***@example.com", email_hmac="h", contact_status="subscribed",
    tag_id=269, tag_slug="set_vocab_b1", pre_write_tag_ids=(1, 2), state="executed",
    created_at="2026-08-15T12:00:00+00:00", expires_at="2026-08-15T12:10:00+00:00",
    terminal_at="2026-08-15T12:05:00+00:00",
)


def _contact(tag_ids, *, status="subscribed"):
    return ContactRecord(id=7, email="Dana@Example.COM", full_name="Dana",
                         status=status, tag_ids=tag_ids)


REVAL_OK = _contact((1, 2))          # subscribed, tag set matches preview
SUCCESS_READ = _contact((1, 2, 269))  # target present, prior tags preserved
ABSENT_READ = _contact((1, 2))        # target absent, prior tags preserved
LOST_READ = _contact((1, 269))        # a prior tag (2) disappeared -> destructive


class _Claim:
    def __init__(self, preview=PREVIEW):
        self.preview, self.journal, self.resumed = preview, None, False


class FakeClient:
    def __init__(self, *, tag=LIVE_TAG, reval=REVAL_OK, write=None, reads=()):
        self._tag, self._reval, self._write = tag, reval, write
        self._reads = list(reads)
        self.posts = 0
        self.verify_reads = 0

    def fetch_tag(self, tag_id, *, deadline):
        if isinstance(self._tag, Exception):
            raise self._tag
        return self._tag

    def fetch_contact_by_id(self, contact_id, *, deadline, retryable=True):
        if retryable:
            if isinstance(self._reval, Exception):
                raise self._reval
            return self._reval
        self.verify_reads += 1
        item = self._reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def attach_tag(self, contact_id, tag_id, *, deadline):
        self.posts += 1
        if self._write is not None:
            raise self._write

class FakeStore:
    def __init__(self, *, claim=None):
        self._claim = claim if claim is not None else _Claim()
        self.marked: list[str] = []
        self.finalized: list[tuple] = []

    def claim_preview(self, request_id, now):
        if isinstance(self._claim, Exception):
            raise self._claim
        return self._claim

    def mark_dispatching(self, request_id, now):
        self.marked.append(request_id)

    def finalize_journal(self, request_id, outcome, now, *, http_status=None,
                         verification_ts=None):
        self.finalized.append((outcome, http_status, verification_ts))


def _app(client, store, *, now=NOW, monotonic=None, sleep=None):
    return Application(
        client=client, store=store, tag_definition=TAG, audit_hmac_key="key",
        now=lambda: now, monotonic=monotonic or (lambda: 100.0),
        sleep=sleep or (lambda _: None), new_request_id=lambda: "req-new",
    )


# --- claim rejection propagates without a write --------------------------------

def test_claim_rejection_propagates_without_dispatch():
    store = FakeStore(claim=CommandError(ErrorCategory.PREVIEW_MISSING, request_id="req-1"))
    client = FakeClient()
    with pytest.raises(CommandError) as exc:
        _app(client, store).execute(CMD)
    assert exc.value.category is ErrorCategory.PREVIEW_MISSING
    assert client.posts == 0
    assert store.marked == [] and store.finalized == []


# --- pre-dispatch revalidation rejections -> finalize 'rejected', never write --

def test_revalidate_business_approval_stale():
    store, client = FakeStore(), FakeClient()
    with pytest.raises(CommandError) as exc:
        _app(client, store, now=STALE_NOW).execute(CMD)
    assert exc.value.category is ErrorCategory.BUSINESS_APPROVAL_STALE
    assert client.posts == 0 and store.marked == []
    assert store.finalized == [("rejected", None, None)]


def test_revalidate_tag_definition_mismatch():
    store = FakeStore()
    client = FakeClient(tag=TagRecord(id=269, title="WRONG", slug="set_vocab_b1"))
    with pytest.raises(CommandError) as exc:
        _app(client, store).execute(CMD)
    assert exc.value.category is ErrorCategory.TAG_DEFINITION_MISMATCH
    assert client.posts == 0 and store.finalized == [("rejected", None, None)]


def test_revalidate_contact_status_rejected():
    store = FakeStore()
    client = FakeClient(reval=_contact((1, 2), status="unsubscribed"))
    with pytest.raises(CommandError) as exc:
        _app(client, store).execute(CMD)
    assert exc.value.category is ErrorCategory.CONTACT_STATUS_REJECTED
    assert client.posts == 0


def test_revalidate_stale_preview_on_tag_drift():
    store = FakeStore()
    client = FakeClient(reval=_contact((1, 2, 99)))
    with pytest.raises(CommandError) as exc:
        _app(client, store).execute(CMD)
    assert exc.value.category is ErrorCategory.STALE_PREVIEW
    assert client.posts == 0


def test_revalidate_read_failure_is_pre_dispatch_rejection():
    store = FakeStore()
    client = FakeClient(tag=CommandError(ErrorCategory.REMOTE_TRANSPORT_FAILED))
    with pytest.raises(CommandError) as exc:
        _app(client, store).execute(CMD)
    assert exc.value.category is ErrorCategory.REMOTE_TRANSPORT_FAILED
    assert exc.value.mutation_attempted is False
    assert client.posts == 0 and store.finalized == [("rejected", None, None)]

ISO_NOW = "2026-08-15T12:00:00+00:00"


# --- confirmed 2xx: one immediate read decides ---------------------------------

def test_confirmed_success():
    store = FakeStore()
    client = FakeClient(write=None, reads=[SUCCESS_READ])
    resp = _app(client, store).execute(CMD)
    assert isinstance(resp, ExecuteResponse)
    assert (resp.outcome, resp.tag_present, resp.verified_at) == ("succeeded", True, ISO_NOW)
    assert client.posts == 1 and client.verify_reads == 1
    assert store.marked == ["req-1"]
    assert store.finalized == [("succeeded", None, ISO_NOW)]


def test_confirmed_target_absent_is_verification_failed():
    store = FakeStore()
    client = FakeClient(write=None, reads=[ABSENT_READ])
    with pytest.raises(CommandError) as exc:
        _app(client, store).execute(CMD)
    assert exc.value.category is ErrorCategory.EXECUTION_VERIFICATION_FAILED
    assert exc.value.mutation_attempted is True
    assert client.posts == 1 and store.finalized == [("verification_failed", None, ISO_NOW)]


def test_confirmed_prior_tag_lost_is_verification_failed():
    store = FakeStore()
    client = FakeClient(write=None, reads=[LOST_READ])
    with pytest.raises(CommandError) as exc:
        _app(client, store).execute(CMD)
    assert exc.value.category is ErrorCategory.EXECUTION_VERIFICATION_FAILED
    assert client.posts == 1


def test_confirmed_unreadable_is_outcome_unknown():
    store = FakeStore()
    client = FakeClient(write=None, reads=[CommandError(ErrorCategory.REMOTE_TRANSPORT_FAILED)])
    with pytest.raises(CommandError) as exc:
        _app(client, store).execute(CMD)
    assert exc.value.category is ErrorCategory.EXECUTION_OUTCOME_UNKNOWN
    assert client.posts == 1 and store.finalized == [("outcome_unknown", None, ISO_NOW)]

# --- settled write (auth, or definitive 4xx): one immediate read, no polling ---

def test_settled_auth_unchanged_re_raises_rejection():
    store = FakeStore()
    sleeps: list[float] = []
    client = FakeClient(
        write=CommandError(ErrorCategory.AUTHENTICATION_FAILED, http_status=401),
        reads=[ABSENT_READ],
    )
    with pytest.raises(CommandError) as exc:
        _app(client, store, sleep=sleeps.append).execute(CMD)
    assert exc.value.category is ErrorCategory.AUTHENTICATION_FAILED
    assert exc.value.http_status == 401 and exc.value.mutation_attempted is True
    assert exc.value.exit_code == 3
    assert client.posts == 1 and sleeps == []
    assert store.finalized == [("rejected", 401, ISO_NOW)]


def test_settled_auth_but_tag_present_is_recovered():
    store = FakeStore()
    client = FakeClient(
        write=CommandError(ErrorCategory.AUTHORIZATION_FAILED, http_status=403),
        reads=[SUCCESS_READ],
    )
    resp = _app(client, store).execute(CMD)
    assert (resp.outcome, resp.tag_present) == ("recovered", True)
    assert store.finalized == [("recovered", 403, ISO_NOW)]


def test_settled_definitive_4xx_prior_tag_lost_is_verification_failed():
    store = FakeStore()
    sleeps: list[float] = []
    client = FakeClient(
        write=CommandError(ErrorCategory.REMOTE_REJECTED, http_status=422),
        reads=[LOST_READ],
    )
    with pytest.raises(CommandError) as exc:
        _app(client, store, sleep=sleeps.append).execute(CMD)
    assert exc.value.category is ErrorCategory.EXECUTION_VERIFICATION_FAILED
    assert client.posts == 1 and sleeps == []


def test_settled_unreadable_is_outcome_unknown():
    store = FakeStore()
    client = FakeClient(
        write=CommandError(ErrorCategory.REMOTE_REJECTED, http_status=404),
        reads=[CommandError(ErrorCategory.REMOTE_TRANSPORT_FAILED)],
    )
    with pytest.raises(CommandError) as exc:
        _app(client, store).execute(CMD)
    assert exc.value.category is ErrorCategory.EXECUTION_OUTCOME_UNKNOWN
    assert store.finalized == [("outcome_unknown", 404, ISO_NOW)]

def _advancing_clock(step=1000.0):
    """Monotonic that jumps ``step`` each call, so the post-dispatch budget is
    already spent by the first verification wait."""

    state = {"t": 0.0}

    def clock():
        v = state["t"]
        state["t"] += step
        return v

    return clock


# --- ambiguous write: polls at 1/2/4s within the post-dispatch budget ----------

def test_ambiguous_first_read_recovered():
    store = FakeStore()
    sleeps: list[float] = []
    client = FakeClient(
        write=CommandError(ErrorCategory.REMOTE_SERVER_ERROR, http_status=503),
        reads=[SUCCESS_READ],
    )
    resp = _app(client, store, sleep=sleeps.append).execute(CMD)
    assert (resp.outcome, resp.tag_present) == ("recovered", True)
    assert sleeps == [1.0] and client.verify_reads == 1
    assert store.finalized == [("recovered", 503, ISO_NOW)]


def test_ambiguous_prior_tag_lost_is_verification_failed():
    store = FakeStore()
    sleeps: list[float] = []
    client = FakeClient(
        write=CommandError(ErrorCategory.REMOTE_SERVER_ERROR, http_status=503),
        reads=[LOST_READ],
    )
    with pytest.raises(CommandError) as exc:
        _app(client, store, sleep=sleeps.append).execute(CMD)
    assert exc.value.category is ErrorCategory.EXECUTION_VERIFICATION_FAILED
    assert sleeps == [1.0]


def test_ambiguous_all_inconclusive_is_outcome_unknown():
    store = FakeStore()
    sleeps: list[float] = []
    client = FakeClient(
        write=CommandError(ErrorCategory.REMOTE_SERVER_ERROR, http_status=503),
        reads=[CommandError(ErrorCategory.REMOTE_RESPONSE_INVALID), ABSENT_READ, ABSENT_READ],
    )
    with pytest.raises(CommandError) as exc:
        _app(client, store, sleep=sleeps.append).execute(CMD)
    assert exc.value.category is ErrorCategory.EXECUTION_OUTCOME_UNKNOWN
    assert sleeps == [1.0, 2.0, 4.0] and client.verify_reads == 3


def test_ambiguous_budget_exhausted_skips_reads():
    store = FakeStore()
    sleeps: list[float] = []
    client = FakeClient(
        write=CommandError(ErrorCategory.REMOTE_SERVER_ERROR, http_status=503), reads=[],
    )
    with pytest.raises(CommandError) as exc:
        _app(client, store, monotonic=_advancing_clock(), sleep=sleeps.append).execute(CMD)
    assert exc.value.category is ErrorCategory.EXECUTION_OUTCOME_UNKNOWN
    assert sleeps == [] and client.verify_reads == 0


def test_non_definitive_4xx_takes_ambiguous_path():
    store = FakeStore()
    sleeps: list[float] = []
    client = FakeClient(
        write=CommandError(ErrorCategory.REMOTE_REJECTED, http_status=408),
        reads=[SUCCESS_READ],
    )
    resp = _app(client, store, sleep=sleeps.append).execute(CMD)
    assert resp.outcome == "recovered" and sleeps == [1.0]

