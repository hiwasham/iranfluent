"""Operations tests for the Infisical launcher (``scripts/iranfluent_tag_launch``).

The launcher lives under ``scripts/`` and is outside the coverage source set, so
these tests do not touch the 100% gate. They pin the T9 safety contract as
behavior regressions: fetched secrets reach only the child process environment
(never the parent, never argv, never printed), and every failure path fails
closed with a secret-free diagnostic.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_PATH = REPO_ROOT / "scripts" / "iranfluent_tag_launch.py"

_spec = importlib.util.spec_from_file_location("iranfluent_tag_launch", LAUNCHER_PATH)
launch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launch)

# Sentinel values that must never escape the child process env.
SECRETS = {
    "IRANFLUENT_TAG_OPERATOR_BASE_URL": "https://sentinel.example",
    "IRANFLUENT_TAG_OPERATOR_USERNAME": "sentinel-user",
    "IRANFLUENT_TAG_OPERATOR_APP_PASSWORD": "SENTINEL-PW-DO-NOT-LEAK-0",
    "IRANFLUENT_TAG_OPERATOR_AUDIT_HMAC_KEY": "SENTINEL-HMAC-DO-NOT-LEAK-1",
}


class _Err:
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s


def test_secrets_reach_only_child_env():
    captured = {}

    def run_child(argv, child_env):
        captured["argv"] = argv
        captured["env"] = child_env
        return 0

    err = _Err()
    parent_env = {"PATH": "/usr/bin", "HOME": "/root"}
    code = launch.main(environ=parent_env, fetch=lambda: dict(SECRETS),
                       run_child=run_child, stderr=err)

    assert code == 0
    # 1:1 mapped into the child env with the exact displayed values.
    for key, value in SECRETS.items():
        assert captured["env"][key] == value
    # Non-secret parent env is preserved for the child.
    assert captured["env"]["PATH"] == "/usr/bin"
    # Never on argv, and the child runs under uv --locked.
    assert captured["argv"] == launch.CLI_ARGV
    for value in SECRETS.values():
        assert not any(value in str(a) for a in captured["argv"])
    # Never leaked into the parent environ dict handed in.
    for value in SECRETS.values():
        assert value not in parent_env.values()
        assert value not in os.environ.values()
    # Never printed.
    assert err.text == ""


def test_missing_secret_fails_closed_and_runs_nothing():
    called = []
    err = _Err()
    partial = {k: v for k, v in SECRETS.items()
               if k != "IRANFLUENT_TAG_OPERATOR_APP_PASSWORD"}

    code = launch.main(environ={}, fetch=lambda: partial,
                       run_child=lambda *a: called.append(a) or 0, stderr=err)

    assert code == 5
    assert called == []  # child never runs without every credential
    assert "IRANFLUENT_TAG_OPERATOR_APP_PASSWORD" in err.text  # key name only
    for value in SECRETS.values():
        assert value not in err.text  # no secret value printed


def test_fetch_failure_fails_closed_without_exception_text():
    err = _Err()
    called = []

    def boom():
        raise RuntimeError("connect https://100.116.105.2 failed: SENTINEL-PW-DO-NOT-LEAK-0")

    code = launch.main(environ={}, fetch=boom,
                       run_child=lambda *a: called.append(a) or 0, stderr=err)

    assert code == 5
    assert called == []
    # The raised message carried a secret-shaped token; it must not surface.
    assert "SENTINEL-PW-DO-NOT-LEAK-0" not in err.text
    assert "100.116.105.2" not in err.text
    assert "Infisical" in err.text


def test_child_return_code_is_propagated():
    code = launch.main(environ={}, fetch=lambda: dict(SECRETS),
                       run_child=lambda argv, env: 2, stderr=_Err())
    assert code == 2
