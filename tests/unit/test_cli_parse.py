"""Unit tests for the strict stdin/JSON command parser in ``cli``.

These drive every parse branch in-process (empty/oversized/BOM/bad-UTF-8 input,
malformed/duplicate-key/non-object JSON, command dispatch, per-command field and
type validation) so the 100% branch gate does not depend on subprocess execution,
which coverage does not measure.
"""

from __future__ import annotations

import pytest

from iranfluent_tag_operator.cli import MAX_STDIN_BYTES, parse_command
from iranfluent_tag_operator.models import (
    CancelCommand,
    CommandError,
    ErrorCategory,
    ExecuteCommand,
    PreviewCommand,
    ReconcileCommand,
)

UUID = "550e8400-e29b-41d4-a716-446655440000"


def _preview(**over: object) -> bytes:
    import json
    body = {"command": "preview", "operation": "add_tag",
            "email": "a@b.co", "tag_key": "vocab_b1"}
    body.update(over)
    return json.dumps(body).encode("utf-8")


REJECTIONS = [
    (b"", ErrorCategory.INVALID_JSON),
    (b" " * (MAX_STDIN_BYTES + 1), ErrorCategory.INVALID_JSON),
    (b"\xef\xbb\xbf{}", ErrorCategory.INVALID_JSON),
    (b"\xff", ErrorCategory.INVALID_JSON),
    (b"{", ErrorCategory.INVALID_JSON),
    (b"{} 5", ErrorCategory.INVALID_JSON),
    (b'{"command":"preview","command":"preview"}', ErrorCategory.INVALID_JSON),
    (b"[]", ErrorCategory.INVALID_JSON),
    (b"5", ErrorCategory.INVALID_JSON),
    (b"null", ErrorCategory.INVALID_JSON),
    (b'{"foo":1}', ErrorCategory.INVALID_COMMAND),
    (b'{"command":5}', ErrorCategory.INVALID_COMMAND),
    (b'{"command":"nope"}', ErrorCategory.INVALID_COMMAND),
    # preview: wrong key set (extra / missing) is a structural command error
    (_preview(x=1), ErrorCategory.INVALID_COMMAND),
    (b'{"command":"preview","operation":"add_tag","email":"a@b.co"}',
     ErrorCategory.INVALID_COMMAND),
    # preview: operation not a string / not the one supported operation
    (_preview(operation=5), ErrorCategory.UNSUPPORTED_OPERATION),
    (_preview(operation="remove_tag"), ErrorCategory.UNSUPPORTED_OPERATION),
    # preview: tag_key / email must be strings
    (_preview(tag_key=5), ErrorCategory.INVALID_COMMAND),
    (_preview(email=5), ErrorCategory.INVALID_COMMAND),
    # preview: email shape
    (_preview(email="a" * 255 + "@b.co"), ErrorCategory.INVALID_EMAIL),
    (_preview(email="abc"), ErrorCategory.INVALID_EMAIL),
    (_preview(email="a@b@c"), ErrorCategory.INVALID_EMAIL),
    (_preview(email="@b.co"), ErrorCategory.INVALID_EMAIL),
    (_preview(email="a@"), ErrorCategory.INVALID_EMAIL),
    # id commands: wrong key set / missing / non-string / non-UUIDv4 request_id
    (b'{"command":"execute","request_id":"' + UUID.encode() + b'","x":1}',
     ErrorCategory.INVALID_COMMAND),
    (b'{"command":"execute"}', ErrorCategory.INVALID_COMMAND),
    (b'{"command":"execute","request_id":5}', ErrorCategory.INVALID_COMMAND),
    (b'{"command":"execute","request_id":"not-a-uuid"}',
     ErrorCategory.INVALID_COMMAND),
    (b'{"command":"execute","request_id":"' + UUID.upper().encode() + b'"}',
     ErrorCategory.INVALID_COMMAND),
]


@pytest.mark.parametrize("raw,category", REJECTIONS)
def test_parse_rejections(raw: bytes, category: ErrorCategory) -> None:
    with pytest.raises(CommandError) as exc:
        parse_command(raw)
    assert exc.value.category is category


def test_parse_preview_success() -> None:
    cmd = parse_command(_preview())
    assert isinstance(cmd, PreviewCommand)
    assert cmd.email == "a@b.co"
    assert cmd.tag_key == "vocab_b1"


@pytest.mark.parametrize("command,cls", [
    ("execute", ExecuteCommand),
    ("cancel", CancelCommand),
    ("reconcile", ReconcileCommand),
])
def test_parse_id_command_success(command: str, cls: type) -> None:
    raw = b'{"command":"' + command.encode() + b'","request_id":"' + UUID.encode() + b'"}'
    cmd = parse_command(raw)
    assert isinstance(cmd, cls)
    assert cmd.request_id == UUID
