"""Application orchestration: preview, cancel, and read-only reconciliation.

This layer owns deterministic policy sequencing. It receives its FluentCRM
client, state store, clocks, and request-ID generator as injected dependencies
and never touches HTTP, SQLite, the environment, or the terminal directly. Every
FluentCRM read is bounded by a 20-second monotonic phase deadline computed here
and passed down as an absolute ``deadline=`` float; the injected ``monotonic``
MUST be the same clock the client uses so the budget is honored end to end.

Claim-to-dispatch-to-verification pipeline (execute orchestration lands in T6;
the diagram lives here so execute sits beside it):

    preview ── create_preview ──> [active preview + request_id]
                                        |
                                  human confirmation (step 4, outside this CLI)
                                        v
    execute ── claim_preview ──> [journal: claimed]
                    |
              mark_dispatching ──> [journal: dispatching]  (write gets a 45s deadline; verification gets its own fresh 45s budget after the write returns)
                    |
              attach_tag POST (single-shot, never retried)
                    |
              fetch_contact_by_id (verify) ─┬─ tag present ──> finalize succeeded/recovered
                                             └─ unreadable ───> finalize outcome_unknown
                    |
    reconcile ── get_journal / get_preview
                    |
              dispatching w/o terminal event ──> reconcile_dispatching (close outcome_unknown)
                    |
              fetch_contact_by_id ─┬─ present ─┬─ absent ─┬─ unreadable ──> record_reconciliation
                                   present      absent      unknown
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import check_approval_fresh
from .fluentcrm import ContactRecord, FluentCrmClient
from .models import (
    OPERATION_ADD_TAG,
    AlreadyAttachedResponse,
    CancelCommand,
    CancelResponse,
    CommandError,
    ContactView,
    ErrorCategory,
    ExecuteCommand,
    ExecuteResponse,
    PreviewCommand,
    PreviewResponse,
    ReconcileCommand,
    ReconcileResponse,
    TagDefinition,
    TagView,
)
from .state import AuditFields, PreviewRecord, StateStore

OPERATION_DEADLINE = 20.0
POST_DISPATCH_DEADLINE = 45.0
SUBSCRIBED_STATUS = "subscribed"
VERIFICATION_READ_DELAYS = (1.0, 2.0, 4.0)
# Contract-defined definitive rejections: one read confirming unchanged state is
# decisive, so these need no ambiguous 1/2/4-second polling.
_DEFINITIVE_REJECT_STATUSES = frozenset({400, 404, 409, 422})


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def _mask_email(normalized: str) -> str:
    """First code point + ``***@`` + domain of an already-normalized address."""

    local, _, domain = normalized.partition("@")
    return f"{local[:1]}***@{domain}"


def _email_hmac(normalized: str, key: str) -> str:
    return hmac.new(key.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass
class _RejectCtx:
    """Audit context accumulated as resolution proceeds; fields stay None until known."""

    contact_id: int | None = None
    email_masked: str | None = None
    email_hmac: str | None = None
    tag_id: int | None = None
    tag_slug: str | None = None
    pre_write_tag_ids: tuple[int, ...] | None = None


@dataclass
class _PreviewPlan:
    """Fully resolved preview ready to commit as an active preview or a no-op."""

    contact_id: int
    status: str
    email_masked: str
    email_hmac: str
    tag_id: int
    tag_slug: str
    current_tag_ids: tuple[int, ...]
    contact_view: ContactView
    tag_view: TagView
    already_attached: bool


class Application:
    """Deterministic preview/cancel/reconcile orchestration over injected deps."""

    def __init__(
        self,
        *,
        client: FluentCrmClient,
        store: StateStore,
        tag_definition: TagDefinition,
        audit_hmac_key: str,
        now: Callable[[], datetime],
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        new_request_id: Callable[[], str],
    ) -> None:
        self._client = client
        self._store = store
        self._tag = tag_definition
        self._hmac_key = audit_hmac_key
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep
        self._new_request_id = new_request_id

    def preview(self, command: PreviewCommand) -> PreviewResponse | AlreadyAttachedResponse:
        """Resolve one add-tag preview; audit every rejection as ``preview_rejected``.

        Local policy (allowlist membership, business-approval freshness) is
        checked before any FluentCRM read so a disallowed or stale request never
        touches the network or the 20-second phase deadline.
        """

        now = self._now()
        ctx = _RejectCtx()
        try:
            plan = self._resolve_preview(command, now, ctx)
        except CommandError as exc:
            self._store.record_rejection(
                "preview_rejected",
                AuditFields(
                    operation=OPERATION_ADD_TAG,
                    contact_id=ctx.contact_id,
                    email_masked=ctx.email_masked,
                    email_hmac=ctx.email_hmac,
                    tag_id=ctx.tag_id,
                    tag_slug=ctx.tag_slug,
                    pre_write_tag_ids=ctx.pre_write_tag_ids,
                    http_status=exc.http_status,
                    error_category=exc.category.value,
                ),
                now,
            )
            raise
        if plan.already_attached:
            self._store.record_noop(
                AuditFields(
                    operation=OPERATION_ADD_TAG,
                    contact_id=plan.contact_id,
                    email_masked=plan.email_masked,
                    email_hmac=plan.email_hmac,
                    tag_id=plan.tag_id,
                    tag_slug=plan.tag_slug,
                    pre_write_tag_ids=plan.current_tag_ids,
                ),
                now,
            )
            return AlreadyAttachedResponse(plan.contact_view, plan.tag_view, plan.current_tag_ids)
        record = self._store.create_preview(
            request_id=self._new_request_id(),
            operation=OPERATION_ADD_TAG,
            contact_id=plan.contact_id,
            email_masked=plan.email_masked,
            email_hmac=plan.email_hmac,
            contact_status=plan.status,
            tag_id=plan.tag_id,
            tag_slug=plan.tag_slug,
            pre_write_tag_ids=plan.current_tag_ids,
            now=now,
        )
        return PreviewResponse(
            request_id=record.request_id,
            contact=plan.contact_view,
            tag=plan.tag_view,
            current_tag_ids=plan.current_tag_ids,
            expires_at=record.expires_at,
        )

    def _resolve_preview(
        self, command: PreviewCommand, now: datetime, ctx: _RejectCtx
    ) -> _PreviewPlan:
        email = self._normalize_email(command.email)
        tag = self._tag
        if command.tag_key != tag.key or not tag.enabled:
            raise CommandError(ErrorCategory.TAG_NOT_ALLOWED)
        ctx.tag_id = tag.id
        ctx.tag_slug = tag.slug
        check_approval_fresh(tag, now)

        deadline = self._monotonic() + OPERATION_DEADLINE
        contact = self._client.fetch_contact_by_email(email, deadline=deadline)
        if contact is None:
            raise CommandError(ErrorCategory.CONTACT_NOT_FOUND)
        normalized = contact.email.strip().lower()
        ctx.contact_id = contact.id
        ctx.email_masked = _mask_email(normalized)
        ctx.email_hmac = _email_hmac(normalized, self._hmac_key)
        ctx.pre_write_tag_ids = contact.tag_ids

        live = self._client.fetch_tag(tag.id, deadline=deadline)
        if live is None or live.id != tag.id or live.title != tag.title or live.slug != tag.slug:
            raise CommandError(ErrorCategory.TAG_DEFINITION_MISMATCH)
        if contact.status != SUBSCRIBED_STATUS:
            raise CommandError(ErrorCategory.CONTACT_STATUS_REJECTED)

        contact_view = ContactView(contact.id, ctx.email_masked, contact.full_name, contact.status)
        tag_view = TagView(tag.id, tag.title, tag.slug, tag.purpose)
        return _PreviewPlan(
            contact_id=contact.id,
            status=contact.status,
            email_masked=ctx.email_masked,
            email_hmac=ctx.email_hmac,
            tag_id=tag.id,
            tag_slug=tag.slug,
            current_tag_ids=contact.tag_ids,
            contact_view=contact_view,
            tag_view=tag_view,
            already_attached=tag.id in contact.tag_ids,
        )

    @staticmethod
    def _normalize_email(raw: str) -> str:
        normalized = raw.strip().lower()
        if not normalized or any(ch.isspace() for ch in normalized):
            raise CommandError(ErrorCategory.INVALID_EMAIL)
        return normalized

    def cancel(self, command: CancelCommand) -> CancelResponse:
        """Delegate to the store; all cancel outcome/rejection logic lives there."""

        outcome = self._store.cancel_preview(command.request_id, self._now())
        return CancelResponse(command.request_id, outcome)

    def reconcile(self, command: ReconcileCommand) -> ReconcileResponse:
        """Read-only: close a crashed ``dispatching`` journal, then report live presence."""

        now = self._now()
        journal = self._store.get_journal(command.request_id)
        if journal is None:
            self._store.record_reconciliation(command.request_id, None, now)
            return ReconcileResponse(command.request_id, "unknown", None, _iso(now))
        if journal.state == "dispatching":
            self._store.reconcile_dispatching(command.request_id, now)
        preview = self._store.get_preview(command.request_id)
        tag_present: bool | None = None
        if preview is not None:
            deadline = self._monotonic() + OPERATION_DEADLINE
            try:
                contact = self._client.fetch_contact_by_id(preview.contact_id, deadline=deadline)
                tag_present = preview.tag_id in contact.tag_ids
            except CommandError:
                tag_present = None
        self._store.record_reconciliation(command.request_id, tag_present, now)
        outcome = (
            "present" if tag_present is True
            else "absent" if tag_present is False
            else "unknown"
        )
        return ReconcileResponse(command.request_id, outcome, tag_present, _iso(now))

    def execute(self, command: ExecuteCommand) -> ExecuteResponse:
        """Claim, re-validate, dispatch a single-shot attach, verify, finalize.

        Claiming (and stranded-``dispatching`` rejection) lives in the store; a
        strand is refused as ``execution_outcome_unknown`` without any new write.
        This method owns the fresh-state re-validation gate, the exactly-once
        mutation, and the verification matrix. A dispatched write's outcome is
        decided only from this method's own post-write reads plus the raised
        category/status — never from a retry or flag inside the write call, since
        the client cannot tell an ambiguous 408/425/429 from a definitive 4xx.
        """

        claim = self._store.claim_preview(command.request_id, self._now())
        preview = claim.preview
        try:
            self._revalidate(preview)
        except CommandError as exc:
            self._store.finalize_journal(
                command.request_id, "rejected", self._now(), http_status=exc.http_status
            )
            raise
        self._store.mark_dispatching(command.request_id, self._now())
        write_deadline = self._monotonic() + POST_DISPATCH_DEADLINE
        write_error: CommandError | None = None
        try:
            self._client.attach_tag(preview.contact_id, self._tag.id, deadline=write_deadline)
        except CommandError as exc:
            write_error = exc
        return self._finalize_execution(command.request_id, preview, write_error)

    def _revalidate(self, preview: PreviewRecord) -> None:
        """Re-check policy and live state before dispatch; reject any drift.

        Business approval is re-checked (a fresh execute process must not mutate
        under an expired review), the live tag must still match the reviewed
        definition, the contact must still be subscribed, and the stored
        pre-write tag set must still match exactly. Any divergence fails closed
        before the write, within the 20-second pre-dispatch deadline.
        """

        check_approval_fresh(self._tag, self._now())
        deadline = self._monotonic() + OPERATION_DEADLINE
        live = self._client.fetch_tag(self._tag.id, deadline=deadline)
        if (
            live is None
            or live.id != self._tag.id
            or live.title != self._tag.title
            or live.slug != self._tag.slug
        ):
            raise CommandError(ErrorCategory.TAG_DEFINITION_MISMATCH)
        contact = self._client.fetch_contact_by_id(preview.contact_id, deadline=deadline)
        if contact.status != SUBSCRIBED_STATUS:
            raise CommandError(ErrorCategory.CONTACT_STATUS_REJECTED)
        if set(contact.tag_ids) != set(preview.pre_write_tag_ids):
            raise CommandError(ErrorCategory.STALE_PREVIEW)

    def _finalize_execution(
        self,
        request_id: str,
        preview: PreviewRecord,
        write_error: CommandError | None,
    ) -> ExecuteResponse:
        """Run the verification matrix for the dispatched write and finalize.

        The write response selects the read cadence: a confirmed 2xx success or a
        contract-defined settled rejection (auth, or a definitive 4xx) is decided
        by an immediate read (a confirmed 2xx then polls at 1/2/4s for
        read-replica lag); every other response polls at 1, 2, and 4 seconds. The
        verification budget is computed fresh here, after the write returns, so a
        slow write can never starve the read-back of its own full window. A
        destructive loss of a pre-write tag is reported as ``verification_failed``
        and never repaired. A post-dispatch failure to persist the terminal audit
        surfaces as ``audit_incomplete`` (mutation dispatched, record incomplete).
        """

        branch = self._write_branch(write_error)
        verify_deadline = self._monotonic() + POST_DISPATCH_DEADLINE
        if branch == "confirmed":
            token = self._verify_confirmed(preview, verify_deadline)
        elif branch == "settled":
            token = self._verify_settled(preview, verify_deadline)
        else:
            token = self._verify_ambiguous(preview, verify_deadline)

        now = self._now()
        verified_at = _iso(now)
        status = write_error.http_status if write_error is not None else None
        if token in ("succeeded", "recovered"):
            self._finalize_journal_durable(
                request_id, token, now, http_status=status, verification_ts=verified_at
            )
            return ExecuteResponse(request_id, token, True, verified_at)
        self._finalize_journal_durable(
            request_id,
            "rejected" if token == "rejected" else token,
            now,
            http_status=status,
            verification_ts=verified_at,
        )
        if token == "rejected":
            # Contract-defined rejection (or auth failure) with state confirmed
            # unchanged: the write response stands as the terminal outcome.
            raise CommandError(
                write_error.category,
                request_id=request_id,
                http_status=status,
                mutation_attempted=True,
            )
        raise CommandError(
            ErrorCategory.EXECUTION_VERIFICATION_FAILED
            if token == "verification_failed"
            else ErrorCategory.EXECUTION_OUTCOME_UNKNOWN,
            request_id=request_id,
            mutation_attempted=True,
        )

    def _finalize_journal_durable(
        self,
        request_id: str,
        outcome: str,
        now: datetime,
        *,
        http_status: int | None,
        verification_ts: str | None,
    ) -> None:
        """Persist the terminal journal event; a durability failure fails closed.

        By the time any terminal event is written the mutation has already been
        dispatched, so a lost audit row must never surface as a bland
        ``local_state_busy``. It is a dispatched-but-unaudited write:
        ``audit_incomplete`` (mutation_attempted, reconcile-directing).
        """

        try:
            self._store.finalize_journal(
                request_id, outcome, now,
                http_status=http_status, verification_ts=verification_ts,
            )
        except CommandError as exc:
            raise CommandError(
                ErrorCategory.AUDIT_INCOMPLETE,
                request_id=request_id,
                http_status=http_status,
                mutation_attempted=True,
            ) from exc

    @staticmethod
    def _write_branch(write_error: CommandError | None) -> str:
        """Classify the write response into a verification cadence.

        ``confirmed`` (2xx) and a contract-defined ``settled`` rejection (401/403
        auth, or 400/404/409/422) each take one immediate read; every other
        post-dispatch condition is ``ambiguous`` and polls at 1/2/4s.
        """

        if write_error is None:
            return "confirmed"
        if write_error.category in (
            ErrorCategory.AUTHENTICATION_FAILED,
            ErrorCategory.AUTHORIZATION_FAILED,
        ):
            return "settled"
        if (
            write_error.category is ErrorCategory.REMOTE_REJECTED
            and write_error.http_status in _DEFINITIVE_REJECT_STATUSES
        ):
            return "settled"
        return "ambiguous"

    def _verify_confirmed(self, preview: PreviewRecord, deadline: float) -> str:
        """Verify a confirmed 2xx write, polling for read-replica lag.

        The write already returned success, so the target is usually visible on
        the first (immediate) read; the 1/2/4s cadence only costs time when a
        replica lags or a read transiently fails. A destructive loss of a
        pre-write tag is decisive at once. A clean read with the target still
        absent after the full cadence is a real ``verification_failed``; only
        all-unreadable reads stay ``outcome_unknown``.
        """

        saw_absent = False
        for delay in (0.0,) + VERIFICATION_READ_DELAYS:
            if delay and not self._bounded_wait(delay, deadline):
                break
            contact = self._read_once(preview.contact_id, deadline)
            if contact is None:
                continue
            state = self._evaluate(contact.tag_ids, preview.pre_write_tag_ids)
            if state == "success":
                return "succeeded"
            if state == "verification_failed":
                return "verification_failed"
            saw_absent = True
        return "verification_failed" if saw_absent else "outcome_unknown"

    def _verify_settled(self, preview: PreviewRecord, deadline: float) -> str:
        contact = self._read_once(preview.contact_id, deadline)
        if contact is None:
            return "outcome_unknown"
        state = self._evaluate(contact.tag_ids, preview.pre_write_tag_ids)
        if state == "success":
            return "recovered"
        if state == "verification_failed":
            return "verification_failed"
        return "rejected"

    def _verify_ambiguous(self, preview: PreviewRecord, deadline: float) -> str:
        for delay in VERIFICATION_READ_DELAYS:
            if not self._bounded_wait(delay, deadline):
                break
            contact = self._read_once(preview.contact_id, deadline)
            if contact is None:
                continue
            state = self._evaluate(contact.tag_ids, preview.pre_write_tag_ids)
            if state == "verification_failed":
                return "verification_failed"
            if state == "success":
                return "recovered"
        return "outcome_unknown"

    def _evaluate(
        self, post_tag_ids: tuple[int, ...], pre_tag_ids: tuple[int, ...]
    ) -> str:
        """Classify observed post-write tags; a destructive loss beats the target check.

        Extra tags added concurrently by another actor are tolerated (audit-only,
        not surfaced): the predicate requires only ``pre ⊆ post`` and the target.
        """

        pre_set, post_set = set(pre_tag_ids), set(post_tag_ids)
        if not pre_set <= post_set:
            return "verification_failed"
        if self._tag.id in post_set:
            return "success"
        return "absent"

    def _read_once(self, contact_id: int, deadline: float) -> ContactRecord | None:
        """One single-shot verification read; a read failure is not decisive."""

        try:
            return self._client.fetch_contact_by_id(
                contact_id, deadline=deadline, retryable=False
            )
        except CommandError:
            return None

    def _bounded_wait(self, delay: float, deadline: float) -> bool:
        """Sleep ``delay`` only if the whole wait fits in the remaining budget."""

        if delay >= deadline - self._monotonic():
            return False
        self._sleep(delay)
        return True
