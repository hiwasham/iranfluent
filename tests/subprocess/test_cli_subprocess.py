"""Thin subprocess framing test: proves the real console-script contract.

Coverage does not measure subprocess execution, so the exhaustive branch matrix
lives in-process (``test_cli_main.py`` / ``test_cli_parse.py``). This single
end-to-end run proves the framing the in-process tests cannot: the real
``uv run iranfluent-tag-operator`` binary emits exactly one JSON object on
stdout, exits non-zero on a rejected run, and never leaks the injected secret
onto stdout or stderr.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "SEKRET-DO-NOT-LEAK-0123456789"
ENV = {
    "IRANFLUENT_TAG_OPERATOR_BASE_URL": "https://example.invalid",
    "IRANFLUENT_TAG_OPERATOR_USERNAME": "operator",
    "IRANFLUENT_TAG_OPERATOR_APP_PASSWORD": SECRET,
    "IRANFLUENT_TAG_OPERATOR_AUDIT_HMAC_KEY": SECRET + "-hmac",
    "PATH": __import__("os").environ.get("PATH", ""),
}
PREVIEW = (b'{"command":"preview","operation":"add_tag",'
           b'"email":"a@b.co","tag_key":"vocab_b1"}')


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not available")
def test_console_script_frames_one_object_and_hides_secret(tmp_path):
    proc = subprocess.run(
        ["uv", "run", "iranfluent-tag-operator"],
        cwd=str(REPO_ROOT), input=PREVIEW,
        env={**ENV, "XDG_STATE_HOME": str(tmp_path)},
        capture_output=True, timeout=120,
    )
    stdout, stderr = proc.stdout.decode(), proc.stderr.decode()
    # Exactly one JSON object on stdout, and it is a rejection.
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["ok"] is False
    assert proc.returncode != 0
    # The injected secret never crosses the boundary on either stream.
    assert SECRET not in stdout and SECRET not in stderr
