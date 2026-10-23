"""
Links to stored files (receipt photos/PDFs, Get My Papers PDFs, accountant
uploads). The `receipts` storage bucket is private, so the database keeps
the file's location and every response gets a short-lived signed link.

  IN_APP_SECONDS  links shown inside the app (1 hour; the app refetches)
  SHARE_SECONDS   links that leave the app: CSV export, data export (30 days,
                  same as accountant dropbox links)
"""

import os
from typing import Iterable, Optional

BUCKET = "receipts"
IN_APP_SECONDS = 60 * 60
SHARE_SECONDS = 30 * 24 * 60 * 60
_MARKERS = ("/storage/v1/object/public/receipts/", "/storage/v1/object/sign/receipts/", "/storage/v1/object/receipts/")


def object_path(file_path: Optional[str]) -> Optional[str]:
    """Bucket-relative path from whatever we stored (old public-style URL
    or a bare object path)."""
    if not file_path:
        return None
    for m in _MARKERS:
        if m in file_path:
            return file_path.split(m, 1)[1].split("?", 1)[0]
    if "://" not in file_path:
        return file_path.lstrip("/")
    return None


def _absolute(url: str) -> str:
    if url.startswith("http"):
        return url
    base = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co").rstrip("/")
    return f"{base}/storage/v1{url if url.startswith('/') else '/' + url}"


def sign(supabase, file_paths: Iterable[Optional[str]], seconds: int = IN_APP_SECONDS) -> dict:
    """{stored file_path: signed URL}. Files that can't be signed are left
    out (callers keep the stored value), so one bad file never breaks a page."""
    wanted = {}
    for fp in file_paths:
        op = object_path(fp)
        if fp and op:
            wanted.setdefault(op, []).append(fp)
    if not wanted:
        return {}
    out = {}
    paths = list(wanted)
    try:
        bucket = supabase.storage.from_(BUCKET)
        for i in range(0, len(paths), 100):
            for item in bucket.create_signed_urls(paths[i:i + 100], seconds) or []:
                url = item.get("signedURL") or item.get("signedUrl") or item.get("signed_url")
                if url and item.get("path") in wanted and not item.get("error"):
                    for fp in wanted[item["path"]]:
                        out[fp] = _absolute(url)
    except Exception as e:
        print(f"[files] signing {len(paths)} file(s) failed: {e}")
    return out


def sign_one(supabase, file_path: Optional[str], seconds: int = IN_APP_SECONDS) -> Optional[str]:
    return sign(supabase, [file_path], seconds).get(file_path, file_path) if file_path else file_path


def sign_receipts(supabase, receipts: list, seconds: int = IN_APP_SECONDS) -> list:
    """Receipts in API shape (to_receipt) with filePath swapped for a signed link."""
    links = sign(supabase, [r.get("filePath") for r in receipts if r], seconds)
    for r in receipts:
        if r and r.get("filePath") in links:
            r["filePath"] = links[r["filePath"]]
    return receipts
