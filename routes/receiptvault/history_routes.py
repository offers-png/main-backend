"""
Receipt History — every receipt in month folders, search across all months,
and "Download month" (ZIP of the photos/PDFs + CSV + accountant PDF).

  GET /api/history/months?scope=store|all
        -> {"months": [{"month": "2026-02", "count": 34, "total": 8412.5}]}
  GET /api/history/receipts?scope=&month=YYYY-MM&q=&start=&end=&category=
        &min=&max=&page=1&pageSize=50
        -> {"items": [...], "total": 123, "page": 1, "pageSize": 50}
  GET /api/history/months/{YYYY-MM}/download?scope=&kind=zip|pdf

A receipt's month comes from receipts.history_date, a stored generated
column = coalesce(receipt_date, uploaded_at::date), indexed with
(business_id, history_date desc). scope=all is the owner's every store.
"""

import csv
import io
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from routes.receiptvault.routes import get_current_user, get_supabase as _rv_get_supabase, to_receipt
from routes.receiptvault.stores import resolve_store, owned_stores
from routes.receiptvault.categories import LINE_LABELS, line_for
from routes.receiptvault.files import BUCKET, object_path, sign, IN_APP_SECONDS

history_routes = APIRouter(prefix="/api/history", tags=["history"])

MAX_PAGE_SIZE = 100
_CHUNK = 1000  # PostgREST returns at most 1000 rows per request
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def get_supabase():
    return _rv_get_supabase()


def _stores(supabase, user_id: str, scope: str) -> list:
    business, role, modules = resolve_store(supabase, user_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    if role != "owner" and "receipts" not in (modules or []):
        raise HTTPException(status_code=403, detail="You don't have access to this. Ask the owner to add it on the Team page.")
    if scope == "all":
        if role != "owner":
            raise HTTPException(status_code=403, detail="Only the owner can see all stores.")
        return owned_stores(supabase, user_id)
    return [business]


def _date(v: Optional[str], name: str) -> Optional[str]:
    if not v:
        return None
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD")
    return v


def _month_bounds(month: str):
    if not _MONTH_RE.match(month or ""):
        raise HTTPException(status_code=400, detail="Month must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:])
    start = date(y, m, 1)
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return start.isoformat(), end.isoformat()  # end is exclusive


def _amount(v: Optional[str]) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _search_clause(q: str) -> str:
    """PostgREST or=() for the search box: merchant, notes, category, file
    name, and an exact amount when the text is a number. Values are quoted,
    and quote/backslash are dropped, so the text can't change the filter."""
    text = re.sub(r'["\\]', "", q).strip()
    like = f'"%{text}%"'
    parts = [f"merchant.ilike.{like}", f"notes.ilike.{like}", f"category.ilike.{like}", f"original_name.ilike.{like}"]
    amt = _amount(text)
    if amt is not None:
        parts.append(f"amount.eq.{amt}")
    return ",".join(parts)


def _filtered(supabase, store_ids: list, *, month=None, q=None, start=None, end=None, category=None, min_amt=None, max_amt=None, count=False):
    query = supabase.table("receipts").select("*", count="exact" if count else None).in_("business_id", store_ids)
    if month:
        m_start, m_end = _month_bounds(month)
        query = query.gte("history_date", m_start).lt("history_date", m_end)
    if start:
        query = query.gte("history_date", start)
    if end:
        query = query.lte("history_date", end)
    if category:
        query = query.eq("category", category)
    if min_amt is not None:
        query = query.gte("amount", min_amt)
    if max_amt is not None:
        query = query.lte("amount", max_amt)
    if q and q.strip():
        query = query.or_(_search_clause(q))
    return query.order("history_date", desc=True).order("uploaded_at", desc=True)


def _all_rows(make_query) -> list:
    rows, offset = [], 0
    while True:
        chunk = make_query().range(offset, offset + _CHUNK - 1).execute().data or []
        rows.extend(chunk)
        if len(chunk) < _CHUNK:
            return rows
        offset += _CHUNK


def month_summaries(supabase, store_ids: list) -> list:
    """[{month, count, total}] newest first. Grouped in the database
    (rk_receipt_months); if that function is missing, grouped here from
    just the two columns needed."""
    try:
        data = supabase.rpc("rk_receipt_months", {"p_business_ids": store_ids}).execute().data or []
        return [{"month": r["month"], "count": int(r["receipt_count"]), "total": round(float(r["total"] or 0), 2)} for r in data]
    except Exception as e:
        print(f"[history] rk_receipt_months unavailable, grouping in Python: {e}")
    groups = {}
    rows = _all_rows(lambda: supabase.table("receipts").select("history_date,amount").in_("business_id", store_ids).order("history_date", desc=True))
    for r in rows:
        if not r.get("history_date"):
            continue
        g = groups.setdefault(str(r["history_date"])[:7], {"count": 0, "total": 0.0})
        g["count"] += 1
        g["total"] += float(r.get("amount") or 0)
    return [{"month": k, "count": v["count"], "total": round(v["total"], 2)} for k, v in sorted(groups.items(), reverse=True)]


def _item(row: dict, store_names: dict) -> dict:
    out = to_receipt(row)
    hd = str(row.get("history_date") or row.get("receipt_date") or str(row.get("uploaded_at") or "")[:10])
    out["historyDate"] = hd
    out["month"] = hd[:7]
    out["storeName"] = store_names.get(row.get("business_id"))
    return out


@history_routes.get("/months")
async def list_months(scope: str = "store", current_user=Depends(get_current_user)):
    supabase = get_supabase()
    stores = _stores(supabase, current_user.user.id, scope)
    if not stores:
        return {"months": []}
    return {"months": month_summaries(supabase, [s["id"] for s in stores])}


@history_routes.get("/receipts")
async def search_receipts(
    scope: str = "store",
    month: Optional[str] = None,
    q: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    category: Optional[str] = None,
    min_amount: Optional[str] = Query(None, alias="min"),
    max_amount: Optional[str] = Query(None, alias="max"),
    page: int = 1,
    pageSize: int = 50,
    current_user=Depends(get_current_user),
):
    supabase = get_supabase()
    stores = _stores(supabase, current_user.user.id, scope)
    page = page if page >= 1 else 1
    page_size = min(max(pageSize, 1), MAX_PAGE_SIZE)
    if not stores:
        return {"items": [], "total": 0, "page": page, "pageSize": page_size}
    offset = (page - 1) * page_size
    res = _filtered(
        supabase, [s["id"] for s in stores], month=month, q=q,
        start=_date(start, "start"), end=_date(end, "end"), category=category or None,
        min_amt=_amount(min_amount), max_amt=_amount(max_amount), count=True,
    ).range(offset, offset + page_size - 1).execute()
    names = {s["id"]: s.get("business_name") for s in stores}
    items = [_item(r, names) for r in res.data or []]
    links = sign(supabase, [i["filePath"] for i in items], IN_APP_SECONDS)
    for i in items:
        i["filePath"] = links.get(i["filePath"], i["filePath"])
    return {"items": items, "total": res.count if res.count is not None else len(items), "page": page, "pageSize": page_size}



def _slug(s: Optional[str], fallback: str = "receipt") -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-")[:40]
    return out or fallback


def _ext(row: dict) -> str:
    name = object_path(row.get("file_path")) or row.get("original_name") or ""
    m = re.search(r"\.([A-Za-z0-9]{2,5})$", name)
    return "." + m.group(1).lower() if m else ".jpg"


def _csv_safe(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _download_files(supabase, rows: list) -> dict:
    """{row id: bytes} — a file that can't be fetched is skipped (listed as
    missing in the CSV), never failing the whole download."""
    bucket = supabase.storage.from_(BUCKET)

    def fetch(row):
        op = object_path(row.get("file_path"))
        if not op:
            return row["id"], None
        try:
            return row["id"], bucket.download(op)
        except Exception as e:
            print(f"[history] download of {op} failed: {e}")
            return row["id"], None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(fetch, rows))


async def _month_pdf(supabase, business: dict, rows: list, month: str, label: str) -> bytes:
    from pdf_generator import generate_cover_pdf
    from routes.receiptvault.bank_core import business_transactions
    m_start, m_end = _month_bounds(month)
    last_day = (date.fromisoformat(m_end) - timedelta(days=1)).isoformat()
    try:
        charges = [t for t in business_transactions(supabase, business["id"], m_start, last_day) if float(t.get("amount") or 0) < 0]
    except Exception as e:
        print(f"[history] bank charges skipped: {e}")
        charges = []
    return await generate_cover_pdf(
        business_name=business.get("business_name", ""),
        owner_name=business.get("owner_name", ""),
        accountant_email=business.get("accountant_email", ""),
        receipts=[to_receipt(r) for r in rows],
        period_label=f"{label} Expenses",
        bank_transactions=charges,
    )


@history_routes.get("/months/{month}/download")
async def download_month(month: str, scope: str = "store", kind: str = "zip", current_user=Depends(get_current_user)):
    supabase = get_supabase()
    stores = _stores(supabase, current_user.user.id, scope)
    if kind not in ("zip", "pdf"):
        raise HTTPException(status_code=400, detail="kind must be zip or pdf")
    _month_bounds(month)
    label = date(int(month[:4]), int(month[5:]), 1).strftime("%B %Y")
    names = {s["id"]: s.get("business_name") for s in stores}
    rows = _all_rows(lambda: _filtered(supabase, list(names), month=month)) if stores else []
    if not rows:
        raise HTTPException(status_code=404, detail=f"No receipts in {label}.")
    rows.sort(key=lambda r: (str(r.get("history_date") or ""), str(r.get("uploaded_at") or "")))
    by_store = {}
    for r in rows:
        by_store.setdefault(r["business_id"], []).append(r)
    multi = scope == "all"
    base = f"receiptkept-{'all-stores' if multi else _slug(stores[0].get('business_name'), 'store').lower()}-{month}"

    pdfs = {}
    for s in stores:
        if by_store.get(s["id"]):
            fname = f"accountant-report-{_slug(s.get('business_name'), 'store').lower()}-{month}.pdf" if multi else f"accountant-report-{month}.pdf"
            pdfs[fname] = await _month_pdf(supabase, s, by_store[s["id"]], month, label)

    if kind == "pdf":
        if len(pdfs) == 1:
            name, data = next(iter(pdfs.items()))
            return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{base}-accountant.pdf"'})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in pdfs.items():
                z.writestr(name, data)
        return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{base}-accountant.zip"'})

    files = _download_files(supabase, rows)
    buf = io.BytesIO()
    used = set()
    csv_buf = io.StringIO()
    cols = (["Store"] if multi else []) + ["Date", "Merchant", "Amount", "Category", "Schedule C line", "Notes", "File"]
    w = csv.DictWriter(csv_buf, fieldnames=cols)
    w.writeheader()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            d = str(r.get("history_date") or "")[:10]
            amt = r.get("amount")
            stem = f"{d}_{_slug(r.get('merchant') or r.get('original_name'))}" + (f"_{float(amt):.2f}" if amt is not None else "")
            folder = f"receipts/{_slug(names.get(r['business_id']), 'store')}/" if multi else "receipts/"
            name, n = f"{folder}{stem}{_ext(r)}", 2
            while name in used:
                name, n = f"{folder}{stem}-{n}{_ext(r)}", n + 1
            content = files.get(r["id"])
            if content:
                used.add(name)
                # photos/PDFs are already compressed
                z.writestr(zipfile.ZipInfo(name, date_time=datetime.now().timetuple()[:6]), content, compress_type=zipfile.ZIP_STORED)
            row = {
                "Date": d,
                "Merchant": r.get("merchant") or r.get("original_name") or "",
                "Amount": f"{float(amt):.2f}" if amt is not None else "",
                "Category": r.get("category") or "",
                "Schedule C line": LINE_LABELS.get(r.get("schedule_c_line") or line_for(r.get("category")), r.get("schedule_c_line") or ""),
                "Notes": r.get("notes") or "",
                "File": name if content else "(file missing)",
            }
            if multi:
                row["Store"] = names.get(r["business_id"]) or ""
            w.writerow({k: _csv_safe(v) for k, v in row.items()})
        z.writestr(f"receipts-{month}.csv", "﻿" + csv_buf.getvalue())  # BOM so Excel reads UTF-8
        for name, data in pdfs.items():
            z.writestr(name, data)
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{base}.zip"'})
