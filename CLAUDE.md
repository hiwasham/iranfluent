# TPRS Scraper — Project Instructions

Two-phase scraper that downloads the public TPRS Books curriculum from
`portal.tprsbooks.com` into a clean tree and syncs it to Google Drive.

## Scraping Protocol (routing rule)

For `portal.tprsbooks.com`, use `bin/tprs-scrape`. Follow the detailed domain runbook
at `.claude/protocols/tprs-scraper.md` or query GBrain (page id 2426). DO NOT reach for
`$B`, Playwright, or external crawlers — WP Engine 403s them and the content is static
server-rendered HTML.

## Layout

- `tprs/common.py` — cloudscraper session, Drive-URL + title parsing, path builders.
- `tprs/crawl.py` — Phase 1: taxonomy crawl → `out/manifest.json`.
- `tprs/download.py` — Phase 2: export Google-native docs, stream Drive binaries.
- `bin/tprs-scrape` — CLI entry point wrapping both phases.

## Detailed runbooks

Domain-specific runbooks live in `.claude/protocols/`, not in this file. Keep this file
lean; offload depth to a protocol file + GBrain.
