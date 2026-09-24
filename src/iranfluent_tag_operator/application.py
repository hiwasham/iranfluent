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
              mark_dispatching ──> [journal: dispatching]  (starts 45s post-dispatch deadline)
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
from .fluentcrm import FluentCrmClient
from .models import (
    OPERATION_ADD_TAG,
    AlreadyAttachedResponse,
    CancelCommand,
    CancelResponse,
    CommandError,
    ContactView,
    ErrorCategory,
    PreviewCommand,
    PreviewResponse,
    ReconcileCommand,
    ReconcileResponse,
    TagDefinition,
    TagView,
)
from .state import AuditFields, StateStore

OPERATION_DEADLINE = 20.0
SUBSCRIBED_STATUS = "subscribed"


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
        new_request_id: Callable[[], str],
    ) -> None:
        self._client = client
        self._store = store
        self._tag = tag_definition
        self._hmac_key = audit_hmac_key
        self._now = now
        self._monotonic = monotonic
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
