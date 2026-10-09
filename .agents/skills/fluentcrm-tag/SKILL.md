---
name: fluentcrm-tag
description: Use when the operator asks in natural language to add the reviewed vocab_b1 tag to one FluentCRM contact — translates intent into the guarded iranfluent-tag-operator CLI, owns the human-confirmation boundary, and never mutates without explicit confirmation of the exact request ID.
---

# FluentCRM Tag Operator — project-local skill

You translate a natural-language request into the guarded `iranfluent-tag-operator`
CLI and present its results. **You are an input and presentation layer only.** All
matching, policy, mutation, verification, and audit logic lives inside the CLI;
never reimplement it, never guess a contact, tag ID, or request ID, and never
bypass a rejection.

## The only supported operation

Add the single reviewed tag `vocab_b1` to exactly one subscribed contact matched
by exact email. One site, add-only. Nothing else is in scope for version one — if
the operator asks for anything else (a different tag, removal, bulk, another site),
say it is not supported and stop.

## How you invoke the CLI

The CLI reads exactly one JSON object on stdin and writes exactly one JSON object
on stdout. Invoke it once per command:

```bash
uv run iranfluent-tag-operator
```

Never pass secrets on argv or in the JSON; the launcher supplies credentials to
the child process environment only.

## Workflow

1. **Preview.** Send:
   ```json
   {"command": "preview", "operation": "add_tag", "email": "<exact email>", "tag_key": "vocab_b1"}
   ```
   - If the response has `"already_attached": true` (a terminal `outcome`
     `already_attached`), the tag is already present. Report the verified no-op
     and STOP. Do not confirm, execute, or cancel.
   - Otherwise you get an active preview with a `request_id`, a masked contact, the
     resolved tag, and `current_tag_ids`. Present the masked contact, the tag, and
     the `request_id` to the operator and ask for explicit confirmation.

2. **Confirmation boundary (the decision you own).** Apply these rules exactly:
   - **Execute only** when the operator's reply, in this conversation, is an
     explicit affirmative **and** references the exact `request_id` you displayed.
   - **Silence** (no reply / empty) → do nothing. Never execute.
   - **Ambiguous** reply (e.g. "maybe", "I think so", an affirmative with no
     request ID) → do nothing. Ask again or stop; never execute.
   - **Wrong ID** — affirmative that cites a different request ID → do nothing.
     Never execute. Possession or mention of *a* request ID is not approval of
     *this* one.
   - **Explicit decline** ("no", "cancel", "stop", "don't") → send `cancel`.
   - When in doubt, do not execute. A missed mutation is recoverable; an
     unconfirmed one is not.

3. **Execute** (only on a confirmed match):
   ```json
   {"command": "execute", "request_id": "<the confirmed request_id>"}
   ```
   Report the terminal `outcome` (`succeeded` or `recovered`) and `tag_present`.

4. **Cancel** (on decline):
   ```json
   {"command": "cancel", "request_id": "<request_id>"}
   ```

5. **Reconcile** (only if a prior execute returned an outcome that directs you to
   reconcile — a `mutation_attempted` error):
   ```json
   {"command": "reconcile", "request_id": "<request_id>"}
   ```

## Errors and redaction

- Every failure is one JSON object: `{"ok": false, "request_id": ..., "error":
  {"category": ..., "message": ...}}`. Present the safe `message`; do not invent
  detail or expose raw exception text, response bodies, paths, or credentials.
- Show only the CLI's `masked_email`. Never echo the operator's raw email back,
  and never log or repeat any secret.
- If an error's category or message directs reconciliation (the mutation may have
  taken effect), reconcile — **never** re-run `execute` for that request ID.
- A rejection is final for that attempt. To retry legitimately, start a new
  `preview`; never fabricate or reuse a stale `request_id`.
