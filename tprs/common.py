"""Shared helpers: HTTP session, Drive-URL parsing, title parsing, path building."""
import re
import time
import cloudscraper

# --- HTTP -------------------------------------------------------------------

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def make_session():
    """cloudscraper session that bypasses the portal's anti-bot 403 page."""
    s = cloudscraper.create_scraper(browser={"custom": _UA})
    return s


def get(session, url, tries=4, backoff=1.5, timeout=30):
    """GET with retry/backoff. Returns the response or raises after `tries`."""
    last = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:  # network hiccup
            last = str(e)
        time.sleep(backoff * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries ({last}): {url}")


# --- Google Drive / Docs URL parsing ----------------------------------------

# /d/<ID>  or  ?id=<ID>  or  /uc?id=<ID>
_ID_FROM_PATH = re.compile(r"/d/([A-Za-z0-9_-]{10,})")
_ID_FROM_QUERY = re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})")

# kind -> the google host/path family it came from
_KIND_BY_PATH = [
    ("gdoc", re.compile(r"docs\.google\.com/document")),
    ("gslides", re.compile(r"docs\.google\.com/presentation")),
    ("gsheet", re.compile(r"docs\.google\.com/spreadsheets")),
    ("gform", re.compile(r"docs\.google\.com/forms")),
    ("drivefile", re.compile(r"drive\.google\.com")),
    ("drivefile", re.compile(r"docs\.google\.com/uc")),
]


def parse_drive(url):
    """Return (kind, file_id) for a Google Drive/Docs URL, or (None, None)."""
    kind = None
    for k, rx in _KIND_BY_PATH:
        if rx.search(url):
            kind = k
            break
    if kind is None:
        return None, None
    m = _ID_FROM_PATH.search(url) or _ID_FROM_QUERY.search(url)
    if not m:
        return None, None
    return kind, m.group(1)


def is_google_asset(url):
    return parse_drive(url)[1] is not None


# --- Lesson title parsing ---------------------------------------------------

# Titles look like:
#   "C1-9-2 Chapter 1 Supplemental Lesson 2"
#   "C1-8-1 Variety Lesson 1"
#   "C1-2-2 Chapter 1 Slides Lesson 2"
_TITLE = re.compile(
    r"^\s*C(\d+)-(\d+)-(\d+)\b(.*)$", re.IGNORECASE
)

# Map the middle section-code to a clean folder name. The code is the second
# number in the C{ch}-{code}-{lesson} prefix and is stable across the portal.
SECTION_BY_CODE = {
    1: "TeacherGuide",
    2: "Slides",
    3: "Readings",
    4: "Activities",
    5: "Assessment",
    6: "Culture",
    7: "SongActivities",
    8: "Variety",
    9: "Supplemental",
    10: "SrJordan",
}


def parse_title(title):
    """Parse a lesson-block title into coordinates.

    Returns dict {chapter, section_code, lesson, section} or None if the title
    is not a C-prefixed lesson block.
    """
    m = _TITLE.match(title)
    if not m:
        return None
    chapter = int(m.group(1))
    section_code = int(m.group(2))
    lesson = int(m.group(3))
    tail = m.group(4)
    # Derive a human section name from the tail, fall back to the code map.
    name = re.sub(r"chapter\s*\d+", "", tail, flags=re.IGNORECASE)
    name = re.sub(r"lesson\s+\d+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[\s\-–—]+", " ", name).strip()
    if not name:
        name = SECTION_BY_CODE.get(section_code, f"Section{section_code}")
    return {
        "chapter": chapter,
        "section_code": section_code,
        "lesson": lesson,
        "section": _clean_component(name) or SECTION_BY_CODE.get(section_code, f"Section{section_code}"),
    }


# --- Filesystem-safe names --------------------------------------------------

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean_component(s):
    """Sanitise one path component (no slashes, trimmed, collapsed spaces)."""
    s = (s or "").replace("–", "-").replace("—", "-")
    s = _BAD.sub("", s).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    return s[:120]


def dest_dir(language, coords):
    """Build the nested directory for a lesson block (list of components)."""
    return [
        _clean_component(language),
        f"Chapter_{coords['chapter']}",
        coords["section"],
        f"Lesson_{coords['lesson']}",
    ]


def base_name(label, file_id):
    """Collision-proof base filename: <label>_<id8>. Extension added later."""
    lbl = _clean_component(label) or "file"
    return f"{lbl}_{file_id[:8]}"
