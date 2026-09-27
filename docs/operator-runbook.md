# Operator Runbook — iranfluent-tag-operator (tag-v0)

Add the single reviewed tag `vocab_b1` (id `269`) to **one** exact-email,
subscribed FluentCRM contact. Add-only, one site, FluentCRM 3.1.5. Nothing else
is supported in version one.

## Run it (the only supported invocation)

```bash
cd /path/to/repo            # clean, committed checkout
scripts/iranfluent_tag_launch.py < command.json
```

The launcher fetches credentials from Infisical and injects them **only** into
the CLI child process, then runs `uv run --locked iranfluent-tag-operator`. One
JSON command in on stdin, one JSON response out on stdout. Do not export the
secrets into your own shell and do not run the CLI directly for live work — that
would leave raw credentials in the parent (agent) environment.

## Configuration — credentials (Infisical only)

Never commit, hardcode, print, or paste these. They live in the self-hosted
vault (`http://100.116.105.2:8080`, project **Personal**, `dev`) under four keys
whose names match the env vars the CLI reads exactly:

| Infisical key = child env var | Contents |
| --- | --- |
| `IRANFLUENT_TAG_OPERATOR_BASE_URL` | Site REST base URL |
| `IRANFLUENT_TAG_OPERATOR_USERNAME` | Dedicated FluentCRM manager username |
| `IRANFLUENT_TAG_OPERATOR_APP_PASSWORD` | That manager's Application Password |
| `IRANFLUENT_TAG_OPERATOR_AUDIT_HMAC_KEY` | Audit HMAC key generated for this tool |

Use a **dedicated** manager with only *View Contacts* and *Manage Contacts* — do
not reuse the WordPress administrator credentials from the manual browser
workflow. The launcher forwards these four keys and nothing else from the vault;
a missing key fails closed (exit 5) before the CLI ever runs.

## Configuration — committed, non-secret

- `config/tags.json` — reviewed tag allowlist. Exactly one entry (`vocab_b1`)
  in v1. Carries business purpose, risk notes, and the time-limited review
  fields (`business_reviewed_at`, `business_review_expires_at`). The CLI
  fails closed (rejects `preview` and `execute`) once `now >=
  business_review_expires_at`, so a lapsed review must be refreshed and
  committed before any run.
- `config/fluentcrm-contract.json` — redacted API contract fixture. **Not
  committed by autonomous work**; produced by the live contract probe (T3/T11).
- `uv.lock` — committed; run with `--locked`. The CLI records package version,
  full commit hash, lockfile digest, and allowlist digest at startup.

## Workflow

1. **preview** → returns a `request_id`, masked contact, resolved tag, and
   `current_tag_ids`. If `already_attached` is true, it is a terminal no-op —
   report and stop.
2. **Confirm** (owned by the `fluentcrm-tag` skill, not the CLI): execute only on
   an explicit affirmative naming the **exact** displayed `request_id`. Silence,
   ambiguity, an affirmative without the id, or a different id → do not execute.
   An explicit decline → cancel.
3. **execute** `{"command":"execute","request_id":"…"}` → reports terminal
   `outcome` and `tag_present` after a fresh read-back.
4. **cancel** `{"command":"cancel","request_id":"…"}` on decline.
5. **reconcile** `{"command":"reconcile","request_id":"…"}` **only** when a prior
   execute returned a `mutation_attempted` error. Never re-run `execute` for that
   request id. This is defense in depth, not a fragile rule: the execution
   journal already refuses a re-run of a dispatched request as
   `execution_outcome_unknown` and never re-sends the write.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success (one JSON response on stdout) |
| 2 | Rejected: contract stale / contact not found / policy |
| 5 | Fail-closed: runtime identity invalid, audit unavailable, launcher could not load credentials, or internal error |

## Redaction guarantees

stdout and audit rows never contain credentials, raw exception text, response
bodies, database paths, full emails, phone numbers, IPs, or unrelated contact
fields. Only the CLI's `masked_email` is shown. stderr may carry a sanitized
diagnostic plus a request id. The auth header is held in memory only, never
logged. Verify with a repository/log scan: no secret value should appear
anywhere in the tree or in captured output.

## Threat model (version one)

The CLI is a deterministic policy implementation, **not** an OS-enforced security
boundary. v1 trusts the same local OS user, the project-local agent, the source
checkout, and the Infisical identity. The private SQLite state database and its
parent directory must be owned by the operator and inaccessible to other users.
A deliberate same-user caller could still modify source/allowlist, alter SQLite,
or call FluentCRM directly; preventing that needs a separate service identity or
WordPress-side enforcement and is out of scope for v1.
