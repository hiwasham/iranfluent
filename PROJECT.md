# IranFluent — Project Source of Truth

> Open this file first when you feel lost. It holds **status + next action**.
> It never duplicates the deep docs — it points to them (see *Canonical sources*).

Last updated: 2026-10-09 · Owner: Hiwa (solo)

## North star

Remove the founder from support. Turn IranFluent into an AI-native platform where
students get **content / study-plan / technical / motivation** help without routing
to Hiwa personally on Telegram.

Approved design: **gbrain page 2132** — "Phase 1: Remove the Founder from Support,"
EMPOWER methodology, generated and APPROVED via `/office-hours` on 2026-06-20.

## Why the sequence is what it is

Can't scale chaos. The AI growth engine gets built **on top of** a consolidated
stack, so consolidation (tranches 0–2) comes **before** growth (tranche 3).
CEO direction resolved 2026-09-27 via `/plan-ceo-review`: standardize on the
**Fluent suite + LearnDash**, grow only after the stack is clean.

## Where it stands today

| Thing | State |
|---|---|
| Age / model | 12 years · Persian-language + life-skills learning |
| Students | ~3,000 paying |
| Team | Hiwa, solo (was a 10-person team) |
| Core stack | WordPress + LearnDash + GamiPress + BuddyBoss + FluentCRM |
| Two installs | `my.iranfluent.com` (LMS/members — 179 plugins, 60 active) · `iranfluent.com` (marketing) |
| LMS content | 80 courses · 1,716 lessons · 2,680 topics · 1,394 questions · 43 groups |
| Audience | 18,158 WP users · 17,097 FluentCRM subscribers |
| Commerce | WooCommerce + Zarinpal — **dormant by design**, reactivated at launch |
| Gamification | GamiPress — **KEEP** (major active engine) |
| MRR goal | Growth push $500 → $7,500/mo, gated on consolidation first |

## The tranche ladder (execution)

| # | Tranche | Status |
|---|---|---|
| 0 | Current-state map + live read-only data pull | ✅ done 2026-09-27 |
| 1 | Remove 3 confirmed-dead plugins: **EDD · LearnPress · Restrict Content Pro** | ⬜ **NEXT** — live-prod write; verify what each touches first |
| 2 | WooCommerce → FluentCart migration | ⬜ parked (heaviest/riskiest; IP-coupled to payments) |
| 3 | Growth engine: flashcard 15K-vocab funnel · AI RAG tutor · retention · upsells | ⬜ parked (this is what *realizes the north star*) |

## Risk register

| Risk | Impact | State |
|---|---|---|
| Single server, one IP **49.12.129.169** (Hetzner DE, HostDL, h9.hostdl.com) | SPOF — both sites + mail + LMS fall together; the same IP is the **Zarinpal gateway whitelist**, so any migration breaks payments | ⚠️ open, never acted on (surfaced 2026-09-27) |
| cPanel programmatic login blocked (HTTP 500, Error ID `e1d9fa650044`) | Can't pull plan/quota, disk/inode, cron, backup config programmatically | ⚠️ blocked, not client-fixable |
| Tranche 1 is a **live-prod** write | Deleting a plugin that still touches live data could break a running page | ⚠️ verify-before-delete, destructive-op confirm required |

## Still-owed data (blocked)

- cPanel-gated infra: plan/quota, disk + inode, CPU/RAM/LVE, PHP/MySQL versions, cron, email accounts, backup config
- `iranfluent.com`'s own app password (marketing site — not yet obtained)
- FluentCRM per-tag / per-list subscriber counts

## Canonical sources (point, don't copy)

| What | Where |
|---|---|
| Full strategy / design doc | gbrain page **2132** (`design-docs/iranfluent/2026-06-20-root-master-design-...`) |
| Operational current state | memory `iranfluent-live-access-deferred.md` |
| Infra fingerprint | `hostdl.com.json` (0600 — **never commit**) |
| All credentials | Infisical vault `http://100.116.105.2:8080` (never in repo) |

## Next action

Resume at **Tranche 1**: for each of EDD / LearnPress / Restrict Content Pro, list
what it still touches on `my.iranfluent.com`, then deactivate → remove (one at a
time, confirm before each).
