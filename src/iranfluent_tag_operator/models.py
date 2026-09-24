"""Immutable command, response, entity, outcome, and error types plus JSON rules.

This module is pure: no HTTP, SQLite, environment, filesystem, or terminal
access. ``cli.py`` derives every process exit code from :data:`EXIT_CODES`
here, and every error response from a single :class:`CommandError`.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from enum import Enum

OPERATION_ADD_TAG = "add_tag"
TAG_KEY_VOCAB_B1 = "vocab_b1"
CHANGE_ATTACH = "attach"


class ErrorCategory(str, Enum):
    """Closed set of normalized failure categories."""

    # Exit 2 — input or pre-write policy rejection.
    INVALID_JSON = "invalid_json"
    INVALID_COMMAND = "invalid_command"
    INVALID_EMAIL = "invalid_email"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    CONTACT_NOT_FOUND = "contact_not_found"
    CONTACT_AMBIGUOUS = "contact_ambiguous"
    TAG_NOT_ALLOWED = "tag_not_allowed"
    TAG_DEFINITION_MISMATCH = "tag_definition_mismatch"
    CONTACT_STATUS_REJECTED = "contact_status_rejected"
    PREVIEW_MISSING = "preview_missing"
    PREVIEW_EXPIRED = "preview_expired"
    PREVIEW_CONSUMED = "preview_consumed"
    STALE_PREVIEW = "stale_preview"
    CONTRACT_STALE = "contract_stale"
    BUSINESS_APPROVAL_STALE = "business_approval_stale"
    REMOTE_REJECTED = "remote_rejected"
    # Exit 3 — identity rejection.
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    # Exit 4 — remote read/protocol failure before a known mutation outcome.
    REMOTE_TRANSPORT_FAILED = "remote_transport_failed"
    REMOTE_RATE_LIMITED = "remote_rate_limited"
    REMOTE_SERVER_ERROR = "remote_server_error"
    REMOTE_RESPONSE_INVALID = "remote_response_invalid"
    OPERATION_DEADLINE_EXCEEDED = "operation_deadline_exceeded"
    # Exit 5 — local durability or mutation-outcome failure.
    RUNTIME_IDENTITY_INVALID = "runtime_identity_invalid"
    AUDIT_UNAVAILABLE = "audit_unavailable"
    LOCAL_STATE_BUSY = "local_state_busy"
    LOCAL_STATE_INVALID = "local_state_invalid"
    AUDIT_INCOMPLETE = "audit_incomplete"
    EXECUTION_OUTCOME_UNKNOWN = "execution_outcome_unknown"
    EXECUTION_VERIFICATION_FAILED = "execution_verification_failed"
    INTERNAL_ERROR = "internal_error"


EXIT_CODES: dict[ErrorCategory, int] = {
    ErrorCategory.INVALID_JSON: 2,
    ErrorCategory.INVALID_COMMAND: 2,
    ErrorCategory.INVALID_EMAIL: 2,
    ErrorCategory.UNSUPPORTED_OPERATION: 2,
    ErrorCategory.CONTACT_NOT_FOUND: 2,
    ErrorCategory.CONTACT_AMBIGUOUS: 2,
    ErrorCategory.TAG_NOT_ALLOWED: 2,
    ErrorCategory.TAG_DEFINITION_MISMATCH: 2,
    ErrorCategory.CONTACT_STATUS_REJECTED: 2,
    ErrorCategory.PREVIEW_MISSING: 2,
    ErrorCategory.PREVIEW_EXPIRED: 2,
    ErrorCategory.PREVIEW_CONSUMED: 2,
    ErrorCategory.STALE_PREVIEW: 2,
    ErrorCategory.CONTRACT_STALE: 2,
    ErrorCategory.BUSINESS_APPROVAL_STALE: 2,
    ErrorCategory.REMOTE_REJECTED: 2,
    ErrorCategory.AUTHENTICATION_FAILED: 3,
    ErrorCategory.AUTHORIZATION_FAILED: 3,
    ErrorCategory.REMOTE_TRANSPORT_FAILED: 4,
    ErrorCategory.REMOTE_RATE_LIMITED: 4,
    ErrorCategory.REMOTE_SERVER_ERROR: 4,
    ErrorCategory.REMOTE_RESPONSE_INVALID: 4,
    ErrorCategory.OPERATION_DEADLINE_EXCEEDED: 4,
    ErrorCategory.RUNTIME_IDENTITY_INVALID: 5,
    ErrorCategory.AUDIT_UNAVAILABLE: 5,
    ErrorCategory.LOCAL_STATE_BUSY: 5,
    ErrorCategory.LOCAL_STATE_INVALID: 5,
    ErrorCategory.AUDIT_INCOMPLETE: 5,
    ErrorCategory.EXECUTION_OUTCOME_UNKNOWN: 5,
    ErrorCategory.EXECUTION_VERIFICATION_FAILED: 5,
    ErrorCategory.INTERNAL_ERROR: 5,
}

# Predefined, operator-safe messages. No secrets, no raw exception text, no
# response bodies, no paths. Reconcile-directing messages are used wherever a
# mutation may already have taken effect.
_RECONCILE = " Reconcile before any further action; do not execute this request again."
SAFE_MESSAGES: dict[ErrorCategory, str] = {
    ErrorCategory.INVALID_JSON: "Input was not a single valid JSON object.",
    ErrorCategory.INVALID_COMMAND: "Unknown or malformed command.",
    ErrorCategory.INVALID_EMAIL: "Email address is malformed.",
    ErrorCategory.UNSUPPORTED_OPERATION: "Requested operation is not supported.",
    ErrorCategory.CONTACT_NOT_FOUND: "No contact matched the email address.",
    ErrorCategory.CONTACT_AMBIGUOUS: "More than one contact matched the email address.",
    ErrorCategory.TAG_NOT_ALLOWED: "Requested tag is not in the reviewed allowlist.",
    ErrorCategory.TAG_DEFINITION_MISMATCH: "Live tag no longer matches the reviewed definition.",
    ErrorCategory.CONTACT_STATUS_REJECTED: "Contact status does not permit this change.",
    ErrorCategory.PREVIEW_MISSING: "No preview exists for that request ID.",
    ErrorCategory.PREVIEW_EXPIRED: "Preview has expired; create a new preview.",
    ErrorCategory.PREVIEW_CONSUMED: "Preview has already been used.",
    ErrorCategory.STALE_PREVIEW: "Preview state no longer matches FluentCRM; create a new preview.",
    ErrorCategory.CONTRACT_STALE: "FluentCRM contract fixture is missing or expired.",
    ErrorCategory.BUSINESS_APPROVAL_STALE: "Business approval for this tag is missing or expired.",
    ErrorCategory.REMOTE_REJECTED: "FluentCRM rejected the mutation." + _RECONCILE,
    ErrorCategory.AUTHENTICATION_FAILED: "FluentCRM authentication failed.",
    ErrorCategory.AUTHORIZATION_FAILED: "FluentCRM authorization failed.",
    ErrorCategory.REMOTE_TRANSPORT_FAILED: "Could not reach FluentCRM.",
    ErrorCategory.REMOTE_RATE_LIMITED: "FluentCRM rate limited the request.",
    ErrorCategory.REMOTE_SERVER_ERROR: "FluentCRM returned a server error.",
    ErrorCategory.REMOTE_RESPONSE_INVALID: "FluentCRM returned an unreadable response.",
    ErrorCategory.OPERATION_DEADLINE_EXCEEDED: "Operation exceeded its time deadline.",
    ErrorCategory.RUNTIME_IDENTITY_INVALID: "Runtime identity preflight failed.",
    ErrorCategory.AUDIT_UNAVAILABLE: "Audit store is unavailable.",
    ErrorCategory.LOCAL_STATE_BUSY: "Local state database was busy.",
    ErrorCategory.LOCAL_STATE_INVALID: "Local state database is invalid.",
    ErrorCategory.AUDIT_INCOMPLETE: "Mutation dispatched but the audit record is incomplete." + _RECONCILE,
    ErrorCategory.EXECUTION_OUTCOME_UNKNOWN: "Mutation outcome is unknown." + _RECONCILE,
    ErrorCategory.EXECUTION_VERIFICATION_FAILED: "Post-write verification failed." + _RECONCILE,
    ErrorCategory.INTERNAL_ERROR: "An internal error occurred.",
}


def exit_code_for(category: ErrorCategory) -> int:
    """Return the single authoritative process exit code for ``category``."""

    return EXIT_CODES[category]


@dataclass(eq=False)
class CommandError(Exception):
    """Immutable, normalized failure.

    Raised at a module boundary and rendered once by :mod:`cli`. The exit code
    and operator-safe message derive only from the central maps above; callers
    never pass free text. ``mutation_attempted`` records whether a FluentCRM
    write may already have taken effect, which forces reconcile-directing
    messaging.

    The declared fields are frozen by hand rather than via ``frozen=True``:
    Python's ``@contextmanager`` sets ``__traceback__`` on the exception when a
    ``CommandError`` propagates out of a ``with`` block, which a frozen
    dataclass would reject. Guarding only the public fields keeps the contract
    immutable while letting the interpreter manage exception bookkeeping.
    """

    category: ErrorCategory
    request_id: str | None = None
    http_status: int | None = None
    mutation_attempted: bool = False

    def __setattr__(self, name: str, value: object) -> None:
        if name in _COMMAND_ERROR_FIELDS and name in self.__dict__:
            raise FrozenInstanceError(f"cannot assign to field {name!r}")
        object.__setattr__(self, name, value)

    @property
    def message(self) -> str:
        return SAFE_MESSAGES[self.category]

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.category]

    def to_json_obj(self) -> dict[str, object]:
        return {
            "ok": False,
            "request_id": self.request_id,
            "error": {"category": self.category.value, "message": self.message},
        }


_COMMAND_ERROR_FIELDS = frozenset(
    {"category", "request_id", "http_status", "mutation_attempted"}
)


@dataclass(frozen=True)
class TagDefinition:
    """One reviewed, immutable allowlist entry, loaded from ``config/tags.json``."""

    key: str
    id: int
    title: str
    slug: str
    purpose: str
    risk_note: str
    business_reviewed_at: str
    business_review_expires_at: str
    business_reviewer: str
    known_downstream_effects: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True)
class TagView:
    """Tag block echoed in the preview response (identity + purpose only)."""

    id: int
    title: str
    slug: str
    purpose: str

    def to_json_obj(self) -> dict[str, object]:
        return {"id": self.id, "title": self.title, "slug": self.slug, "purpose": self.purpose}


@dataclass(frozen=True)
class ContactView:
    """Masked contact block echoed in preview responses. Never carries a raw email."""

    id: int
    masked_email: str
    full_name: str
    status: str

    def to_json_obj(self) -> dict[str, object]:
        return {
            "id": self.id,
            "masked_email": self.masked_email,
            "full_name": self.full_name,
            "status": self.status,
        }


@dataclass(frozen=True)
class PreviewCommand:
    """Parsed ``preview`` request. ``operation`` is fixed to ``add_tag`` at parse time."""

    email: str
    tag_key: str


@dataclass(frozen=True)
class ExecuteCommand:
    request_id: str


@dataclass(frozen=True)
class CancelCommand:
    request_id: str


@dataclass(frozen=True)
class ReconcileCommand:
    request_id: str


@dataclass(frozen=True)
class PreviewResponse:
    """Active-preview response: a consumable request ID plus the exact proposed change."""

    request_id: str
    contact: ContactView
    tag: TagView
    current_tag_ids: tuple[int, ...]
    expires_at: str
    change: str = CHANGE_ATTACH
    already_attached: bool = False

    def to_json_obj(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "contact": self.contact.to_json_obj(),
            "tag": self.tag.to_json_obj(),
            "current_tag_ids": list(self.current_tag_ids),
            "change": self.change,
            "already_attached": self.already_attached,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class AlreadyAttachedResponse:
    """Terminal preview no-op: the exact tag is already attached. No consumable state."""

    contact: ContactView
    tag: TagView
    current_tag_ids: tuple[int, ...]

    def to_json_obj(self) -> dict[str, object]:
        return {
            "ok": True,
            "command": "preview",
            "outcome": "already_attached",
            "contact": self.contact.to_json_obj(),
            "tag": self.tag.to_json_obj(),
            "current_tag_ids": list(self.current_tag_ids),
            "already_attached": True,
        }


@dataclass(frozen=True)
class ExecuteResponse:
    """Terminal execute outcome. ``outcome`` is ``succeeded`` or ``recovered``."""

    request_id: str
    outcome: str
    tag_present: bool
    verified_at: str

    def to_json_obj(self) -> dict[str, object]:
        return {
            "ok": True,
            "command": "execute",
            "request_id": self.request_id,
            "outcome": self.outcome,
            "tag_present": self.tag_present,
            "verified_at": self.verified_at,
        }


@dataclass(frozen=True)
class CancelResponse:
    """Terminal cancel outcome. ``outcome`` is ``cancelled`` or ``expired``."""

    request_id: str
    outcome: str

    def to_json_obj(self) -> dict[str, object]:
        return {
            "ok": True,
            "command": "cancel",
            "request_id": self.request_id,
            "outcome": self.outcome,
        }


@dataclass(frozen=True)
class ReconcileResponse:
    """Reconcile outcome. ``outcome`` is ``present``, ``absent``, or ``unknown``.

    ``tag_present`` is ``None`` when the live state was unreadable (``unknown``).
    """

    request_id: str
    outcome: str
    tag_present: bool | None
    checked_at: str

    def to_json_obj(self) -> dict[str, object]:
        return {
            "ok": True,
            "command": "reconcile",
            "request_id": self.request_id,
            "outcome": self.outcome,
            "tag_present": self.tag_present,
            "checked_at": self.checked_at,
        }

