"""
Bank statement upload (CSV or PDF), always in two steps so nothing is saved
until the person has seen it:

  POST /api/bank/statements/preview       file (+ optional column mapping / flip)
       -> rows found, how many are already in, or "pick your columns"
  POST /api/bank/statements/{id}/confirm  -> dedupe, sort, match, save
  GET  /api/bank/statements               -> past uploads

Basic: 2 uploads a month (counted when confirmed). Growth/Business: unlimited.
"""

import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from routes.receiptvault.routes import get_current_user, get_supabase as _rv_get_supabase
from routes.receiptvault.bank_routes import bank_store
from routes.receiptvault import bank_core, statement_parser as sp
from routes.receiptvault.entitlements import check_statement_upload_allowed
from routes.receiptvault.files import UPLOAD_CACHE_SECONDS

statement_routes = APIRouter(prefix="/api", tags=["statements"])
MAX_BYTES = 10 * 1024 * 1024
PREVIEW_ROWS = 200


def get_supabase():
    return _rv_get_supabase()


def _account_ref(name: str) -> str:
    return bank_core.normalize_description(name) or "statement"


def _find_or_create_account(supabase, business_id: str, name: str, institution: Optional[str]) -> dict:
    rows = (
        supabase.table("rv_bank_accounts").select("*").eq("business_id", business_id)
        .eq("source", "upload").eq("name", name).execute()
    )
    if rows.data:
        return rows.data[0]
    res = supabase.table("rv_bank_accounts").insert({
        "business_id": business_id, "source": "upload", "name": name, "institution": institution, "status": "active",
    }).execute()
    return res.data[0]


def _to_upload(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "fileName": row.get("file_name"),
        "fileType": row.get("file_type"),
        "bankName": row.get("bank_name"),
        "accountName": row.get("account_name"),
        "status": row.get("status"),
        "rowCount": row.get("row_count") or 0,
        "importedCount": row.get("imported_count") or 0,
        "duplicateCount": row.get("duplicate_count") or 0,
        "createdAt": str(row.get("created_at") or ""),
        "importedAt": str(row.get("imported_at") or "") or None,
    }


@statement_routes.post("/bank/statements/preview")
async def preview_statement(request: Request, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, role = bank_store(supabase, current_user)
    check_statement_upload_allowed(supabase, business, role)

    form = await request.form()
    file_obj = form.get("file")
    if not file_obj or not hasattr(file_obj, "read"):
        raise HTTPException(status_code=400, detail="Pick a statement file to upload.")
    raw = await file_obj.read()
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="That file is over 10 MB. Download a shorter date range from your bank.")
    name = file_obj.filename or "statement"
    ext = os.path.splitext(name)[1].lower()
    is_pdf = ext == ".pdf" or (file_obj.content_type or "").endswith("pdf") or raw[:4] == b"%PDF"
    mapping = json.loads(form.get("mapping")) if form.get("mapping") else None
    flip_raw = form.get("flip")
    flip = None if flip_raw in (None, "", "auto") else str(flip_raw).lower() == "true"

    try:
        parsed = await sp.parse_pdf(raw) if is_pdf else sp.parse_csv(raw, mapping=mapping, flip=flip)
        if is_pdf and flip:
            for t in parsed["transactions"]:
                t["amount"] = -t["amount"]
            parsed["flipped"] = True
    except sp.MappingNeeded as e:
        return {"needsMapping": True, "reason": e.reason, "headers": e.headers, "sampleRows": e.sample, "fileName": name}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    txns = parsed["transactions"]
    bank = parsed.get("bank") or sp.guess_bank(name)
    last4 = parsed.get("last4")
    default_account = f"{bank or 'Bank'}{' …' + str(last4) if last4 else ''}"

    # Store the original file + the parsed rows; nothing is imported yet
    object_path = f"statements/{business['id']}/{int(time.time() * 1000)}-{secrets.token_hex(4)}{ext or ('.pdf' if is_pdf else '.csv')}"
    try:
        supabase.storage.from_("receipts").upload(object_path, raw, {"content-type": "application/pdf" if is_pdf else "text/csv", "cache-control": UPLOAD_CACHE_SECONDS})
    except Exception as e:
        print(f"[statement] storing original failed (continuing): {e}")
        object_path = None

    rec = supabase.table("rv_statement_uploads").insert({
        "business_id": business["id"],
        "file_name": name,
        "file_path": object_path,
        "file_type": "pdf" if is_pdf else "csv",
        "bank_name": bank,
        "account_name": default_account,
        "status": "preview",
        "row_count": len(txns),
        "parsed": {"transactions": txns, "flipped": parsed.get("flipped", False)},
        "created_by": current_user.user.id,
    }).execute().data[0]

    # How many of these are already saved (re-uploads)?
    rows = bank_core.prepare_rows(business["id"], txns, "upload", _account_ref(default_account))
    existing = bank_core._existing_keys(supabase, business["id"], [r["dedupe_key"] for r in rows])
    money_out = [t for t in txns if t["amount"] < 0]
    return {
        "needsMapping": False,
        "uploadId": rec["id"],
        "fileName": name,
        "fileType": "pdf" if is_pdf else "csv",
        "bankName": bank,
        "accountName": default_account,
        "flipped": parsed.get("flipped", False),
        "count": len(txns),
        "skipped": parsed.get("skipped", 0),
        "alreadySaved": len(existing),
        "moneyOut": round(-sum(t["amount"] for t in money_out), 2),
        "moneyIn": round(sum(t["amount"] for t in txns if t["amount"] > 0), 2),
        "firstDate": min(t["posted_date"] for t in txns),
        "lastDate": max(t["posted_date"] for t in txns),
        "transactions": txns[:PREVIEW_ROWS],
    }


class ConfirmBody(BaseModel):
    accountName: Optional[str] = None
    flip: bool = False  # "charges are showing as money in" — flip every sign


@statement_routes.post("/bank/statements/{upload_id}/confirm")
async def confirm_statement(upload_id: str, body: ConfirmBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, role = bank_store(supabase, current_user)
    rows = supabase.table("rv_statement_uploads").select("*").eq("id", upload_id).eq("business_id", business["id"]).execute()
    if not rows.data:
        raise HTTPException(status_code=404, detail="Upload not found")
    rec = rows.data[0]
    if rec.get("status") == "imported":
        return {"alreadyImported": True, **_to_upload(rec)}
    check_statement_upload_allowed(supabase, business, role)

    txns = (rec.get("parsed") or {}).get("transactions") or []
    if body.flip:
        txns = [dict(t, amount=-float(t["amount"])) for t in txns]
    account_name = (body.accountName or rec.get("account_name") or "Bank").strip()[:80]
    account = _find_or_create_account(supabase, business["id"], account_name, rec.get("bank_name"))
    result = await bank_core.import_transactions(
        supabase, business, txns, source="upload", account_ref=_account_ref(account_name),
        bank_account_id=account["id"], statement_upload_id=upload_id,
    )
    now = datetime.now(timezone.utc).isoformat()
    supabase.table("rv_statement_uploads").update({
        "status": "imported", "imported_at": now, "account_name": account_name, "bank_account_id": account["id"],
        "imported_count": result["imported"], "duplicate_count": result["duplicates"],
    }).eq("id", upload_id).execute()
    supabase.table("rv_bank_accounts").update({"last_synced_at": now}).eq("id", account["id"]).execute()
    needs = sum(1 for t in result["transactions"] if bank_core.receipt_required(t))
    return {
        "imported": result["imported"],
        "duplicates": result["duplicates"],
        "matched": result["matched"],
        "needsReceipt": needs,
    }


@statement_routes.get("/bank/statements")
async def list_statements(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    rows = (
        supabase.table("rv_statement_uploads").select("*").eq("business_id", business["id"])
        .eq("status", "imported").order("created_at", desc=True).limit(50).execute()
    )
    return [_to_upload(r) for r in (rows.data or [])]
