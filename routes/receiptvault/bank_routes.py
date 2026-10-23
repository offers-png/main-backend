"""
Bank transactions: list, fix category, Personal button, receipt matching,
"Missing receipts". Transactions themselves arrive from statement uploads
(statement_routes.py) and bank connections (connect_routes.py).

Owners, managers and bookkeepers only — employees never see bank data.
"""

import base64 as b64lib
import os
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from routes.receiptvault.routes import (
    get_current_user, get_supabase as _rv_get_supabase, to_receipt, save_receipt, AUTO_APPROVE_ROLES,
)
from routes.receiptvault.stores import resolve_store
from routes.receiptvault.categories import CATEGORY_NAMES, TRANSFER, line_for, normalize_category
from routes.receiptvault import bank_core
from routes.receiptvault.files import sign_receipts

bank_routes = APIRouter(prefix="/api", tags=["bank"])
BANK_ROLES = ("owner", "manager", "bookkeeper")


def get_supabase():
    return _rv_get_supabase()


def bank_store(supabase, current_user):
    business, role, _m = resolve_store(supabase, current_user.user.id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    if role not in BANK_ROLES:
        raise HTTPException(status_code=403, detail="Only the owner, a manager or the bookkeeper can see bank charges.")
    return business, role


def _get_txn(supabase, business_id: str, txn_id: str) -> dict:
    rows = supabase.table("rv_bank_transactions").select("*").eq("id", txn_id).eq("business_id", business_id).execute()
    if not rows.data:
        raise HTTPException(status_code=404, detail="Charge not found")
    return rows.data[0]


def _with_receipts(supabase, business_id: str, txns: list) -> list:
    ids = [t["matched_receipt_id"] for t in txns if t.get("matched_receipt_id")]
    receipts = {}
    if ids:
        rows = supabase.table("receipts").select("*").eq("business_id", business_id).in_("id", ids).execute()
        receipts = {r["id"]: r for r in (rows.data or [])}
    out = []
    for t in txns:
        item = bank_core.to_txn(t)
        r = receipts.get(t.get("matched_receipt_id"))
        item["receipt"] = to_receipt(r) if r else None
        out.append(item)
    sign_receipts(supabase, [i["receipt"] for i in out if i["receipt"]])
    return out


def _all_rows(supabase, business_id: str) -> list:
    """Every charge for the store (paged: the database returns 1000 at a time)."""
    out, page = [], 1000
    while True:
        chunk = (
            supabase.table("rv_bank_transactions").select("*").eq("business_id", business_id)
            .order("posted_date", desc=True).range(len(out), len(out) + page - 1).execute()
        ).data or []
        out += chunk
        if len(chunk) < page:
            return out


def _is_big_business_charge(r: dict) -> bool:
    """A business charge the IRS wants a receipt for, with or without one."""
    if r.get("status") not in ("needs_receipt", "matched"):
        return False
    if bank_core.bank_check_number(r.get("description")):
        return False
    return abs(float(r.get("amount") or 0)) >= bank_core.RECEIPT_THRESHOLD or r.get("category") in bank_core.ALWAYS_NEEDS_RECEIPT


def _goal(rows: list, today: Optional[date] = None) -> dict:
    """This month's goal: big charges that still need a receipt. If nothing
    has come in for this month yet (statements lag), use the newest month
    that has charges."""
    today = today or date.today()
    month = today.strftime("%Y-%m")
    big = [r for r in rows if _is_big_business_charge(r)]
    months = sorted({str(r.get("posted_date") or "")[:7] for r in big if r.get("posted_date")})
    if month not in months and months:
        month = months[-1]
    in_month = [r for r in big if str(r.get("posted_date") or "")[:7] == month]
    need = [r for r in in_month if r.get("status") == "needs_receipt"]
    earlier = [r for r in big if r.get("status") == "needs_receipt" and str(r.get("posted_date") or "")[:7] < month]
    return {
        "month": month,
        "isCurrentMonth": month == today.strftime("%Y-%m"),
        "needReceipt": len(need),
        "needReceiptAmount": round(sum(abs(float(r.get("amount") or 0)) for r in need), 2),
        "haveReceipt": len(in_month) - len(need),
        "bigCharges": len(in_month),
        "earlier": len(earlier),
        "threshold": bank_core.RECEIPT_THRESHOLD,
    }


def _counts(rows: list, today: Optional[date] = None) -> dict:
    counts = {"needsReceipt": 0, "matched": 0, "personal": 0, "ignored": 0, "total": len(rows),
              "needsReceiptAmount": 0.0, "noReceiptNeeded": 0}
    for r in rows:
        st = r.get("status")
        if st == "needs_receipt":
            if bank_core.receipt_required(r):
                counts["needsReceipt"] += 1
                counts["needsReceiptAmount"] += abs(float(r.get("amount") or 0))
            else:
                counts["noReceiptNeeded"] += 1
        elif st in ("matched", "personal", "ignored"):
            counts[st] += 1
    counts["needsReceiptAmount"] = round(counts["needsReceiptAmount"], 2)
    counts["goal"] = _goal(rows, today)
    return counts


@bank_routes.get("/bank/transactions")
async def list_transactions(
    status: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 300,
    current_user=Depends(get_current_user),
):
    """status: needs_receipt (only the ones the IRS wants a receipt for),
    no_receipt_needed (business charges under $75), matched, personal, ignored."""
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    all_rows = _all_rows(supabase, business["id"])
    bank_core.match_checks(supabase, business["id"], all_rows)
    bank_core.apply_known_vendors(supabase, all_rows)
    rows = all_rows
    if status == "needs_receipt":
        rows = [r for r in rows if bank_core.receipt_required(r)]
    elif status == "no_receipt_needed":
        rows = [r for r in rows if r.get("status") == "needs_receipt" and not bank_core.receipt_required(r)]
    elif status:
        rows = [r for r in rows if r.get("status") == status]
    if start:
        rows = [r for r in rows if str(r.get("posted_date") or "") >= start]
    if end:
        rows = [r for r in rows if str(r.get("posted_date") or "") <= end]
    if q:
        rows = [r for r in rows if q.lower() in (r.get("description") or "").lower()]
    rows = sorted(rows, key=lambda r: (str(r.get("posted_date") or ""), str(r.get("created_at") or "")), reverse=True)
    rows = rows[: max(1, min(limit, 1000))]
    return {"transactions": _with_receipts(supabase, business["id"], rows), "counts": _counts(all_rows)}


@bank_routes.get("/bank/missing-receipts")
async def missing_receipts(current_user=Depends(get_current_user)):
    """This month's goal: big business charges with no receipt yet."""
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    rows = _all_rows(supabase, business["id"])
    goal = _goal(rows)
    need = [r for r in rows if bank_core.receipt_required(r) and str(r.get("posted_date") or "")[:7] == goal["month"]]
    return {
        "count": goal["needReceipt"],
        "total": goal["needReceiptAmount"],
        "goal": goal,
        "transactions": [bank_core.to_txn(r) for r in need],
    }


class UpdateTxnBody(BaseModel):
    category: Optional[str] = None
    isPersonal: Optional[bool] = None
    ignored: Optional[bool] = None          # "Already counted" / not a business charge
    countsAsIncome: Optional[bool] = None   # deposits only
    applyToVendor: bool = True


def _change(supabase, txn: dict, body) -> Optional[str]:
    """Apply a category / personal / already-counted change to one charge.
    Returns the new category (or None)."""
    txn_id = txn["id"]
    amount = float(txn.get("amount") or 0)
    data = {"updated_at": datetime.now(timezone.utc).isoformat()}

    category = None
    if body.category is not None:
        category = TRANSFER if body.category == TRANSFER else normalize_category(body.category)
        data["category"] = category
        data["schedule_c_line"] = line_for(category) if category in CATEGORY_NAMES else None
        data["category_source"] = "user"
    if body.isPersonal is not None:
        data["is_personal"] = body.isPersonal
        data["category_source"] = "user"
    if body.countsAsIncome is not None:
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Only money coming in can be counted as income.")
        data["counts_as_income"] = body.countsAsIncome

    personal = data.get("is_personal", bool(txn.get("is_personal")))
    if personal:
        data["status"] = "personal"
    elif body.ignored is True or category == TRANSFER:
        data["status"] = "ignored"
    elif amount < 0:
        data["status"] = "matched" if txn.get("matched_receipt_id") else "needs_receipt"
    else:
        data["status"] = "ignored"

    supabase.table("rv_bank_transactions").update(data).eq("id", txn_id).execute()
    return category


@bank_routes.patch("/bank/transactions/{txn_id}")
async def update_transaction(txn_id: str, body: UpdateTxnBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    txn = _get_txn(supabase, business["id"], txn_id)
    category = _change(supabase, txn, body)

    # Remember the choice for this vendor and re-sort its other charges
    updated_others = 0
    if body.applyToVendor and (body.category is not None or body.isPersonal is not None):
        pattern = bank_core.save_rule(supabase, business["id"], txn.get("description"), category=category, is_personal=body.isPersonal)
        if pattern:
            updated_others = bank_core.apply_rule_to_existing(
                supabase, business["id"], pattern, category=category, is_personal=body.isPersonal, skip_id=txn_id)

    fresh = _get_txn(supabase, business["id"], txn_id)
    out = _with_receipts(supabase, business["id"], [fresh])[0]
    out["alsoUpdated"] = updated_others
    return out


class BulkBody(BaseModel):
    ids: List[str]
    category: Optional[str] = None
    isPersonal: Optional[bool] = None
    ignored: Optional[bool] = None          # "Already counted"
    applyToVendor: bool = True


@bank_routes.post("/bank/transactions/bulk")
async def bulk_update(body: BulkBody, current_user=Depends(get_current_user)):
    """Select many -> set category / mark personal / already counted.
    Each vendor's choice is remembered, like a single edit."""
    if not body.ids:
        raise HTTPException(status_code=400, detail="Pick at least one charge.")
    if len(body.ids) > 500:
        raise HTTPException(status_code=400, detail="Pick 500 charges or fewer at a time.")
    if body.category is None and body.isPersonal is None and body.ignored is None:
        raise HTTPException(status_code=400, detail="Pick what to change.")
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    ids = list(dict.fromkeys(body.ids))
    txns = supabase.table("rv_bank_transactions").select("*").eq("business_id", business["id"]).in_("id", ids).execute().data or []
    # Money in has no expense category, so a category change skips deposits
    txns = [t for t in txns if body.category is None or float(t.get("amount") or 0) < 0]
    change = UpdateTxnBody(category=body.category, isPersonal=body.isPersonal, ignored=body.ignored)
    category = None
    for t in txns:
        category = _change(supabase, t, change)

    learned, also = set(), 0
    if body.applyToVendor and (body.category is not None or body.isPersonal is not None):
        for t in txns:
            key = bank_core.vendor_key(t.get("description"))
            if not key or key in learned:
                continue
            pattern = bank_core.save_rule(supabase, business["id"], t.get("description"), category=category, is_personal=body.isPersonal)
            if pattern:
                learned.add(pattern)
                also += bank_core.apply_rule_to_existing(
                    supabase, business["id"], pattern, category=category, is_personal=body.isPersonal, skip_id=t["id"])
    return {"updated": len(txns), "alsoUpdated": also, "vendorsLearned": len(learned)}


@bank_routes.delete("/bank/transactions/{txn_id}/money-match")
async def unlink_money_check(txn_id: str, current_user=Depends(get_current_user)):
    """'Not the same check': back to the owner's list, never re-matched."""
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    txn = _get_txn(supabase, business["id"], txn_id)
    if not txn.get("matched_money_entry_id"):
        raise HTTPException(status_code=400, detail="This charge isn't linked to a Money Box check.")
    bank_core.unlink_money_check(supabase, txn)
    return _with_receipts(supabase, business["id"], [_get_txn(supabase, business["id"], txn_id)])[0]


class MatchBody(BaseModel):
    receiptId: str


@bank_routes.post("/bank/transactions/{txn_id}/match")
async def link_receipt(txn_id: str, body: MatchBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    txn = _get_txn(supabase, business["id"], txn_id)
    r = supabase.table("receipts").select("id").eq("id", body.receiptId).eq("business_id", business["id"]).execute()
    if not r.data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    # a receipt belongs to one charge: unlink it from anywhere else first
    supabase.table("rv_bank_transactions").update({"matched_receipt_id": None, "status": "needs_receipt"})\
        .eq("business_id", business["id"]).eq("matched_receipt_id", body.receiptId).execute()
    supabase.table("rv_bank_transactions").update({
        "matched_receipt_id": body.receiptId,
        "status": "personal" if txn.get("is_personal") else "matched",
        "match_locked": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", txn_id).execute()
    return _with_receipts(supabase, business["id"], [_get_txn(supabase, business["id"], txn_id)])[0]


@bank_routes.delete("/bank/transactions/{txn_id}/match")
async def unlink_receipt(txn_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    txn = _get_txn(supabase, business["id"], txn_id)
    amount = float(txn.get("amount") or 0)
    supabase.table("rv_bank_transactions").update({
        "matched_receipt_id": None,
        "status": bank_core.initial_status(amount, bool(txn.get("is_personal")), txn.get("category")),
        "match_locked": True,  # don't auto-link this charge again
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", txn_id).execute()
    return _with_receipts(supabase, business["id"], [_get_txn(supabase, business["id"], txn_id)])[0]


@bank_routes.get("/bank/transactions/{txn_id}/candidates")
async def match_candidates(txn_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    txn = _get_txn(supabase, business["id"], txn_id)
    return sign_receipts(supabase, [to_receipt(r) for r in bank_core.receipt_candidates(supabase, business["id"], txn)])


@bank_routes.post("/bank/transactions/{txn_id}/receipt", status_code=201)
async def scan_receipt_for_charge(txn_id: str, request: Request, current_user=Depends(get_current_user)):
    """One tap from "Missing receipts": photo in, receipt saved, linked to
    this exact charge. Counts as a scan."""
    from routes.receiptvault.entitlements import check_scan_allowed
    supabase = get_supabase()
    business, role = bank_store(supabase, current_user)
    txn = _get_txn(supabase, business["id"], txn_id)
    check_scan_allowed(supabase, business, role)

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        image_base64 = body.get("imageBase64")
        filename = body.get("filename") or f"receipt-{int(datetime.now().timestamp())}.jpg"
        if not image_base64:
            raise HTTPException(status_code=400, detail="imageBase64 is required")
        content = b64lib.b64decode(image_base64)
        ext = os.path.splitext(filename)[1].lower() or ".jpg"
        mime = "application/pdf" if ext == ".pdf" else f"image/{ext[1:].replace('jpg', 'jpeg')}"
    else:
        form = await request.form()
        file_obj = form.get("file")
        if not file_obj:
            raise HTTPException(status_code=400, detail="file is required")
        content = await file_obj.read()
        filename = file_obj.filename or "receipt.jpg"
        ext = os.path.splitext(filename)[1].lower() or ".jpg"
        mime = file_obj.content_type or f"image/{ext[1:]}"

    row = await save_receipt(
        supabase, business, content=content, original_name=filename, ext=ext, mime=mime,
        submitted_by=current_user.user.id,
        approval_status="approved" if role in AUTO_APPROVE_ROLES else "pending",
        check_duplicate_name=False,
    )
    # save_receipt may already have auto-linked it somewhere; this charge wins
    supabase.table("rv_bank_transactions").update({"matched_receipt_id": None, "status": "needs_receipt"})\
        .eq("business_id", business["id"]).eq("matched_receipt_id", row["id"]).neq("id", txn_id).execute()
    fill = {}
    if row.get("amount") is None:
        fill["amount"] = abs(float(txn.get("amount") or 0))
    if not row.get("receipt_date"):
        fill["receipt_date"] = str(txn.get("posted_date"))
    if not row.get("merchant"):
        fill["merchant"] = txn.get("vendor") or txn.get("description")
    if not row.get("category") and txn.get("category") in CATEGORY_NAMES:
        fill["category"] = txn.get("category")
        fill["schedule_c_line"] = line_for(txn.get("category"))
    if fill:
        row = (supabase.table("receipts").update(fill).eq("id", row["id"]).execute().data or [row])[0]
    supabase.table("rv_bank_transactions").update({
        "matched_receipt_id": row["id"],
        "status": "personal" if txn.get("is_personal") else "matched",
        "match_locked": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", txn_id).execute()
    return {"receipt": sign_receipts(supabase, [to_receipt(row)])[0], "transaction": _with_receipts(supabase, business["id"], [_get_txn(supabase, business["id"], txn_id)])[0]}


@bank_routes.post("/bank/rematch")
async def rematch(current_user=Depends(get_current_user)):
    """Try matching every charge that still needs a receipt."""
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    rows = (
        supabase.table("rv_bank_transactions").select("*").eq("business_id", business["id"])
        .eq("status", "needs_receipt").execute()
    ).data or []
    return {"matched": bank_core.match_transactions(supabase, business["id"], rows)}


@bank_routes.get("/bank/rules")
async def list_rules(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    rows = supabase.table("rv_vendor_rules").select("*").eq("business_id", business["id"]).order("vendor_pattern").execute()
    return [{
        "id": r["id"], "vendorPattern": r.get("vendor_pattern"), "category": r.get("category"),
        "isPersonal": bool(r.get("is_personal")),
    } for r in (rows.data or [])]


@bank_routes.delete("/bank/rules/{rule_id}")
async def delete_rule(rule_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    supabase.table("rv_vendor_rules").delete().eq("id", rule_id).eq("business_id", business["id"]).execute()
    return {"ok": True}
