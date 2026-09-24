"""Unit tests for the pure contract layer in ``models.py``."""

from __future__ import annotations

import pytest

from iranfluent_tag_operator import models
from iranfluent_tag_operator.models import (
    AlreadyAttachedResponse,
    CancelResponse,
    CommandError,
    ContactView,
    ErrorCategory,
    ExecuteResponse,
    PreviewResponse,
    ReconcileResponse,
    TagView,
    exit_code_for,
)

_RECONCILE_CATEGORIES = {
    ErrorCategory.REMOTE_REJECTED,
    ErrorCategory.AUDIT_INCOMPLETE,
    ErrorCategory.EXECUTION_OUTCOME_UNKNOWN,
    ErrorCategory.EXECUTION_VERIFICATION_FAILED,
}


@pytest.mark.parametrize("category", list(ErrorCategory))
def test_every_category_has_exit_code_and_safe_message(category: ErrorCategory) -> None:
    assert category in models.EXIT_CODES
    assert exit_code_for(category) in {2, 3, 4, 5}
    message = models.SAFE_MESSAGES[category]
    assert message and message == message.strip()


@pytest.mark.parametrize("category", sorted(_RECONCILE_CATEGORIES, key=lambda c: c.value))
def test_mutation_categories_direct_reconcile(category: ErrorCategory) -> None:
    assert "Reconcile before any further action" in models.SAFE_MESSAGES[category]


def test_command_error_message_exit_and_json_shape() -> None:
    err = CommandError(ErrorCategory.TAG_NOT_ALLOWED, request_id="req-1")
    assert err.exit_code == 2
    assert err.message == models.SAFE_MESSAGES[ErrorCategory.TAG_NOT_ALLOWED]
    assert err.to_json_obj() == {
        "ok": False,
        "request_id": "req-1",
        "error": {"category": "tag_not_allowed", "message": err.message},
    }


def test_command_error_json_carries_no_extra_fields() -> None:
    err = CommandError(ErrorCategory.REMOTE_SERVER_ERROR, http_status=500, mutation_attempted=True)
    obj = err.to_json_obj()
    assert set(obj) == {"ok", "request_id", "error"}
    assert set(obj["error"]) == {"category", "message"}
    assert obj["request_id"] is None


def test_command_error_is_raisable_and_frozen() -> None:
    with pytest.raises(CommandError) as caught:
        raise CommandError(ErrorCategory.INTERNAL_ERROR)
    assert caught.value.exit_code == 5
    with pytest.raises(Exception):
        caught.value.category = ErrorCategory.INVALID_JSON  # type: ignore[misc]


def test_preview_response_is_bare_object() -> None:
    response = PreviewResponse(
        request_id="req-2",
        contact=ContactView(id=7, masked_email="a***@example.com", full_name="A B", status="subscribed"),
        tag=TagView(id=269, title="set_vocab_B1", slug="set_vocab_b1", purpose="Enable B1 vocabulary"),
        current_tag_ids=(1, 2),
        expires_at="2026-09-24T00:10:00Z",
    )
    obj = response.to_json_obj()
    assert "ok" not in obj and "command" not in obj
    assert obj["change"] == "attach"
    assert obj["already_attached"] is False
    assert obj["current_tag_ids"] == [1, 2]
    assert obj["contact"]["masked_email"] == "a***@example.com"
    assert obj["tag"]["id"] == 269


def test_already_attached_response_shape() -> None:
    obj = AlreadyAttachedResponse(
        contact=ContactView(id=7, masked_email="a***@example.com", full_name="A B", status="subscribed"),
        tag=TagView(id=269, title="set_vocab_B1", slug="set_vocab_b1", purpose="Enable B1 vocabulary"),
        current_tag_ids=(269,),
    ).to_json_obj()
    assert obj["ok"] is True
    assert obj["command"] == "preview"
    assert obj["outcome"] == "already_attached"
    assert obj["already_attached"] is True
    assert obj["current_tag_ids"] == [269]


def test_terminal_response_shapes() -> None:
    execute = ExecuteResponse(request_id="r", outcome="succeeded", tag_present=True, verified_at="t").to_json_obj()
    assert execute == {
        "ok": True, "command": "execute", "request_id": "r",
        "outcome": "succeeded", "tag_present": True, "verified_at": "t",
    }
    cancel = CancelResponse(request_id="r", outcome="cancelled").to_json_obj()
    assert cancel == {"ok": True, "command": "cancel", "request_id": "r", "outcome": "cancelled"}
    reconcile = ReconcileResponse(request_id="r", outcome="unknown", tag_present=None, checked_at="t").to_json_obj()
    assert reconcile["tag_present"] is None and reconcile["outcome"] == "unknown"
