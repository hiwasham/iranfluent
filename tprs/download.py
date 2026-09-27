"""Phase 2: consume out/manifest.json -> download into out/library/<tree>.

Idempotent: skips files already present with non-trivial size.
Google-native docs are exported to editable formats; Drive binaries are streamed
as-is with the extension resolved from the server's Content-Type.
"""
import argparse
import csv
import json
import os
import re
import time

from tprs import common

OUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "out")
LIB = os.path.join(OUT, "library")
LOG = os.path.join(OUT, "download_log.csv")

# Google-native export targets (editable formats; flip to "pdf" if preferred).
EXPORT = {
    "gdoc": ("https://docs.google.com/document/d/{id}/export?format=docx", ".docx"),
    "gslides": ("https://docs.google.com/presentation/d/{id}/export/pptx", ".pptx"),
    "gsheet": ("https://docs.google.com/spreadsheets/d/{id}/export?format=xlsx", ".xlsx"),
}

# Content-Type -> extension for Drive binaries.
MIME_MAP = {
    "application/pdf": ".pdf",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/x-m4a": ".m4a",
    "video/mp4": ".mp4",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/zip": ".zip",
    "application/msword": ".doc",
    "application/vnd.ms-powerpoint": ".ppt",
}

MIN_BYTES = 2048  # smaller than this == almost certainly an error/HTML page


_CD_FILENAME = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', re.IGNORECASE)


def _ext_from_headers(resp, default=".bin"):
    """Resolve extension: Content-Disposition filename first, then MIME, then default.

    Drive serves binaries as application/octet-stream but puts the true name (with
    extension) in Content-Disposition, e.g. filename="… mini-stories.p1.PDF".
    """
    cd = resp.headers.get("Content-Disposition") or ""
    m = _CD_FILENAME.search(cd)
    if m:
        name = m.group(1).strip()
        _, ext = os.path.splitext(name)
        if ext and len(ext) <= 6:
            return ext.lower()
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return MIME_MAP.get(ctype, default)


def _confirm_token(resp):
    """Large Drive files return an HTML interstitial with a confirm token."""
    for k, v in resp.cookies.items():
        if k.startswith("download_warning"):
            return v
    return None


def download_binary(session, file_id, out_no_ext):
    """Stream a drive.google.com/file binary; return final path or None."""
    base = "https://drive.google.com/uc?export=download&id=" + file_id
    r = session.get(base, stream=True, timeout=60)
    token = _confirm_token(r)
    if token:
        r.close()
        r = session.get(base + "&confirm=" + token, stream=True, timeout=60)
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype:  # gated/denied -> not a real file
        r.close()
        return None, "html-response"
    ext = _ext_from_headers(r)
    path = out_no_ext + ext
    _stream_to(r, path)
    return path, None


def download_export(session, kind, file_id, out_no_ext):
    """Export a Google-native doc to an editable Office format."""
    url_tpl, ext = EXPORT[kind]
    r = session.get(url_tpl.format(id=file_id), stream=True, timeout=60)
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype:
        r.close()
        return None, "html-response"
    path = out_no_ext + ext
    _stream_to(r, path)
    return path, None


def _stream_to(resp, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        for chunk in resp.iter_content(chunk_size=16384):
            if chunk:
                f.write(chunk)
    resp.close()
    os.replace(tmp, path)


def already_done(out_no_ext):
    """True if any extension variant already exists with a real size."""
    d = os.path.dirname(out_no_ext)
    base = os.path.basename(out_no_ext)
    if not os.path.isdir(d):
        return False
    for fn in os.listdir(d):
        if fn.startswith(base) and not fn.endswith(".part"):
            if os.path.getsize(os.path.join(d, fn)) > MIN_BYTES:
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N downloads (smoke test)")
    ap.add_argument("--chapter", type=int, default=0, help="only this chapter")
    ap.add_argument("--section", default="", help="only this section (e.g. Slides)")
    ap.add_argument("--sleep", type=float, default=0.4)
    args = ap.parse_args()

    entries = json.load(open(os.path.join(OUT, "manifest.json"), encoding="utf-8"))
    if args.chapter:
        entries = [e for e in entries if e["chapter"] == args.chapter]
    if args.section:
        entries = [e for e in entries if e["section"].lower() == args.section.lower()]

    session = common.make_session()
    done = skipped = failed = 0
    rows = []
    for i, e in enumerate(entries, 1):
        out_no_ext = os.path.join(LIB, e["dir"], e["base_name"])
        if already_done(out_no_ext):
            skipped += 1
            continue
        try:
            if e["kind"] in EXPORT:
                path, err = download_export(session, e["kind"], e["file_id"], out_no_ext)
            else:
                path, err = download_binary(session, e["file_id"], out_no_ext)
        except Exception as ex:
            path, err = None, str(ex)
        if path:
            done += 1
            size = os.path.getsize(path)
            print(f"[{i}/{len(entries)}] OK {os.path.relpath(path, LIB)} ({size} B)")
            rows.append([e["file_id"], e["kind"], "ok", os.path.relpath(path, LIB), size])
        else:
            failed += 1
            print(f"[{i}/{len(entries)}] FAIL {e['kind']} {e['file_id']} :: {err}")
            rows.append([e["file_id"], e["kind"], "fail", e["dir"] + "/" + e["base_name"], err])
        time.sleep(args.sleep)
        if args.limit and done >= args.limit:
            break

    os.makedirs(OUT, exist_ok=True)
    with open(LOG, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["file_id", "kind", "status", "path", "info"], *rows])
    print(f"\ndone={done} skipped={skipped} failed={failed}  log -> {LOG}")


if __name__ == "__main__":
    main()
