---
type: note
title: Iranfluent WP audit — credential access + pending vault fixes
ingested_via: 'mcp:put_page'
ingested_at: '2026-07-15T21:04:41.791Z'
source_kind: 'mcp:put_page'
tags:
  - audit
  - credentials
  - infisical
  - iranfluent
  - wordpress
---

# Iranfluent WP audit — credential access (handoff note, 2026-07-15)

**For the audit agent working on iranfluent.com. No secret values in this page — fetch live.**

## How to get credentials (runtime only, never on disk, never printed)

Prefix every Bash command that needs them:

```bash
source <(python3 /root/.infisical/creds-env.py IRANFLUENT) && <your command>
```

Env vars do NOT persist between Bash calls — source in the same call, every time.
Never print values, never write them to files. Do not ask Hiwa to paste credentials in chat.

## Current vault state (checked 2026-07-15)

- `IRANFLUENT_WP_USERNAME` exists, value currently `hiwa` — **wrong**. Hiwa is renaming it to `audit-ai` in the Infisical UI (agent machine identity is read-only, API edit returned 403).
- `IRANFLUENT_WP_PASSWORD` — **missing from vault**. Blocked until Hiwa adds it via the vault UI.

## WordPress state

- Site live: https://iranfluent.com/wp-json/ returns 200.
- `hiwa` = user id 1, publicly enumerable via REST.
- `audit-ai` — no public REST trace; almost certainly not created yet.

## Action required by audit agent

The audit plan requires the dedicated admin `audit-ai`, NOT Hiwa's personal account. On first wp-admin login (with whatever working admin credentials exist), if `audit-ai` doesn't exist: create it via wp-admin (Users → Add New, role Administrator), then have Hiwa store its password in the vault as `IRANFLUENT_WP_PASSWORD` via the Infisical UI. Then verify with:

```bash
source <(python3 /root/.infisical/creds-env.py IRANFLUENT) && [ -n "$IRANFLUENT_WP_PASSWORD" ] && echo ok
```
