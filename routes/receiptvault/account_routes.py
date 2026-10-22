"""
Settings → "Your Data".

  GET    /api/account/export  -> zip of everything ReceiptKept holds for this login
  DELETE /api/account         -> body {"confirm": "DELETE"}; permanently deletes it

Owner: every store they own (all its records + uploaded files), their
billing account, and any team memberships. Team member: their memberships
and the records they personally added stay with the store they belong to
(the store's books are the owner's), so for them export = their own entries
and delete = leave every team.

The Supabase login is SHARED with other apps in the same project, so by
default delete removes all ReceiptKept data but keeps the login itself.
Set RK_DELETE_AUTH_LOGIN=true to also delete the login.
"""

import csv
import io
import json
import os
import zipfile
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from routes.receiptvault.routes import get_current_user, get_supabase as _rv_get_supabase
from routes.receiptvault.stores import owned_stores, memberships
from routes.receiptvault import entitlements as ent

account_routes = APIRouter(prefix="/api", tags=["account"])

BUCKET_MARKER = "/storage/v1/object/public/receipts/"

# table -> columns never included in an export (secrets / internal links)
REDACT = {
    "rv_dropbox_tokens": {"token"},
    "rv_inbound_addresses": {"token"},
    "rv_statement_uploads": {"parsed"},
}
# per-store tables, in a safe delete order (children first)
STORE_TABLES = [
    "rv_bank_transactions", "rv_vendor_rules", "rv_statement_uploads", "rv_bank_accounts",
    "rv_inbound_emails", "rv_inbound_addresses",
    "receipts", "rv_money_entries", "rv_money_packets", "rv_mileage_entries",
    "invoices",  # invoice_items handled through invoice ids
    "inventory_transactions", "inventory_items",
    "rv_dropbox_tokens", "rv_feedback", "business_users", "send_events",
]


def get_supabase():
    return _rv_get_supabase()


def _rows(supabase, table: str, col: str, val) -> list:
    try:
        return supabase.table(table).select("*").eq(col, val).execute().data or []
    except Exception as e:  # a table that doesn't exist yet shouldn't break export/delete
        print(f"[account] read {table} failed: {e}")
        return []


def _object_path(file_path: Optional[str]) -> Optional[str]:
    if not file_path:
        return None
    if BUCKET_MARKER in file_path:
        return file_path.split(BUCKET_MARKER, 1)[1]
    if "://" not in file_path:
        return file_path  # already an object path (statements/...)
    return None


# ── export ──────────────────────────────────────────────────────────────────

def _store_bundle(supabase, business: dict) -> dict:
    bid = business["id"]
    data = {t: _rows(supabase, t, "business_id", bid) for t in STORE_TABLES}
    inv_ids = [i["id"] for i in data["invoices"]]
    data["invoice_items"] = [it for iid in inv_ids for it in _rows(supabase, "invoice_items", "invoice_id", iid)]
    for table, cols in REDACT.items():
        data[table] = [{k: v for k, v in r.items() if k not in cols} for r in data.get(table, [])]
    if data.get("rv_inbound_addresses"):
        from routes.receiptvault.inbound_email_routes import address_for
        tok = _rows(supabase, "rv_inbound_addresses", "business_id", bid)
        for r, t in zip(data["rv_inbound_addresses"], tok):
            r["address"] = address_for(t["token"])
    data["referrals"] = _rows(supabase, "referrals", "referrer_business_id", bid)
    return data


def _csv(rows: list) -> str:
    if not rows:
        return ""
    cols = list(dict.fromkeys(k for r in rows for k in r))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
    return buf.getvalue()


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in (name or "store")).strip("-").lower()[:40] or "store"


@account_routes.get("/account/export")
async def export_account(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    uid = current_user.user.id
    owned = owned_stores(supabase, uid)
    mems = memberships(supabase, uid)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        profile = {
            "exportedAt": datetime.now(timezone.utc).isoformat(),
            "user": {"id": uid, "email": current_user.user.email},
            "billingAccount": {k: v for k, v in ent.get_account(supabase, uid).items()} if owned else None,
            "teamMemberships": mems,
        }
        z.writestr("account.json", json.dumps(profile, indent=2, default=str))
        readme = [
            "ReceiptKept data export",
            f"Made {date.today().isoformat()} for {current_user.user.email}.",
            "",
            "Each store has its own folder: a .json file with everything, plus a .csv per kind of record",
            "(receipts, Money Box entries, mileage, invoices, inventory, bank charges...).",
            "expenses.csv is the same spreadsheet as Settings -> Download expenses, for all dates.",
            "Receipt photos and PDFs are linked in the file_path column of receipts.csv.",
        ]
        from routes.receiptvault.export_routes import store_rows, COLUMNS
        for b in owned:
            folder = f"stores/{_slug(b.get('business_name'))}-{b['id'][:8]}"
            bundle = _store_bundle(supabase, b)
            z.writestr(f"{folder}/store.json", json.dumps({"store": b, **bundle}, indent=2, default=str))
            for table, rows in bundle.items():
                if rows:
                    z.writestr(f"{folder}/{table}.csv", _csv(rows))
            exp = io.StringIO()
            w = csv.DictWriter(exp, fieldnames=COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in store_rows(supabase, b, "1900-01-01", "2999-12-31", include_personal=True):
                w.writerow(r)
            z.writestr(f"{folder}/expenses.csv", exp.getvalue())
        # A team member's own entries in the stores they work at
        for m in mems:
            if any(b["id"] == m["business_id"] for b in owned):
                continue
            bid = m["business_id"]
            mine = {
                "receipts": [r for r in _rows(supabase, "receipts", "business_id", bid) if r.get("submitted_by") == uid],
                "rv_money_entries": [r for r in _rows(supabase, "rv_money_entries", "business_id", bid) if str(r.get("user_id")) == uid],
                "rv_mileage_entries": [r for r in _rows(supabase, "rv_mileage_entries", "business_id", bid) if str(r.get("user_id")) == uid],
            }
            folder = f"my-entries/store-{bid[:8]}"
            z.writestr(f"{folder}/my-entries.json", json.dumps(mine, indent=2, default=str))
            for table, rows in mine.items():
                if rows:
                    z.writestr(f"{folder}/{table}.csv", _csv(rows))
        if not owned and not mems:
            readme.append("\nThere are no ReceiptKept records for this login.")
        z.writestr("README.txt", "\n".join(readme) + "\n")

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="receiptkept-export.zip"'},
    )


# ── delete ──────────────────────────────────────────────────────────────────

class DeleteBody(BaseModel):
    confirm: str


async def _cancel_billing(supabase, uid: str) -> None:
    acct = ent.get_account(supabase, uid)
    if acct.get("stripe_subscription_id") and ent.stripe_key():
        try:
            await ent.stripe_request("DELETE", f"subscriptions/{acct['stripe_subscription_id']}")
        except HTTPException as e:
            # Don't leave them paying for an account that's gone
            raise HTTPException(status_code=502, detail=f"We couldn't cancel your plan with the card company, so nothing was deleted. Please try again. ({e.detail})")


async def _disconnect_banks(supabase, business_id: str) -> None:
    for a in _rows(supabase, "rv_bank_accounts", "business_id", business_id):
        if a.get("source") == "stripe" and a.get("status") == "active" and a.get("stripe_account_id") and ent.stripe_key():
            try:
                await ent.stripe_request("POST", f"financial_connections/accounts/{a['stripe_account_id']}/disconnect")
            except HTTPException as e:
                print(f"[account] bank disconnect failed (continuing): {e.detail}")


def _delete_store(supabase, business: dict) -> dict:
    bid = business["id"]
    counts = {}
    paths = []
    for r in _rows(supabase, "receipts", "business_id", bid):
        paths.append(_object_path(r.get("file_path")))
    for r in _rows(supabase, "rv_money_packets", "business_id", bid):
        paths.append(_object_path(r.get("file_path")))
    for r in _rows(supabase, "rv_statement_uploads", "business_id", bid):
        paths.append(_object_path(r.get("file_path")))
    for r in _rows(supabase, "rv_dropbox_tokens", "business_id", bid):
        for f in r.get("uploaded_files") or []:
            paths.append(_object_path((f or {}).get("path")))
    paths = [p for p in paths if p]
    for i in range(0, len(paths), 100):
        try:
            supabase.storage.from_("receipts").remove(paths[i:i + 100])
        except Exception as e:
            print(f"[account] storage remove failed (continuing): {e}")
    counts["files"] = len(paths)

    for inv in _rows(supabase, "invoices", "business_id", bid):
        supabase.table("invoice_items").delete().eq("invoice_id", inv["id"]).execute()
    for table in STORE_TABLES:
        try:
            res = supabase.table(table).delete().eq("business_id", bid).execute()
            counts[table] = len(res.data or [])
        except Exception as e:
            print(f"[account] delete {table} failed: {e}")
    supabase.table("referrals").delete().eq("referrer_business_id", bid).execute()
    supabase.table("referrals").delete().eq("referred_business_id", bid).execute()
    supabase.table("businesses").delete().eq("id", bid).execute()
    return counts


@account_routes.delete("/account")
async def delete_account(body: DeleteBody, current_user=Depends(get_current_user)):
    if (body.confirm or "").strip().upper() != "DELETE":
        raise HTTPException(status_code=400, detail='Type DELETE to confirm.')
    supabase = get_supabase()
    uid = current_user.user.id
    owned = owned_stores(supabase, uid)

    if owned:
        await _cancel_billing(supabase, uid)  # first: if this fails, nothing is deleted
    for b in owned:
        await _disconnect_banks(supabase, b["id"])
    deleted_stores = [_delete_store(supabase, b) for b in owned]

    supabase.table("business_users").delete().eq("user_id", uid).execute()
    supabase.table("business_users").delete().eq("email", current_user.user.email or "__none__").execute()
    supabase.table("rv_accounts").delete().eq("owner_user_id", uid).execute()

    login_deleted = False
    if os.getenv("RK_DELETE_AUTH_LOGIN", "").lower() == "true":
        try:
            supabase.auth.admin.delete_user(uid)
            login_deleted = True
        except Exception as e:
            print(f"[account] auth user delete failed: {e}")
    print(f"[account] deleted ReceiptKept data for {uid}: {len(owned)} store(s)")
    return {"ok": True, "storesDeleted": len(owned), "loginDeleted": login_deleted, "details": deleted_stores}
