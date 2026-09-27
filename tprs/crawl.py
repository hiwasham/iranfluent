"""Phase 1: crawl the public taxonomy pages -> out/manifest.json (no downloads)."""
import json
import os
import re
import sys
from collections import Counter, defaultdict

from bs4 import BeautifulSoup

from tprs import common

TAXONOMY_SITEMAP = "https://portal.tprsbooks.com/wp-sitemap-taxonomies-portal-taxonomy-1.xml"
LANGUAGE = "English"

# id-prefixes (8 chars, lowercase) of sitewide "ghost" files to never record.
GHOST_PREFIXES = {"1bn9ih1o", "1cu4nfcs"}

OUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "out")
RAW = os.path.join(OUT, "raw_html")


def term_pages(session):
    """Return the list of /portal-taxonomy/<term>/ URLs from the sitemap."""
    r = common.get(session, TAXONOMY_SITEMAP)
    return re.findall(r"<loc>([^<]+)</loc>", r.text)


def cache_get(session, url):
    """Fetch a term page, caching raw HTML to out/raw_html for offline re-parse."""
    os.makedirs(RAW, exist_ok=True)
    slug = url.rstrip("/").split("/")[-1]
    path = os.path.join(RAW, f"{slug}.html")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        with open(path, encoding="utf-8") as f:
            return f.read()
    html = common.get(session, url).text
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return html


def parse_page(html, term_slug):
    """Yield manifest entries from one taxonomy page."""
    soup = BeautifulSoup(html, "lxml")
    for art in soup.select("article.bde-loop-item, article.ee-post"):
        # Title = first bde-text div whose text parses as a lesson coordinate.
        coords = None
        for div in art.select("div.bde-text"):
            t = div.get_text(" ", strip=True)
            coords = common.parse_title(t)
            if coords:
                break
        if not coords:
            continue
        for a in art.find_all("a", href=True):
            kind, file_id = common.parse_drive(a["href"])
            if not file_id:
                continue
            if file_id[:8].lower() in GHOST_PREFIXES:
                continue
            label = a.get_text(" ", strip=True) or kind
            d = common.dest_dir(LANGUAGE, coords)
            yield {
                "file_id": file_id,
                "kind": kind,
                "url": a["href"],
                "label": label,
                "language": LANGUAGE,
                "chapter": coords["chapter"],
                "section_code": coords["section_code"],
                "section": coords["section"],
                "lesson": coords["lesson"],
                "dir": "/".join(d),
                "base_name": common.base_name(label, file_id),
                "source_terms": [term_slug],
            }


def main():
    session = common.make_session()
    pages = term_pages(session)
    print(f"taxonomy terms: {len(pages)}")

    by_id = {}
    for url in pages:
        slug = url.rstrip("/").split("/")[-1]
        try:
            html = cache_get(session, url)
        except Exception as e:
            print(f"  ! {slug}: {e}", file=sys.stderr)
            continue
        n = 0
        for e in parse_page(html, slug):
            n += 1
            fid = e["file_id"]
            if fid in by_id:
                by_id[fid]["source_terms"].append(slug)  # dedup, track origin
            else:
                by_id[fid] = e
        print(f"  {slug}: {n} links")

    entries = list(by_id.values())
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    # --- summary -----------------------------------------------------------
    print(f"\nunique files: {len(entries)}")
    print("by kind:", dict(Counter(e["kind"] for e in entries)))
    tree = defaultdict(lambda: defaultdict(set))
    for e in entries:
        tree[e["chapter"]][e["section"]].add(e["lesson"])
    print("\nchapter / section / #lessons:")
    for ch in sorted(tree):
        print(f"  Chapter {ch}")
        for sec in sorted(tree[ch]):
            print(f"    {sec:<16} lessons={sorted(tree[ch][sec])}")
    print(f"\nmanifest -> {os.path.join(OUT, 'manifest.json')}")


if __name__ == "__main__":
    main()
