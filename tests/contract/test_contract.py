"""Read-only tests for the FluentCRM contract fixture loader in ``config.py``.

These never touch a live FluentCRM instance. They validate parsing, freshness,
and compatibility of the committed fixture and its synthetic variants. Every
failure path is the single ``contract_stale`` (exit 2) fail-closed category.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from iranfluent_tag_operator import config
from iranfluent_tag_operator.config import ContractFixture, RuntimeIdentity
from iranfluent_tag_operator.models import CommandError, ErrorCategory

UTC = timezone.utc
_COMMIT = "a" * 40
_DIGEST = "b" * 64


def _contract(**over: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "schema_version": 1,
        "generated_at": "2026-09-24T00:00:00Z",
        "expires_at": "2026-10-24T00:00:00Z",
        "site": "https://crm.example.com",
        "rest_namespace": "fluent-crm/v2",
        "plugin_version": "3.1.5",
        "endpoints": {
            "contact_search": "/subscribers",
            "tag_lookup": "/tags",
            "contact_detail": "/subscribers/{id}",
            "attach_tags": "/subscribers/{id}",
        },
        "pagination_mode": "page_number",
        "contact_search_page_limit": 20,
        "tag_lookup_page_limit": 5,
        "response_keys": {
            "contact_search": ["id", "email", "status", "tags"],
            "tag_lookup": ["id", "title", "slug"],
        },
        "mutation": {"method": "POST", "path": "/subscribers/{id}", "body_keys": ["tags"]},
        "success_markers": {"status": 200, "body_keys": ["subscriber"]},
        "runtime_identity": {
            "package_version": "0.1.0",
            "git_commit": _COMMIT,
            "uv_lock_sha256": _DIGEST,
            "allowlist_sha256": "c" * 64,
        },
    }
    doc.update(over)
    return doc


def _identity(**over: object) -> dict[str, object]:
    base = dict(_contract()["runtime_identity"])  # type: ignore[arg-type]
    base.update(over)
    return base


def _raw(doc: object) -> bytes:
    return json.dumps(doc).encode("utf-8")


def _assert_stale(raw: bytes) -> None:
    with pytest.raises(CommandError) as caught:
        config.load_contract(raw)
    assert caught.value.category is ErrorCategory.CONTRACT_STALE
    assert caught.value.exit_code == 2
def test_load_contract_accepts_valid_fixture() -> None:
    fixture = config.load_contract(_raw(_contract()))
    assert isinstance(fixture, ContractFixture)
    assert fixture.schema_version == 1
    assert fixture.site == "https://crm.example.com"
    assert fixture.plugin_version == "3.1.5"
    assert fixture.endpoints["attach_tags"] == "/subscribers/{id}"
    assert fixture.response_keys["tag_lookup"] == ("id", "title", "slug")
    assert fixture.mutation_method == "POST"
    assert fixture.mutation_body_keys == ("tags",)
    assert fixture.success_status == 200
    assert fixture.success_body_keys == ("subscriber",)
    assert isinstance(fixture.runtime_identity, RuntimeIdentity)
    assert fixture.runtime_identity.git_commit == _COMMIT
    # Endpoint and response-key mappings are read-only proxies.
    with pytest.raises(TypeError):
        fixture.endpoints["x"] = "y"  # type: ignore[index]


def test_load_contract_accepts_null_plugin_version() -> None:
    fixture = config.load_contract(_raw(_contract(plugin_version=None)))
    assert fixture.plugin_version is None


def test_load_contract_accepts_empty_success_body_keys() -> None:
    doc = _contract(success_markers={"status": 200, "body_keys": []})
    fixture = config.load_contract(_raw(doc))
    assert fixture.success_body_keys == ()


def test_load_contract_rejects_non_utf8() -> None:
    _assert_stale(b"\xff\xfe not utf-8")


def test_load_contract_rejects_malformed_json() -> None:
    _assert_stale(b"{not json")


def test_load_contract_rejects_duplicate_keys() -> None:
    _assert_stale(b'{"schema_version": 1, "schema_version": 1}')


def test_load_contract_rejects_non_object() -> None:
    _assert_stale(_raw([1, 2, 3]))


def test_load_contract_rejects_missing_key() -> None:
    doc = _contract()
    del doc["site"]
    _assert_stale(_raw(doc))


def test_load_contract_rejects_extra_key() -> None:
    _assert_stale(_raw(_contract(extra="nope")))
def test_load_contract_rejects_non_int_schema_version() -> None:
    _assert_stale(_raw(_contract(schema_version="1")))


def test_load_contract_rejects_wrong_schema_version() -> None:
    _assert_stale(_raw(_contract(schema_version=2)))


@pytest.mark.parametrize("field", ["generated_at", "expires_at", "site", "rest_namespace"])
def test_load_contract_rejects_non_str_top_fields(field: str) -> None:
    _assert_stale(_raw(_contract(**{field: 123})))


def test_load_contract_rejects_non_str_plugin_version() -> None:
    _assert_stale(_raw(_contract(plugin_version=3)))


def test_load_contract_rejects_endpoints_non_mapping() -> None:
    _assert_stale(_raw(_contract(endpoints=["/subscribers"])))


def test_load_contract_rejects_endpoints_wrong_keys() -> None:
    _assert_stale(_raw(_contract(endpoints={"contact_search": "/subscribers"})))


def test_load_contract_rejects_endpoints_non_str_value() -> None:
    doc = _contract()
    endpoints = dict(doc["endpoints"])  # type: ignore[arg-type]
    endpoints["tag_lookup"] = 42
    _assert_stale(_raw(_contract(endpoints=endpoints)))


def test_load_contract_rejects_non_str_pagination_mode() -> None:
    _assert_stale(_raw(_contract(pagination_mode=1)))


def test_load_contract_rejects_unknown_pagination_mode() -> None:
    _assert_stale(_raw(_contract(pagination_mode="offset")))


def test_load_contract_accepts_cursor_pagination_mode() -> None:
    fixture = config.load_contract(_raw(_contract(pagination_mode="cursor")))
    assert fixture.pagination_mode == "cursor"
def test_load_contract_rejects_response_keys_wrong_keys() -> None:
    _assert_stale(_raw(_contract(response_keys={"contact_search": ["id"]})))


def test_load_contract_rejects_response_keys_non_list() -> None:
    doc = _contract(response_keys={"contact_search": "id", "tag_lookup": ["id"]})
    _assert_stale(_raw(doc))


def test_load_contract_rejects_response_keys_non_str_item() -> None:
    doc = _contract(
        response_keys={"contact_search": ["id", 2], "tag_lookup": ["id"]}
    )
    _assert_stale(_raw(doc))


def test_load_contract_rejects_response_keys_empty_list() -> None:
    doc = _contract(response_keys={"contact_search": [], "tag_lookup": ["id"]})
    _assert_stale(_raw(doc))


@pytest.mark.parametrize("limit", ["20", 0, 101])
@pytest.mark.parametrize("field", ["contact_search_page_limit", "tag_lookup_page_limit"])
def test_load_contract_rejects_bad_page_limit(field: str, limit: object) -> None:
    _assert_stale(_raw(_contract(**{field: limit})))


def test_load_contract_accepts_boundary_page_limits() -> None:
    doc = _contract(contact_search_page_limit=1, tag_lookup_page_limit=100)
    fixture = config.load_contract(_raw(doc))
    assert fixture.contact_search_page_limit == 1
    assert fixture.tag_lookup_page_limit == 100


def test_load_contract_rejects_mutation_wrong_keys() -> None:
    _assert_stale(_raw(_contract(mutation={"method": "POST", "path": "/x"})))


def test_load_contract_rejects_mutation_non_str_method() -> None:
    doc = _contract(mutation={"method": 1, "path": "/x", "body_keys": ["tags"]})
    _assert_stale(_raw(doc))


def test_load_contract_rejects_mutation_non_str_path() -> None:
    doc = _contract(mutation={"method": "POST", "path": 1, "body_keys": ["tags"]})
    _assert_stale(_raw(doc))


def test_load_contract_rejects_mutation_empty_body_keys() -> None:
    doc = _contract(mutation={"method": "POST", "path": "/x", "body_keys": []})
    _assert_stale(_raw(doc))


def test_load_contract_rejects_success_wrong_keys() -> None:
    _assert_stale(_raw(_contract(success_markers={"status": 200})))


def test_load_contract_rejects_success_non_int_status() -> None:
    doc = _contract(success_markers={"status": "200", "body_keys": []})
    _assert_stale(_raw(doc))


def test_load_contract_rejects_success_non_list_body_keys() -> None:
    doc = _contract(success_markers={"status": 200, "body_keys": "subscriber"})
    _assert_stale(_raw(doc))
def test_load_contract_rejects_runtime_identity_non_mapping() -> None:
    _assert_stale(_raw(_contract(runtime_identity=["nope"])))


def test_load_contract_rejects_runtime_identity_wrong_keys() -> None:
    _assert_stale(_raw(_contract(runtime_identity={"package_version": "0.1.0"})))


def test_load_contract_rejects_non_str_package_version() -> None:
    _assert_stale(_raw(_contract(runtime_identity=_identity(package_version=1))))


def test_load_contract_rejects_blank_package_version() -> None:
    _assert_stale(_raw(_contract(runtime_identity=_identity(package_version="  "))))


def test_load_contract_rejects_non_str_git_commit() -> None:
    _assert_stale(_raw(_contract(runtime_identity=_identity(git_commit=123))))


def test_load_contract_rejects_wrong_length_git_commit() -> None:
    _assert_stale(_raw(_contract(runtime_identity=_identity(git_commit="abc"))))


def test_load_contract_rejects_non_hex_git_commit() -> None:
    _assert_stale(_raw(_contract(runtime_identity=_identity(git_commit="z" * 40))))


def test_load_contract_accepts_64_char_git_commit() -> None:
    doc = _contract(runtime_identity=_identity(git_commit="a" * 64))
    fixture = config.load_contract(_raw(doc))
    assert len(fixture.runtime_identity.git_commit) == 64


def test_load_contract_rejects_non_str_uv_lock_digest() -> None:
    _assert_stale(_raw(_contract(runtime_identity=_identity(uv_lock_sha256=1))))


def test_load_contract_rejects_wrong_length_uv_lock_digest() -> None:
    _assert_stale(_raw(_contract(runtime_identity=_identity(uv_lock_sha256="b" * 63))))


def test_load_contract_rejects_non_hex_uv_lock_digest() -> None:
    _assert_stale(_raw(_contract(runtime_identity=_identity(uv_lock_sha256="g" * 64))))


def test_load_contract_rejects_bad_allowlist_digest() -> None:
    _assert_stale(_raw(_contract(runtime_identity=_identity(allowlist_sha256="c" * 10))))
def test_load_contract_file_missing_is_stale(tmp_path) -> None:
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(CommandError) as caught:
        config.load_contract_file(missing)
    assert caught.value.category is ErrorCategory.CONTRACT_STALE


def test_load_contract_file_reads_valid_fixture(tmp_path) -> None:
    path = tmp_path / "fluentcrm-contract.json"
    path.write_bytes(_raw(_contract()))
    fixture = config.load_contract_file(path)
    assert fixture.site == "https://crm.example.com"


def _fixture(**over: object) -> ContractFixture:
    return config.load_contract(_raw(_contract(**over)))


def test_check_contract_fresh_accepts_within_window() -> None:
    config.check_contract_fresh(_fixture(), datetime(2026, 10, 1, tzinfo=UTC))


def test_check_contract_fresh_rejects_expires_not_after_generated() -> None:
    doc = _fixture(generated_at="2026-09-24T00:00:00Z", expires_at="2026-09-24T00:00:00Z")
    with pytest.raises(CommandError):
        config.check_contract_fresh(doc, datetime(2026, 9, 24, tzinfo=UTC))


def test_check_contract_fresh_rejects_over_window() -> None:
    doc = _fixture(generated_at="2026-09-01T00:00:00Z", expires_at="2026-10-05T00:00:00Z")
    with pytest.raises(CommandError):
        config.check_contract_fresh(doc, datetime(2026, 9, 2, tzinfo=UTC))


def test_check_contract_fresh_rejects_expired() -> None:
    with pytest.raises(CommandError):
        config.check_contract_fresh(_fixture(), datetime(2026, 11, 1, tzinfo=UTC))


def test_check_contract_fresh_rejects_malformed_timestamp() -> None:
    doc = _fixture(generated_at="not-a-date")
    with pytest.raises(CommandError):
        config.check_contract_fresh(doc, datetime(2026, 10, 1, tzinfo=UTC))


def test_check_contract_fresh_rejects_naive_timestamp() -> None:
    doc = _fixture(generated_at="2026-09-24T00:00:00", expires_at="2026-10-24T00:00:00")
    with pytest.raises(CommandError):
        config.check_contract_fresh(doc, datetime(2026, 10, 1, tzinfo=UTC))
_LIVE_KEYS = {
    "contact_search": ["id", "email", "status", "tags", "extra"],
    "tag_lookup": ["id", "title", "slug"],
}


def _compat(**over: object) -> None:
    kwargs: dict[str, object] = {
        "live_site": "https://crm.example.com",
        "live_rest_namespace": "fluent-crm/v2",
        "live_plugin_version": "3.1.5",
        "live_response_keys": _LIVE_KEYS,
    }
    kwargs.update(over)
    config.check_contract_compatible(_fixture(), **kwargs)  # type: ignore[arg-type]


def test_check_contract_compatible_accepts_match() -> None:
    _compat()


def test_check_contract_compatible_rejects_site_mismatch() -> None:
    with pytest.raises(CommandError):
        _compat(live_site="https://other.example.com")


def test_check_contract_compatible_rejects_namespace_mismatch() -> None:
    with pytest.raises(CommandError):
        _compat(live_rest_namespace="fluent-crm/v1")


def test_check_contract_compatible_rejects_plugin_version_mismatch() -> None:
    with pytest.raises(CommandError):
        _compat(live_plugin_version="3.2.0")


def test_check_contract_compatible_skips_plugin_when_live_none() -> None:
    _compat(live_plugin_version=None)


def test_check_contract_compatible_skips_plugin_when_fixture_none() -> None:
    fixture = _fixture(plugin_version=None)
    config.check_contract_compatible(
        fixture,
        live_site="https://crm.example.com",
        live_rest_namespace="fluent-crm/v2",
        live_plugin_version="3.5.0",
        live_response_keys=_LIVE_KEYS,
    )


def test_check_contract_compatible_rejects_missing_endpoint() -> None:
    with pytest.raises(CommandError):
        _compat(live_response_keys={"contact_search": ["id", "email", "status", "tags"]})


def test_check_contract_compatible_rejects_key_not_subset() -> None:
    live = {"contact_search": ["id"], "tag_lookup": ["id", "title", "slug"]}
    with pytest.raises(CommandError):
        _compat(live_response_keys=live)
