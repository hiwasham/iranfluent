#!/usr/bin/env python3
"""Tool-specific Infisical launcher for ``iranfluent-tag-operator``.

It fetches exactly the four dedicated FluentCRM manager secrets from the
self-hosted Infisical vault and injects them **only into the CLI child
process** environment, then runs the CLI with ``uv run --locked`` so stdin and
stdout pass through untouched (one JSON command in, one JSON response out).

Secrets never touch argv, are never printed, and are never inherited by the
parent agent process: they exist only in the ``child_env`` dict built here and
handed to the child via ``env=``. Every failure path fails closed and prints a
secret-free diagnostic. This reduces accidental exposure; it does not change the
version-one trusted same-user threat model (see the operator runbook).

The four secret keys in Infisical are named identically to the environment
variables the CLI reads, so the mapping is 1:1 and nothing else from the vault
is ever forwarded.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

VAULT = "http://100.116.105.2:8080"
WORKSPACE_ID = "86320c7e-5e7f-4cb7-b1f3-2a05c0afd0b7"  # project "Personal"
ENVIRONMENT = "dev"
AGENT_ENV = Path.home() / ".infisical" / "agent.env"

# Both the Infisical secret names and the child env var names the CLI reads.
REQUIRED_KEYS = (
    "IRANFLUENT_TAG_OPERATOR_BASE_URL",
    "IRANFLUENT_TAG_OPERATOR_USERNAME",
    "IRANFLUENT_TAG_OPERATOR_APP_PASSWORD",
    "IRANFLUENT_TAG_OPERATOR_AUDIT_HMAC_KEY",
)
CLI_ARGV = ("uv", "run", "--locked", "iranfluent-tag-operator")


def _api(path: str, token: str | None = None, payload: dict | None = None) -> dict:
    req = urllib.request.Request(VAULT + path)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(req, data=data, timeout=15) as resp:  # noqa: S310
        return json.loads(resp.read())


def fetch_secrets(agent_env: Path = AGENT_ENV) -> dict[str, str]:
    """Return only the required tool secrets, fetched live from Infisical."""

    env = dict(
        line.strip().split("=", 1)
        for line in agent_env.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    token = _api(
        "/api/v1/auth/universal-auth/login",
        payload={"clientId": env["INFISICAL_CLIENT_ID"],
                 "clientSecret": env["INFISICAL_CLIENT_SECRET"]},
    )["accessToken"]
    resp = _api(
        f"/api/v3/secrets/raw?workspaceId={WORKSPACE_ID}&environment={ENVIRONMENT}",
        token=token,
    )
    return {s["secretKey"]: s["secretValue"]
            for s in resp["secrets"] if s["secretKey"] in REQUIRED_KEYS}


def _default_run_child(argv, child_env) -> int:
    # Inherit stdin/stdout/stderr so the single JSON object passes through
    # untouched; secrets ride only in child_env, never on argv.
    return subprocess.run(list(argv), env=child_env).returncode


def main(argv=None, *, environ=None, fetch=fetch_secrets,
         run_child=_default_run_child, stderr=sys.stderr) -> int:
    environ = os.environ if environ is None else environ
    try:
        secrets = fetch()
    except Exception:
        # Never echo exception text: a URL or secret can hide inside it.
        print("launcher: could not load credentials from Infisical", file=stderr)
        return 5
    missing = [k for k in REQUIRED_KEYS if not secrets.get(k)]
    if missing:
        # Key names are public; only their values are secret.
        print(f"launcher: missing required secrets: {', '.join(missing)}",
              file=stderr)
        return 5
    child_env = dict(environ)
    child_env.update({k: secrets[k] for k in REQUIRED_KEYS})
    return run_child(CLI_ARGV, child_env)


if __name__ == "__main__":
    sys.exit(main())
