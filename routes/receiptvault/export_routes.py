"""
CSV expense export — every plan.

  GET /api/export/expenses.csv?start=YYYY-MM-DD&end=YYYY-MM-DD
        &scope=store|all        (all = every store the owner has)
        &includePersonal=false  (personal charges are left out by default)

One row per expense, from receipts + Money Box money-out entries + bank
charges. A receipt that's matched to a bank charge appears once (on the
charge, with "Has receipt" = Y and the receipt link), never twice.
"""

import csv
import io
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from routes.receiptvault.routes import get_current_user, get_supabase as _rv_get_supabase
from routes.receiptvault.stores import resolve_store, owned_stores
from routes.receiptvault.categories import LINE_LABELS, line_for
from routes.receiptvault.money_routes import CATEGORY_LABELS

export_routes = APIRouter(prefix="/api", tags=["export"])

COLUMNS = ["Date", "Vendor", "Amount", "Category", "Schedule C line", "Payment method", "Has receipt (Y/N)", "Receipt link", "Notes"]
PAYMENT_LABELS = {"cash": "Cash", "card": "Card", "check": "Check", "zelle": "Zelle", "other": "Other"}


def get_supabase():
    return _rv_get_supabase()


def _safe(v) -> str:
    """Stop spreadsheet formula injection from vendor names / notes."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _line_label(line: Optional[str]) -> str:
    return LINE_LABELS.get(line, line or "")


def store_rows(supabase, business: dict, start: str, end: str, include_personal: bool) -> list:
    bid = business["id"]
    rows = []

    txns = (
        supabase.table("rv_bank_transactions").select("*").eq("business_id", bid)
        .gte("posted_date", start).lte("posted_date", end).execute()
    ).data or []
    accounts = {a["id"]: a for a in (supabase.table("rv_bank_accounts").select("*").eq("business_id", bid).execute().data or [])}
    all_matched = {
        r["matched_receipt_id"] for r in (
            supabase.table("rv_bank_transactions").select("matched_receipt_id").eq("business_id", bid)
            .not_.is_("matched_receipt_id", "null").execute().data or []
        )
    }
    receipts = (
        supabase.table("receipts").select("*").eq("business_id", bid)
        .gte("receipt_date", start).lte("receipt_date", end).execute()
    ).data or []
    receipt_by_id = {r["id"]: r for r in receipts}
    linked_ids = [t["matched_receipt_id"] for t in txns if t.get("matched_receipt_id") and t["matched_receipt_id"] not in receipt_by_id]
    if linked_ids:
        for r in supabase.table("receipts").select("*").eq("business_id", bid).in_("id", linked_ids).execute().data or []:
            receipt_by_id[r["id"]] = r

    for t in txns:
        amount = float(t.get("amount") or 0)
        if amount >= 0:
            continue
        personal = bool(t.get("is_personal")) or t.get("status") == "personal"
        if personal and not include_personal:
            continue
        if t.get("status") == "ignored":
            continue
        acct = accounts.get(t.get("bank_account_id")) or {}
        rec = receipt_by_id.get(t.get("matched_receipt_id"))
        notes = "Personal" if personal else ""
        rows.append({
            "Date": str(t.get("posted_date")),
            "Vendor": t.get("vendor") or t.get("description"),
            "Amount": f"{-amount:.2f}",
            "Category": t.get("category") or "",
            "Schedule C line": _line_label(t.get("schedule_c_line")),
            "Payment method": f"{'Card' if (acct.get('account_type') or '').startswith('credit') else 'Bank'}: {acct.get('name') or acct.get('institution') or 'statement'}",
            "Has receipt (Y/N)": "Y" if rec else "N",
            "Receipt link": (rec or {}).get("file_path") or "",
            "Notes": notes,
        })

    for r in receipts:
        if r["id"] in all_matched or r.get("amount") is None:
            continue
        if r.get("approval_status") == "rejected":
            continue
        rows.append({
            "Date": str(r.get("receipt_date") or str(r.get("uploaded_at") or "")[:10]),
            "Vendor": r.get("merchant") or r.get("original_name") or "",
            "Amount": f"{float(r['amount']):.2f}",
            "Category": r.get("category") or "",
            "Schedule C line": _line_label(r.get("schedule_c_line") or line_for(r.get("category"))),
            "Payment method": "",
            "Has receipt (Y/N)": "Y",
            "Receipt link": r.get("file_path") or "",
            "Notes": r.get("notes") or "",
        })

    entries = (
        supabase.table("rv_money_entries").select("*").eq("business_id", bid).eq("entry_type", "expense")
        .gte("entry_date", start).lte("entry_date", end).execute()
    ).data or []
    for e in entries:
        label = CATEGORY_LABELS.get(e.get("category"), (e.get("category") or "").replace("_", " ").title())
        rows.append({
            "Date": str(e.get("entry_date")),
            "Vendor": e.get("note") or label,
            "Amount": f"{float(e.get('amount') or 0):.2f}",
            "Category": f"Money Box: {label}",
            "Schedule C line": "",
            "Payment method": PAYMENT_LABELS.get(e.get("payment_method") or "", (e.get("payment_method") or "").title()),
            "Has receipt (Y/N)": "N",
            "Receipt link": "",
            "Notes": "Entered in Money Box",
        })

    rows.sort(key=lambda x: x["Date"])
    return rows


@export_routes.get("/export/expenses.csv")
async def export_expenses(
    start: Optional[str] = None,
    end: Optional[str] = None,
    scope: str = "store",
    includePersonal: bool = False,
    current_user=Depends(get_current_user),
):
    supabase = get_supabase()
    business, role, _m = resolve_store(supabase, current_user.user.id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    if role not in ("owner", "manager", "bookkeeper"):
        raise HTTPException(status_code=403, detail="Only the owner, a manager or the bookkeeper can export.")
    today = date.today()
    start = start or today.replace(month=1, day=1).isoformat()
    end = end or today.isoformat()
    try:
        datetime.strptime(start, "%Y-%m-%d")
        datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if start > end:
        raise HTTPException(status_code=400, detail="The start date is after the end date.")

    if scope == "all":
        if role != "owner":
            raise HTTPException(status_code=403, detail="Only the owner can export all stores.")
        stores = owned_stores(supabase, current_user.user.id)
    else:
        stores = [business]

    buf = io.StringIO()
    cols = (["Store"] if scope == "all" else []) + COLUMNS
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for b in stores:
        for row in store_rows(supabase, b, start, end, includePersonal):
            if scope == "all":
                row["Store"] = b.get("business_name") or ""
            w.writerow({k: _safe(v) for k, v in row.items()})

    name = "all-stores" if scope == "all" else (business.get("business_name") or "store")
    safe_name = "".join(c if c.isalnum() else "-" for c in name).strip("-").lower() or "store"
    filename = f"receiptkept-expenses-{safe_name}-{start}-to-{end}.csv"
    return StreamingResponse(
        iter(["﻿" + buf.getvalue()]),  # BOM so Excel opens it as UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
