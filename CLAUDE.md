# IranFluent — Project Instructions

Two projects live here:

- **TPRS scraper** (`tprs/`, `bin/tprs-scrape`) — two-phase scraper that downloads the
  public TPRS Books curriculum from `portal.tprsbooks.com` into a clean tree and syncs it
  to Google Drive.
- **FluentCRM tag operator** (`src/iranfluent_tag_operator/`) — guarded, add-only operator
  that applies one pre-approved tag to one subscribed contact.

## Scraping Protocol (routing rule)

For `portal.tprsbooks.com`, use `bin/tprs-scrape`. Follow the detailed domain runbook
at `.claude/protocols/tprs-scraper.md` or query GBrain (page id 2426). DO NOT reach for
`$B`, Playwright, or external crawlers — WP Engine 403s them and the content is static
server-rendered HTML.

## iranfluent_tag_operator (routing rule)

Guarded, add-only FluentCRM tag operator in `src/iranfluent_tag_operator/`. It only ever
**adds** one pre-approved tag (`vocab_b1`, id 269) to one exact-email, subscribed contact —
never removes or edits tags. One site, FluentCRM 3.1.5.

- **Run live work ONLY via the launcher**, never the CLI directly:
  `scripts/iranfluent_tag_launch.py < command.json`. The launcher fetches Infisical
  credentials and injects them into the CLI child process only.
- **Credentials: Infisical only** — vault `http://100.116.105.2:8080`, project Personal,
  env `dev`. A missing key fails closed. Never hardcode or commit one.
- **Human confirmation** is owned by the `fluentcrm-tag` skill, not the CLI
  (preview → confirm → execute → cancel / reconcile).
- Authoritative runbook: `docs/operator-runbook.md`. Version history: `CHANGELOG.md`.

## Layout

- `tprs/common.py` — cloudscraper session, Drive-URL + title parsing, path builders.
- `tprs/crawl.py` — Phase 1: taxonomy crawl → `out/manifest.json`.
- `tprs/download.py` — Phase 2: export Google-native docs, stream Drive binaries.
- `bin/tprs-scrape` — CLI entry point wrapping both phases.

## Detailed runbooks

Domain-specific runbooks live in `.claude/protocols/`, not in this file. Keep this file
lean; offload depth to a protocol file + GBrain.
