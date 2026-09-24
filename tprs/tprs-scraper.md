# TPRS Books Scraping Protocol (domain runbook)

Single source of truth mirrored to **GBrain page 2426**
(`projects/tprs-scraper/scraping-protocol`). Keep the two in sync: edit here, then
`mcp__gbrain__put_page` the same body to slug `projects/tprs-scraper/scraping-protocol`.

## Rule: use the Python cloudscraper pipeline, NOT a browser

For `portal.tprsbooks.com`, use `bin/tprs-scrape` (wraps `tprs/crawl.py` +
`tprs/download.py`). Do **not** use the GStack `$B` browser binary, Playwright,
Firecrawl, or Crawl4AI. All findings below are observed and verified.

- **WP Engine anti-bot:** plain `requests` → HTTP 403. `cloudscraper.create_scraper()`
  passes. A headless browser is unnecessary and slower.
- **Static server-rendered HTML:** one GET returns the full DOM. No JS render needed.
  Use structural selectors (`article.bde-loop-item` + regex on
  `docs.google.com/.../d/<id>`), not accessibility-tree `@ref` ids (those renumber).
- **Enumerate via WP REST:** `GET /wp-json/wp/v2/types` lists every custom post type;
  `X-WP-Total` response headers give per-type counts. Faster than crawling pages.
- **Public scope = English only:** served by the `/portal-taxonomy/<term>/` Breakdance
  loop widget, which renders the same fixed ~131-file English set on every term page.
  The other ~26 language tracks (Spanish, French, German, Japanese, Arabic, etc.) are
  membership-gated server-side; logged-out language pages return only footer junk
  (Privacy Policy + ghost ids `1bn9ih1o`, `1cu4nfcs`).
- **Login does not help under automation:** `wp-login.php` and the WooCommerce
  `my-account` form both 403 programmatically (WPE wall). Auth-based crawls fail.
- **Drive files are public-by-link:** the portal gates *discovery* of Google Drive
  file-ids, not the files. Once an id is known it downloads anonymously via
  `drive.google.com/uc?export=download`. Binary extension comes from the
  `Content-Disposition` filename (fallback: MIME map, then `.bin`).

## Result (2026-06-29)

131 unique public English files (20 .docx, 55 .pptx, 56 pdf/binary) harvested and
moved (not copied) to Google Drive `emilydrive:TPRS_Library_Clean` (account
`emilycunningham7864@gmail.com`, 5TB), verified with `rclone check` (0 differences).
Old Colab `TPRS_*` archive folders cleared to Drive Trash. Local copies deleted.

## Commands

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
bin/tprs-scrape probe       # confirm WPE bypass (1 request)
bin/tprs-scrape crawl       # Phase 1 -> out/manifest.json (review before downloading)
bin/tprs-scrape download    # Phase 2 -> out/library/<tree>
rclone copy out/library/ emilydrive:TPRS_Library_Clean -P
rclone check out/library/ emilydrive:TPRS_Library_Clean   # 0 differences before deleting local
```

## Layout

- `tprs/common.py` — cloudscraper session, Drive-URL + title parsing, path builders.
- `tprs/crawl.py` — Phase 1: taxonomy crawl → `out/manifest.json`.
- `tprs/download.py` — Phase 2: export Google-native docs, stream Drive binaries.
- `bin/tprs-scrape` — CLI entry point wrapping both phases.
