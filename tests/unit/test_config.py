"""Unit tests for strict configuration loading in ``config.py``."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from iranfluent_tag_operator import config
from iranfluent_tag_operator.config import Credentials
from iranfluent_tag_operator.models import CommandError, ErrorCategory, TagDefinition

UTC = timezone.utc


def _entry(**over: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "key": "vocab_b1", "id": 269, "title": "set_vocab_B1", "slug": "set_vocab_b1",
        "purpose": "Enable B1 vocabulary", "risk_note": "risk",
        "business_reviewed_at": "2026-07-30T00:00:00Z",
        "business_review_expires_at": "2026-08-29T00:00:00Z",
        "business_reviewer": "operator",
        "known_downstream_effects": ["e1", "e2"], "enabled": True,
    }
    entry.update(over)
    return entry


def _doc(**over: object) -> dict[str, object]:
    document: dict[str, object] = {"schema_version": 1, "tags": [_entry()]}
    document.update(over)
    return document


def _raw(doc: object) -> bytes:
    return json.dumps(doc).encode("utf-8")


def _assert_invalid(raw: bytes) -> None:
    with pytest.raises(CommandError) as caught:
        config.load_allowlist(raw)
    assert caught.value.category is ErrorCategory.RUNTIME_IDENTITY_INVALID
    assert caught.value.exit_code == 5


def test_load_allowlist_accepts_good_document() -> None:
    tag = config.load_allowlist(_raw(_doc()))
    assert isinstance(tag, TagDefinition)
    assert tag.key == "vocab_b1" and tag.id == 269 and tag.slug == "set_vocab_b1"
    assert tag.known_downstream_effects == ("e1", "e2")
    assert tag.enabled is True


def test_load_allowlist_file_reads_committed_file() -> None:
    tag = config.load_allowlist_file()
    assert tag.key == "vocab_b1" and tag.id == 269


def test_load_allowlist_file_missing_path_is_invalid() -> None:
    with pytest.raises(CommandError) as caught:
        config.load_allowlist_file(Path("/nonexistent/does-not-exist.json"))
    assert caught.value.category is ErrorCategory.RUNTIME_IDENTITY_INVALID


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"not json", id="malformed-json"),
        pytest.param(b"\xff\xfe", id="non-utf8"),
        pytest.param(_raw([1, 2]), id="top-level-not-object"),
        pytest.param(_raw({"schema_version": 1}), id="missing-tags"),
        pytest.param(_raw(_doc(extra="x")), id="extra-top-level-key"),
        pytest.param(_raw(_doc(schema_version=2)), id="unsupported-version"),
        pytest.param(b'{"schema_version":"1","tags":[]}', id="version-string"),
        pytest.param(_raw(_doc(schema_version=True)), id="version-bool"),
        pytest.param(_raw(_doc(tags="x")), id="tags-not-list"),
        pytest.param(_raw(_doc(tags=[])), id="tags-empty"),
        pytest.param(_raw(_doc(tags=["x"])), id="entry-not-object"),
        pytest.param(b'{"schema_version":1,"schema_version":1,"tags":[]}', id="duplicate-keys"),
    ],
)
def test_load_allowlist_rejects_structure(raw: bytes) -> None:
    _assert_invalid(raw)


def _without(field: str) -> dict[str, object]:
    entry = _entry()
    del entry[field]
    return _doc(tags=[entry])


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(_without("risk_note"), id="entry-missing-field"),
        pytest.param(_doc(tags=[_entry(unknown="x")]), id="entry-extra-field"),
        pytest.param(_doc(tags=[_entry(id="269")]), id="id-string"),
        pytest.param(_doc(tags=[_entry(id=True)]), id="id-bool"),
        pytest.param(_doc(tags=[_entry(enabled=1)]), id="enabled-int"),
        pytest.param(_doc(tags=[_entry(title=5)]), id="title-int"),
        pytest.param(_doc(tags=[_entry(known_downstream_effects="x")]), id="effects-not-list"),
        pytest.param(_doc(tags=[_entry(known_downstream_effects=[1])]), id="effects-item-not-str"),
    ],
)
def test_load_allowlist_rejects_entry_fields(raw: dict[str, object]) -> None:
    _assert_invalid(_raw(raw))


def test_rejects_duplicate_id() -> None:
    _assert_invalid(_raw(_doc(tags=[_entry(), _entry(key="other", slug="other")])))


def test_rejects_duplicate_slug_differing_only_by_case() -> None:
    dup = _entry(key="other", id=270, slug="SET_VOCAB_B1")
    _assert_invalid(_raw(_doc(tags=[_entry(), dup])))


def test_rejects_duplicate_key_differing_only_by_case() -> None:
    dup = _entry(key="VOCAB_B1", id=270, slug="other")
    _assert_invalid(_raw(_doc(tags=[_entry(), dup])))


def test_rejects_more_than_one_distinct_entry() -> None:
    second = _entry(key="other", id=270, slug="other")
    _assert_invalid(_raw(_doc(tags=[_entry(), second])))


def test_rejects_single_entry_with_wrong_key() -> None:
    _assert_invalid(_raw(_doc(tags=[_entry(key="not_vocab_b1", slug="not_vocab_b1")])))


def _tag(reviewed: str, expires: str) -> TagDefinition:
    return TagDefinition(
        key="vocab_b1", id=269, title="t", slug="s", purpose="p", risk_note="r",
        business_reviewed_at=reviewed, business_review_expires_at=expires,
        business_reviewer="operator", known_downstream_effects=(), enabled=True,
    )


def _stale(reviewed: str, expires: str, now: datetime) -> None:
    with pytest.raises(CommandError) as caught:
        config.check_approval_fresh(_tag(reviewed, expires), now)
    assert caught.value.category is ErrorCategory.BUSINESS_APPROVAL_STALE
    assert caught.value.exit_code == 2


def test_approval_fresh_within_window() -> None:
    config.check_approval_fresh(
        _tag("2026-07-30T00:00:00Z", "2026-08-29T00:00:00Z"),
        datetime(2026, 8, 15, tzinfo=UTC),
    )


def test_approval_fresh_exact_thirty_day_boundary_allowed() -> None:
    # 2026-07-30 + 30 days == 2026-08-29; the boundary is inclusive.
    config.check_approval_fresh(
        _tag("2026-07-30T00:00:00Z", "2026-08-29T00:00:00Z"),
        datetime(2026, 8, 28, tzinfo=UTC),
    )


def test_approval_window_beyond_thirty_days_rejected() -> None:
    _stale("2026-07-30T00:00:00Z", "2026-08-30T00:00:00Z", datetime(2026, 8, 1, tzinfo=UTC))


def test_approval_expired_rejected() -> None:
    _stale("2026-07-30T00:00:00Z", "2026-08-29T00:00:00Z", datetime(2026, 9, 24, tzinfo=UTC))


def test_approval_expires_not_after_reviewed_rejected() -> None:
    _stale("2026-07-30T00:00:00Z", "2026-07-01T00:00:00Z", datetime(2026, 6, 1, tzinfo=UTC))


def test_approval_malformed_timestamp_rejected() -> None:
    _stale("not-a-date", "2026-08-29T00:00:00Z", datetime(2026, 8, 1, tzinfo=UTC))


def test_approval_naive_timestamp_rejected() -> None:
    _stale("2026-07-30T00:00:00", "2026-08-29T00:00:00Z", datetime(2026, 8, 1, tzinfo=UTC))


_FULL_ENV = {
    config.ENV_BASE_URL: "https://crm.example.com",
    config.ENV_USERNAME: "operator",
    config.ENV_APP_PASSWORD: "ab cd ef gh ij kl",
    config.ENV_AUDIT_HMAC_KEY: "hmac-secret",
}


def test_load_credentials_accepts_full_env_and_preserves_password_spaces() -> None:
    creds = config.load_credentials(dict(_FULL_ENV))
    assert isinstance(creds, Credentials)
    assert creds.base_url == "https://crm.example.com"
    assert creds.app_password == "ab cd ef gh ij kl"


@pytest.mark.parametrize(
    ("missing", "category"),
    [
        (config.ENV_BASE_URL, ErrorCategory.RUNTIME_IDENTITY_INVALID),
        (config.ENV_USERNAME, ErrorCategory.RUNTIME_IDENTITY_INVALID),
        (config.ENV_APP_PASSWORD, ErrorCategory.RUNTIME_IDENTITY_INVALID),
        (config.ENV_AUDIT_HMAC_KEY, ErrorCategory.AUDIT_UNAVAILABLE),
    ],
)
def test_load_credentials_missing_var(missing: str, category: ErrorCategory) -> None:
    env = dict(_FULL_ENV)
    del env[missing]
    with pytest.raises(CommandError) as caught:
        config.load_credentials(env)
    assert caught.value.category is category


def test_load_credentials_whitespace_only_rejected() -> None:
    env = dict(_FULL_ENV, **{config.ENV_BASE_URL: "   "})
    with pytest.raises(CommandError) as caught:
        config.load_credentials(env)
    assert caught.value.category is ErrorCategory.RUNTIME_IDENTITY_INVALID


def _identity(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "package_version": "0.1.0", "git_commit": "a" * 40,
        "git_status_porcelain": "", "uv_lock_bytes": b"lock", "allowlist_bytes": b"allow",
    }
    base.update(over)
    return base


def test_build_runtime_identity_computes_digests() -> None:
    ident = config.build_runtime_identity(**_identity())  # type: ignore[arg-type]
    assert ident.git_commit == "a" * 40
    assert ident.uv_lock_sha256 == hashlib.sha256(b"lock").hexdigest()
    assert ident.allowlist_sha256 == hashlib.sha256(b"allow").hexdigest()


def test_build_runtime_identity_accepts_sha256_commit() -> None:
    ident = config.build_runtime_identity(**_identity(git_commit="b" * 64))  # type: ignore[arg-type]
    assert ident.git_commit == "b" * 64


@pytest.mark.parametrize(
    "over",
    [
        pytest.param({"package_version": "  "}, id="empty-version"),
        pytest.param({"git_commit": "abc"}, id="short-commit"),
        pytest.param({"git_commit": "g" * 40}, id="non-hex-commit"),
        pytest.param({"git_status_porcelain": " M file"}, id="dirty-worktree"),
        pytest.param({"uv_lock_bytes": b""}, id="empty-lock"),
        pytest.param({"allowlist_bytes": b""}, id="empty-allowlist"),
    ],
)
def test_build_runtime_identity_fails_closed(over: dict[str, object]) -> None:
    with pytest.raises(CommandError) as caught:
        config.build_runtime_identity(**_identity(**over))  # type: ignore[arg-type]
    assert caught.value.category is ErrorCategory.RUNTIME_IDENTITY_INVALID

