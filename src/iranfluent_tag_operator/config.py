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
from types import MappingProxyType

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
    # Raise plain ValueError so each loader's json.loads guard maps duplicate
    # keys to its own category (allowlist -> exit 5, contract -> exit 2).
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
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


# --- FluentCRM contract fixture (T3) ---------------------------------------
#
# The committed fixture is a machine-generated artifact of the manually
# confirmed live contract probe, never hand-edited. Any structural, schema,
# freshness, or compatibility problem fails closed as ``contract_stale``
# (exit 2): the single operator remedy for all of them is to rerun the probe.

CONTRACT_SCHEMA_VERSION = 1
MAX_CONTRACT_WINDOW = timedelta(days=30)
MAX_PAGE_LIMIT = 100
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "config" / "fluentcrm-contract.json"

_PAGINATION_MODES = frozenset({"page_number", "cursor"})
_CONTRACT_KEYS = frozenset({
    "schema_version", "generated_at", "expires_at", "site", "rest_namespace",
    "plugin_version", "endpoints", "pagination_mode", "contact_search_page_limit",
    "tag_lookup_page_limit", "response_keys", "mutation", "success_markers",
    "runtime_identity",
})
_ENDPOINT_KEYS = frozenset({"contact_search", "tag_lookup", "contact_detail", "attach_tags"})
_RESPONSE_KEY_ENDPOINTS = frozenset({"contact_search", "tag_lookup"})
_MUTATION_KEYS = frozenset({"method", "path", "body_keys"})
_SUCCESS_KEYS = frozenset({"status", "body_keys"})
_RUNTIME_IDENTITY_KEYS = frozenset({
    "package_version", "git_commit", "uv_lock_sha256", "allowlist_sha256",
})


@dataclass(frozen=True)
class ContractFixture:
    """One reviewed, immutable FluentCRM compatibility fixture."""

    schema_version: int
    generated_at: str
    expires_at: str
    site: str
    rest_namespace: str
    plugin_version: str | None
    endpoints: Mapping[str, str]
    pagination_mode: str
    contact_search_page_limit: int
    tag_lookup_page_limit: int
    response_keys: Mapping[str, tuple[str, ...]]
    mutation_method: str
    mutation_path: str
    mutation_body_keys: tuple[str, ...]
    success_status: int
    success_body_keys: tuple[str, ...]
    runtime_identity: RuntimeIdentity


def _stale_contract() -> CommandError:
    return CommandError(ErrorCategory.CONTRACT_STALE)


def _require_exact_mapping(value: object, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _stale_contract()
    return value


def _require_str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(_is_str(item) for item in value):
        raise _stale_contract()
    return tuple(value)


def _require_nonempty_str_tuple(value: object) -> tuple[str, ...]:
    result = _require_str_tuple(value)
    if not result:
        raise _stale_contract()
    return result


def _require_page_limit(value: object) -> int:
    if not _is_int(value) or value < 1 or value > MAX_PAGE_LIMIT:
        raise _stale_contract()
    return value


def _is_digest(value: object) -> bool:
    return _is_str(value) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _validate_contract_runtime_identity(value: object) -> RuntimeIdentity:
    ident = _require_exact_mapping(value, _RUNTIME_IDENTITY_KEYS)
    if not (_is_str(ident["package_version"]) and ident["package_version"].strip()):
        raise _stale_contract()
    if not _is_str(ident["git_commit"]) or not _is_full_commit(ident["git_commit"]):
        raise _stale_contract()
    if not (_is_digest(ident["uv_lock_sha256"]) and _is_digest(ident["allowlist_sha256"])):
        raise _stale_contract()
    return RuntimeIdentity(
        package_version=ident["package_version"],
        git_commit=ident["git_commit"],
        uv_lock_sha256=ident["uv_lock_sha256"],
        allowlist_sha256=ident["allowlist_sha256"],
    )


def load_contract(raw: bytes) -> ContractFixture:
    """Parse and validate the committed contract fixture bytes.

    Any decode, JSON, structural, or type failure fails closed as
    ``contract_stale``; freshness and live compatibility are separate checks.
    """

    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError):
        raise _stale_contract()
    doc = _require_exact_mapping(document, _CONTRACT_KEYS)

    if not _is_int(doc["schema_version"]) or doc["schema_version"] != CONTRACT_SCHEMA_VERSION:
        raise _stale_contract()
    if not (
        _is_str(doc["generated_at"]) and _is_str(doc["expires_at"])
        and _is_str(doc["site"]) and _is_str(doc["rest_namespace"])
    ):
        raise _stale_contract()
    plugin_version = doc["plugin_version"]
    if plugin_version is not None and not _is_str(plugin_version):
        raise _stale_contract()

    endpoints_raw = _require_exact_mapping(doc["endpoints"], _ENDPOINT_KEYS)
    if not all(_is_str(v) for v in endpoints_raw.values()):
        raise _stale_contract()

    if not _is_str(doc["pagination_mode"]) or doc["pagination_mode"] not in _PAGINATION_MODES:
        raise _stale_contract()

    response_raw = _require_exact_mapping(doc["response_keys"], _RESPONSE_KEY_ENDPOINTS)
    response_keys = {
        name: _require_nonempty_str_tuple(response_raw[name]) for name in _RESPONSE_KEY_ENDPOINTS
    }

    mutation = _require_exact_mapping(doc["mutation"], _MUTATION_KEYS)
    if not (_is_str(mutation["method"]) and _is_str(mutation["path"])):
        raise _stale_contract()

    success = _require_exact_mapping(doc["success_markers"], _SUCCESS_KEYS)
    if not _is_int(success["status"]):
        raise _stale_contract()

    return ContractFixture(
        schema_version=doc["schema_version"],
        generated_at=doc["generated_at"],
        expires_at=doc["expires_at"],
        site=doc["site"],
        rest_namespace=doc["rest_namespace"],
        plugin_version=plugin_version,
        endpoints=MappingProxyType(dict(endpoints_raw)),
        pagination_mode=doc["pagination_mode"],
        contact_search_page_limit=_require_page_limit(doc["contact_search_page_limit"]),
        tag_lookup_page_limit=_require_page_limit(doc["tag_lookup_page_limit"]),
        response_keys=MappingProxyType(response_keys),
        mutation_method=mutation["method"],
        mutation_path=mutation["path"],
        mutation_body_keys=_require_nonempty_str_tuple(mutation["body_keys"]),
        success_status=success["status"],
        success_body_keys=_require_str_tuple(success["body_keys"]),
        runtime_identity=_validate_contract_runtime_identity(doc["runtime_identity"]),
    )


def load_contract_file(path: Path = CONTRACT_PATH) -> ContractFixture:
    """Read and validate the committed contract fixture (thin impure wrapper).

    A missing fixture is a valid fail-closed state: the operator must rerun the
    live probe to produce one.
    """

    try:
        raw = path.read_bytes()
    except OSError:
        raise _stale_contract()
    return load_contract(raw)


def check_contract_fresh(fixture: ContractFixture, now: datetime) -> None:
    """Reject a fixture that is malformed-dated, over-window, or expired."""

    generated = _parse_contract_utc(fixture.generated_at)
    expires = _parse_contract_utc(fixture.expires_at)
    if expires <= generated or expires - generated > MAX_CONTRACT_WINDOW or now >= expires:
        raise _stale_contract()


def _parse_contract_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _stale_contract()
    if parsed.tzinfo is None:
        raise _stale_contract()
    return parsed.astimezone(timezone.utc)


def check_contract_compatible(
    fixture: ContractFixture,
    *,
    live_site: str,
    live_rest_namespace: str,
    live_plugin_version: str | None,
    live_response_keys: Mapping[str, Sequence[str]],
) -> None:
    """Fail closed unless the live site matches the reviewed fixture.

    Site and REST namespace must match exactly. The plugin version is compared
    only when both the fixture and the live endpoint expose one. Every reviewed
    response key must still be present on the corresponding live endpoint.
    """

    if fixture.site != live_site or fixture.rest_namespace != live_rest_namespace:
        raise _stale_contract()
    if (
        fixture.plugin_version is not None
        and live_plugin_version is not None
        and fixture.plugin_version != live_plugin_version
    ):
        raise _stale_contract()
    for endpoint, required in fixture.response_keys.items():
        observed = live_response_keys.get(endpoint)
        if observed is None or not set(required).issubset(observed):
            raise _stale_contract()

