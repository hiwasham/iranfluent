# IranFluent repo

Two projects live here:

- **TPRS scraper** (`tprs/`, `bin/tprs-scrape`) — downloads the public TPRS Books
  curriculum into a clean tree and syncs it to Google Drive. Documented below.
- **FluentCRM tag operator** (`src/iranfluent_tag_operator/`) — a guarded, add-only
  operator that applies one pre-approved tag to one subscribed contact. See the
  [Tag operator](#tag-operator) section below and [`docs/operator-runbook.md`](docs/operator-runbook.md).

## TPRS scraper

Two-phase scraper for the public curriculum on `portal.tprsbooks.com`.
Crawl produces a reviewable manifest; download consumes it. Idempotent.

## Why two phases
The content lives behind Google Drive/Docs links on the public taxonomy pages
(`/portal-taxonomy/<term>/`). Phase 1 maps every link into a strict hierarchy
**without downloading**, so the plan is reviewable before any bytes move. Phase 2
downloads from the manifest.

## Setup
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Phase 1 — crawl → manifest
```bash
.venv/bin/python -m tprs.crawl
```
- Reads the 27 taxonomy term pages (all render the full set; dedup by Drive file-id).
- Title grammar `C{ch}-{sec}-{lesson}` → `English/Chapter_N/<Section>/Lesson_M/`.
- Firewalls sitewide "ghost" files. Caches raw HTML in `out/raw_html/`.
- Writes `out/manifest.json` and prints a chapter/section/lesson tree.

Current result: **127 unique English (Level 1) files** — 55 Slides, 52 Drive binaries,
20 Docs. (That is the entire *public* English set; per-language lesson pages are gated.)

## Phase 2 — download
```bash
.venv/bin/python -m tprs.download                 # everything
.venv/bin/python -m tprs.download --chapter 1      # one chapter
.venv/bin/python -m tprs.download --section Slides  # one section
.venv/bin/python -m tprs.download --limit 6         # smoke test
```
- Google-native: Docs→`.docx`, Slides→`.pptx`, Sheets→`.xlsx`.
- Drive binaries: streamed; extension from `Content-Disposition` then MIME, e.g. `.pdf`/`.mp3`.
- Idempotent: skips files already present (> 2 KB). Collision-proof names `<label>_<id8>`.
- Output tree under `out/library/`; per-file log at `out/download_log.csv`.

## Push to Google Drive (rclone)
```bash
# one-time, token minted on a machine with a browser:
rclone authorize "drive"        # run on laptop, paste the printed token blob into rclone config here
rclone copy out/library/ "gdrive:TPRS_Library_Clean" -P
rclone lsf gdrive:TPRS_Library_Clean -R | wc -l   # expect 127
```

## Layout
```
tprs/common.py    # http session, drive-url + title parsing, path building
tprs/crawl.py     # phase 1
tprs/download.py  # phase 2
out/manifest.json # phase-1 output (reviewable)
out/library/      # phase-2 output (the tree)
```

## Reuse for other sites
Swap `TAXONOMY_SITEMAP`/`LANGUAGE` in `crawl.py` and the title/section grammar in
`common.py`. The download layer is generic for any Google Drive/Docs links.

## Tag operator

Guarded, add-only FluentCRM tag operator in `src/iranfluent_tag_operator/`. It only
ever **adds** one pre-approved tag (`vocab_b1`, id 269) to one exact-email, subscribed
contact — never removes or edits tags. One site, FluentCRM 3.1.5.

Run it **only through the launcher**, never the CLI directly for live work:
```bash
scripts/iranfluent_tag_launch.py < command.json
```
The launcher fetches credentials from Infisical and injects them into the CLI child
process only (`uv run --locked iranfluent-tag-operator`).

- **Credentials: Infisical only** — four keys in the vault at `http://100.116.105.2:8080`
  (project Personal, env `dev`). A missing key fails closed. Never hardcode or commit one.
- **Human confirmation** is owned by the `fluentcrm-tag` skill, not the CLI.
  Workflow: preview → confirm → execute → (cancel / reconcile).

Authoritative docs: [`docs/operator-runbook.md`](docs/operator-runbook.md).
Version history: [`CHANGELOG.md`](CHANGELOG.md).
