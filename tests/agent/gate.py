"""Canonical, deterministic encoding of the skill's human-confirmation boundary.

This is test-support, not production code (it lives under ``tests/`` and is not
in the coverage source set). It mirrors, in executable form, the confirmation
rules documented in ``.agents/skills/fluentcrm-tag/SKILL.md`` so the transcript
suite can assert them as behavior regressions: an affirmative that names the
exact displayed request id executes; silence, ambiguity, a wrong id, or an
already-attached no-op never execute; an explicit decline cancels.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

EXECUTE, CANCEL, HOLD = "execute", "cancel", "hold"

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_AFFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|confirm|confirmed|approve|approved|proceed|correct|"
    r"go ahead|do it)\b"
)
_DECLINE_RE = re.compile(r"\b(no|nope|cancel|stop|don't|do not|abort|decline)\b")


def decide(active_request_id: str | None, reply: str) -> str:
    """Return the CLI action the skill may take for one operator reply.

    ``active_request_id`` is ``None`` when no consumable preview exists (e.g. a
    terminal already-attached no-op), in which case nothing is ever executed.
    """

    text = reply.strip().lower()
    if active_request_id is None or not text:
        return HOLD  # no consumable preview, or silence
    ids = set(_UUID_RE.findall(text))
    affirmative = bool(_AFFIRM_RE.search(text))
    declined = bool(_DECLINE_RE.search(text))
    if declined and not affirmative:
        return CANCEL
    if affirmative and not declined and ids == {active_request_id.lower()}:
        return EXECUTE  # explicit affirmative naming exactly this request id
    return HOLD  # ambiguous, wrong id, id-without-affirmative, or mixed signal


def invoke_cli(fake_cli: Path, command: str, request_id: str, record: Path) -> dict:
    """Invoke the recording fake CLI for a resolved command and return its JSON."""

    payload = json.dumps({"command": command, "request_id": request_id}).encode()
    proc = subprocess.run(
        [sys.executable, str(fake_cli)], input=payload,
        capture_output=True, env={"FAKE_CLI_RECORD": str(record)},
    )
    return json.loads(proc.stdout.decode())
