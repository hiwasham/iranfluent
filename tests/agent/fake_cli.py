#!/usr/bin/env python3
"""Recording fake of the ``iranfluent-tag-operator`` CLI for agent transcript tests.

It is NOT the real operator: it performs no matching, mutation, or audit. It reads
the one JSON command object on stdin, appends the full invocation (argv + parsed
stdin) as one JSON line to the file named by ``$FAKE_CLI_RECORD``, and prints one
canned JSON response so a transcript test can assert exactly which command the
skill would have invoked — and, for rejected conversations, that ``execute`` was
never invoked at all.
"""

from __future__ import annotations

import json
import os
import sys

# Canned, shape-faithful responses keyed by command. Values mirror the real
# CLI's public JSON envelopes closely enough for transcript assertions; the real
# behavior is proven by the in-process and subprocess suites, not here.
_RESPONSES = {
    "preview": {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "contact": {"id": 7, "masked_email": "d***@e***.com",
                    "full_name": "Dana", "status": "subscribed"},
        "tag": {"id": 269, "title": "set_vocab_B1", "slug": "set_vocab_b1",
                "purpose": "B1 vocabulary cohort"},
        "current_tag_ids": [1, 2], "change": "attach",
        "already_attached": False, "expires_at": "2026-08-15T12:10:00+00:00",
    },
    "execute": {
        "ok": True, "command": "execute",
        "request_id": "11111111-1111-4111-8111-111111111111",
        "outcome": "succeeded", "tag_present": True,
        "verified_at": "2026-08-15T12:05:00+00:00",
    },
    "cancel": {
        "ok": True, "command": "cancel",
        "request_id": "11111111-1111-4111-8111-111111111111",
        "outcome": "cancelled",
    },
    "reconcile": {
        "ok": True, "command": "reconcile",
        "request_id": "11111111-1111-4111-8111-111111111111",
        "outcome": "present", "tag_present": True,
        "checked_at": "2026-08-15T12:06:00+00:00",
    },
}


def main() -> int:
    raw = sys.stdin.read()
    try:
        command_obj = json.loads(raw)
    except ValueError:
        command_obj = None
    record = {"argv": sys.argv[1:], "stdin": command_obj}
    record_path = os.environ.get("FAKE_CLI_RECORD")
    if record_path:
        with open(record_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    command = command_obj.get("command") if isinstance(command_obj, dict) else None
    response = _RESPONSES.get(command, {"ok": False, "request_id": None,
                                        "error": {"category": "invalid_command",
                                                  "message": "Unsupported command."}})
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
