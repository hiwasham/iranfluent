"""Transcript contract tests for the project-local ``fluentcrm-tag`` skill.

They pin the trusted human-confirmation boundary as behavior regressions: the
happy path executes only the exact displayed request id, an explicit decline
cancels, and every rejected conversation (silence, ambiguity, a wrong id, an
already-attached no-op) invokes no ``execute`` — proven both at the decision
layer (``gate.decide``) and end to end against the recording fake CLI. This is
not cryptographic proof that a human approved; it guards the skill's rules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import gate

HERE = Path(__file__).resolve().parent
FAKE_CLI = HERE / "fake_cli.py"
RID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"

# (label, active_request_id, operator reply, expected decision)
SCENARIOS = [
    ("matching_id_confirmation", RID, f"Yes, confirm {RID}", gate.EXECUTE),
    ("explicit_decline", RID, "No, cancel that please", gate.CANCEL),
    ("wrong_id_approval", RID, f"Yes, go ahead with {OTHER}", gate.HOLD),
    ("ambiguous_reply", RID, "hmm maybe, I think so", gate.HOLD),
    ("affirmative_without_id", RID, "yes go ahead", gate.HOLD),
    ("id_without_affirmative", RID, f"the request id is {RID}", gate.HOLD),
    ("silence", RID, "", gate.HOLD),
    ("already_attached_noop", None, f"yes confirm {RID}", gate.HOLD),
]


@pytest.mark.parametrize("label,active,reply,expected",
                         SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_decision_matches_confirmation_rules(label, active, reply, expected):
    assert gate.decide(active, reply) == expected


def _drive_all(record: Path):
    """Run every scenario end to end, invoking the fake CLI for resolved
    commands, and return the list of recorded invocations."""

    for _label, active, reply, _expected in SCENARIOS:
        action = gate.decide(active, reply)
        if action in (gate.EXECUTE, gate.CANCEL):
            gate.invoke_cli(FAKE_CLI, action, active, record)
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines() if line]


def test_rejected_conversations_invoke_no_execute(tmp_path):
    record = tmp_path / "invocations.jsonl"
    invocations = _drive_all(record)
    commands = [inv["stdin"]["command"] for inv in invocations]
    # No rejected scenario (silence, ambiguity, wrong id, already-attached) ever
    # reaches execute; the only terminal writes are the one confirmed execute
    # and the one explicit-decline cancel.
    assert commands.count("execute") == 1
    assert commands.count("cancel") == 1
    assert len(invocations) == 2


def test_confirmed_execute_uses_exact_displayed_request_id(tmp_path):
    record = tmp_path / "invocations.jsonl"
    invocations = _drive_all(record)
    executes = [inv for inv in invocations if inv["stdin"]["command"] == "execute"]
    assert len(executes) == 1
    # Matching-request-id confirmation: the id sent equals the displayed preview
    # id, never the wrong id offered in another scenario.
    assert executes[0]["stdin"]["request_id"] == RID
    assert executes[0]["stdin"]["request_id"] != OTHER
