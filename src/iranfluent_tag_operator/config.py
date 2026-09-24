"""Strict configuration: reviewed tag allowlist, credentials, artifact identity.

Parsing and validation take raw bytes or mappings so every branch is
unit-testable without touching the filesystem, environment, or git. A thin
impure wrapper reads the committed allowlist file. Runtime-identity gathering
that shells out to git is wired at the CLI boundary, not here.

Structural allowlist, credential, and artifact-identity failures are startup
preflight failures and raise exit-5 categories. An expired or out-of-window
business review is a pre-write policy rejection and raises the exit-2
``business_approval_stale`` category. There is no environment or command-line
override for allowlist values, the allowlist path, or preflight.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import CommandError, ErrorCategory, TagDefinition

SCHEMA_VERSION = 1
ALLOWLIST_KEY = "vocab_b1"
MAX_REVIEW_WINDOW = timedelta(days=30)

ENV_BASE_URL = "IRANFLUENT_TAG_OPERATOR_BASE_URL"
ENV_USERNAME = "IRANFLUENT_TAG_OPERATOR_USERNAME"
ENV_APP_PASSWORD = "IRANFLUENT_TAG_OPERATOR_APP_PASSWORD"
ENV_AUDIT_HMAC_KEY = "IRANFLUENT_TAG_OPERATOR_AUDIT_HMAC_KEY"

ALLOWLIST_PATH = Path(__file__).resolve().parents[2] / "config" / "tags.json"

_TOP_LEVEL_KEYS = frozenset({"schema_version", "tags"})
_TAG_KEYS = frozenset({
    "key", "id", "title", "slug", "purpose", "risk_note",
    "business_reviewed_at", "business_review_expires_at", "business_reviewer",
    "known_downstream_effects", "enabled",
})


def _invalid() -> CommandError:
    return CommandError(ErrorCategory.RUNTIME_IDENTITY_INVALID)


def _is_int(value: object) -> bool:
    # JSON booleans are ``int`` subclasses; a real int field must reject them.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _invalid()
        seen.add(key)
    return dict(pairs)


def load_allowlist(raw: bytes) -> TagDefinition:
    """Parse and validate the reviewed allowlist bytes into one TagDefinition."""

    try:
        text = raw.decode("utf-8")
        document = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError):
        raise _invalid()
    if not isinstance(document, dict) or set(document) != _TOP_LEVEL_KEYS:
        raise _invalid()
    if not _is_int(document["schema_version"]) or document["schema_version"] != SCHEMA_VERSION:
        raise _invalid()
    tags = document["tags"]
    if not isinstance(tags, list) or not tags:
        raise _invalid()
    entries = [_validate_entry(tag) for tag in tags]
    _reject_duplicate_entries(entries)
    if len(entries) != 1 or entries[0].key != ALLOWLIST_KEY:
        raise _invalid()
    return entries[0]


def _validate_entry(tag: object) -> TagDefinition:
    if not isinstance(tag, dict) or set(tag) != _TAG_KEYS:
        raise _invalid()
    if not (
        _is_str(tag["key"])
        and _is_int(tag["id"])
        and _is_str(tag["title"])
        and _is_str(tag["slug"])
        and _is_str(tag["purpose"])
        and _is_str(tag["risk_note"])
        and _is_str(tag["business_reviewed_at"])
        and _is_str(tag["business_review_expires_at"])
        and _is_str(tag["business_reviewer"])
        and isinstance(tag["enabled"], bool)
    ):
        raise _invalid()
    effects = tag["known_downstream_effects"]
    if not isinstance(effects, list) or not all(_is_str(item) for item in effects):
        raise _invalid()
    return TagDefinition(
        key=tag["key"],
        id=tag["id"],
        title=tag["title"],
        slug=tag["slug"],
        purpose=tag["purpose"],
        risk_note=tag["risk_note"],
        business_reviewed_at=tag["business_reviewed_at"],
        business_review_expires_at=tag["business_review_expires_at"],
        business_reviewer=tag["business_reviewer"],
        known_downstream_effects=tuple(effects),
        enabled=tag["enabled"],
    )


def _reject_duplicate_entries(entries: Sequence[TagDefinition]) -> None:
    keys = [entry.key.lower() for entry in entries]
    slugs = [entry.slug.lower() for entry in entries]
    ids = [entry.id for entry in entries]
    if len(set(keys)) != len(keys) or len(set(slugs)) != len(slugs) or len(set(ids)) != len(ids):
        raise _invalid()


def load_allowlist_file(path: Path = ALLOWLIST_PATH) -> TagDefinition:
    """Read and validate the committed allowlist file (thin impure wrapper)."""

    try:
        raw = path.read_bytes()
    except OSError:
        raise _invalid()
    return load_allowlist(raw)


def check_approval_fresh(tag: TagDefinition, now: datetime) -> None:
    """Reject a business review that is malformed, over-window, or expired.

    ``now`` is injected so this predicate stays pure and fully unit-testable.
    """

    reviewed = _parse_utc(tag.business_reviewed_at)
    expires = _parse_utc(tag.business_review_expires_at)
    if expires <= reviewed or expires - reviewed > MAX_REVIEW_WINDOW or now >= expires:
        raise CommandError(ErrorCategory.BUSINESS_APPROVAL_STALE)


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise CommandError(ErrorCategory.BUSINESS_APPROVAL_STALE)
    if parsed.tzinfo is None:
        raise CommandError(ErrorCategory.BUSINESS_APPROVAL_STALE)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Credentials:
    """FluentCRM connection secrets plus the audit HMAC key, all required."""

    base_url: str
    username: str
    app_password: str
    audit_hmac_key: str


def load_credentials(env: Mapping[str, str]) -> Credentials:
    """Load the four required secrets from an injected environment mapping."""

    return Credentials(
        base_url=_require_env(env, ENV_BASE_URL, ErrorCategory.RUNTIME_IDENTITY_INVALID),
        username=_require_env(env, ENV_USERNAME, ErrorCategory.RUNTIME_IDENTITY_INVALID),
        app_password=_require_env(env, ENV_APP_PASSWORD, ErrorCategory.RUNTIME_IDENTITY_INVALID),
        audit_hmac_key=_require_env(env, ENV_AUDIT_HMAC_KEY, ErrorCategory.AUDIT_UNAVAILABLE),
    )


def _require_env(env: Mapping[str, str], name: str, category: ErrorCategory) -> str:
    value = env.get(name, "")
    # Reject missing or whitespace-only, but return the raw value: a WordPress
    # application password is space-formatted and must not be trimmed.
    if not value or not value.strip():
        raise CommandError(category)
    return value


@dataclass(frozen=True)
class RuntimeIdentity:
    """Reproducible artifact identity, echoed into every audit row."""

    package_version: str
    git_commit: str
    uv_lock_sha256: str
    allowlist_sha256: str


def build_runtime_identity(
    *,
    package_version: str,
    git_commit: str,
    git_status_porcelain: str,
    uv_lock_bytes: bytes,
    allowlist_bytes: bytes,
) -> RuntimeIdentity:
    """Validate raw preflight inputs and compute the artifact identity (pure).

    A dirty worktree, a non-full commit hash, a missing version, or an empty
    lockfile/allowlist fails closed. Impure gathering (shelling to git, reading
    files) is wired at the CLI boundary and delegates here.
    """

    if not package_version.strip():
        raise _invalid()
    if not _is_full_commit(git_commit):
        raise _invalid()
    if git_status_porcelain.strip() != "":
        raise _invalid()
    if not uv_lock_bytes or not allowlist_bytes:
        raise _invalid()
    return RuntimeIdentity(
        package_version=package_version,
        git_commit=git_commit,
        uv_lock_sha256=hashlib.sha256(uv_lock_bytes).hexdigest(),
        allowlist_sha256=hashlib.sha256(allowlist_bytes).hexdigest(),
    )


def _is_full_commit(value: str) -> bool:
    return len(value) in (40, 64) and all(char in "0123456789abcdef" for char in value)

