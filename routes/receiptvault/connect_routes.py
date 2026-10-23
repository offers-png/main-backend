"""
Bank connection through Stripe Financial Connections — Growth and Business.

Read-only: the session asks for the `transactions` permission only (no
payments, no balances, no account/routing numbers).

  POST /api/bank/connect/session         -> client_secret for Stripe.js
  POST /api/bank/connect/complete        -> save linked accounts, subscribe
                                            to daily refreshes, first import
  GET  /api/bank/accounts                -> connected + uploaded accounts
  POST /api/bank/accounts/{id}/sync      -> ask Stripe for a fresh refresh
  POST /api/bank/accounts/{id}/disconnect-> unsubscribe + disconnect (data kept)

Webhook `financial_connections.account.refreshed_transactions` (routed from
/api/billing/webhook) imports only new transactions using
transaction_refresh[after] = the last refresh we already processed.
Live mode needs Financial Connections registration in the Stripe dashboard.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from routes.receiptvault.routes import get_current_user, get_supabase as _rv_get_supabase
from routes.receiptvault.bank_routes import bank_store
from routes.receiptvault import bank_core
from routes.receiptvault import entitlements as ent

connect_routes = APIRouter(prefix="/api", tags=["bank-connect"])

ACCOUNT_SUBCATEGORIES = ["checking", "savings", "credit_card"]


def get_supabase():
    return _rv_get_supabase()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_account(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "source": row.get("source"),
        "name": row.get("name"),
        "institution": row.get("institution"),
        "last4": row.get("last4"),
        "accountType": row.get("account_type"),
        "status": row.get("status"),
        "lastSyncedAt": row.get("last_synced_at"),
        "createdAt": str(row.get("created_at") or ""),
    }


async def _owner_customer(supabase, business: dict) -> str:
    owner = str(business["user_id"])
    account = ent.get_account(supabase, owner)
    if account.get("stripe_customer_id"):
        return account["stripe_customer_id"]
    cust = await ent.stripe_request("POST", "customers", {
        "email": business.get("owner_email") or "",
        "metadata[owner_user_id]": owner,
        "metadata[app]": "receiptkept",
    })
    ent.update_account(supabase, owner, {"stripe_customer_id": cust["id"]})
    return cust["id"]


@connect_routes.post("/bank/connect/session")
async def create_session(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, role = bank_store(supabase, current_user)
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only the store owner can connect a bank.")
    ent.check_bank_account_allowed(supabase, business, role)
    customer = await _owner_customer(supabase, business)
    data = {
        "account_holder[type]": "customer",
        "account_holder[customer]": customer,
        "permissions[]": "transactions",
        "prefetch[]": "transactions",
        "filters[countries][]": "US",
    }
    # form-encode the list filter by hand (dict keys must be unique)
    for i, sub in enumerate(ACCOUNT_SUBCATEGORIES):
        data[f"filters[account_subcategories][{i}]"] = sub
    session = await ent.stripe_request("POST", "financial_connections/sessions", data)
    return {
        "clientSecret": session["client_secret"],
        "sessionId": session["id"],
        "publishableKey": os.getenv("STRIPE_PUBLISHABLE_KEY", ""),
    }


class CompleteBody(BaseModel):
    sessionId: str


def _subcategory(acct: dict) -> str:
    return acct.get("subcategory") or acct.get("category") or "account"


@connect_routes.post("/bank/connect/complete")
async def complete_session(body: CompleteBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, role = bank_store(supabase, current_user)
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only the store owner can connect a bank.")
    session = await ent.stripe_request("GET", f"financial_connections/sessions/{body.sessionId}")
    linked = ((session.get("accounts") or {}).get("data")) or []
    if not linked:
        return {"connected": [], "skipped": 0, "message": "No account was connected."}

    owner = str(business["user_id"])
    account = ent.get_account(supabase, owner)
    limit = ent.limits_for(account)["bank_accounts"]
    used = ent.count_bank_accounts(supabase, owner)

    connected, over_limit = [], []
    for fc in linked:
        existing = supabase.table("rv_bank_accounts").select("*").eq("stripe_account_id", fc["id"]).execute().data
        if existing:
            connected.append(existing[0])
            continue
        if limit is not None and used >= limit:
            over_limit.append(fc)
            continue
        # Reconnect: same bank + last 4 as a disconnected account in this store -> reuse it
        prior = [
            r for r in (supabase.table("rv_bank_accounts").select("*").eq("business_id", business["id"]).eq("source", "stripe").execute().data or [])
            if r.get("status") != "active" and r.get("institution") == fc.get("institution_name") and r.get("last4") == fc.get("last4")
        ]
        data = {
            "business_id": business["id"],
            "source": "stripe",
            "name": fc.get("display_name") or fc.get("institution_name") or "Bank account",
            "institution": fc.get("institution_name"),
            "last4": fc.get("last4"),
            "account_type": _subcategory(fc),
            "stripe_account_id": fc["id"],
            "status": "active",
            "last_transaction_refresh": None,
            "updated_at": _now(),
        }
        if prior:
            row = supabase.table("rv_bank_accounts").update(data).eq("id", prior[0]["id"]).execute().data[0]
            row["_reconnected"] = True
        else:
            row = supabase.table("rv_bank_accounts").insert(data).execute().data[0]
        used += 1
        connected.append(row)
        try:
            await ent.stripe_request("POST", f"financial_connections/accounts/{fc['id']}/subscribe", {"features[]": "transactions"})
        except HTTPException as e:
            print(f"[connect] subscribe failed for {fc['id']}: {e.detail}")

    # Over the plan limit: don't keep the extra links
    for fc in over_limit:
        try:
            await ent.stripe_request("POST", f"financial_connections/accounts/{fc['id']}/disconnect")
        except HTTPException:
            pass

    # First import (up to ~180 days the bank shares). If Stripe is still
    # fetching, the refreshed_transactions webhook brings them in.
    imported = 0
    for row in connected:
        try:
            imported += await sync_account(supabase, business, row, initial=bool(row.get("_reconnected")))
        except HTTPException as e:
            print(f"[connect] first import for {row.get('stripe_account_id')} failed: {e.detail}")

    msg = None
    if over_limit:
        msg = f"Your plan connects {limit} bank account{'s' if limit != 1 else ''}, so we connected {len(connected)}. Upgrade to connect more."
    return {"connected": [to_account(r) for r in connected], "skipped": len(over_limit), "imported": imported, "message": msg}


async def _list_transactions(stripe_account_id: str, after_refresh: Optional[str]) -> list:
    """All transactions for an account created or updated after
    `after_refresh` (all of them if None), following pagination."""
    out, starting_after = [], None
    while True:
        params = f"financial_connections/transactions?account={stripe_account_id}&limit=100"
        if after_refresh:
            params += f"&transaction_refresh[after]={after_refresh}"
        if starting_after:
            params += f"&starting_after={starting_after}"
        page = await ent.stripe_request("GET", params)
        data = page.get("data") or []
        out.extend(data)
        if not page.get("has_more") or not data:
            break
        starting_after = data[-1]["id"]
    return out


def _map_txn(t: dict) -> Optional[dict]:
    if t.get("status") != "posted":
        return None  # pending ones arrive again once posted; void never happened
    ts = (t.get("status_transitions") or {}).get("posted_at") or t.get("transacted_at")
    posted = datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat() if ts else None
    if not posted:
        return None
    return {
        "posted_date": posted,
        "description": t.get("description") or "(no description)",
        "amount": round(int(t.get("amount") or 0) / 100.0, 2),  # Stripe: positive = money in
        "external_id": t["id"],
        "raw": {"id": t["id"], "transaction_refresh": t.get("transaction_refresh"), "status": t.get("status")},
    }


def _void_ids(txns: list) -> list:
    return [t["id"] for t in txns if t.get("status") == "void"]


async def _latest_refresh_id(fc_id: str) -> Optional[str]:
    acct = await ent.stripe_request("GET", f"financial_connections/accounts/{fc_id}")
    ref = acct.get("transaction_refresh") or {}
    return ref.get("id") if ref.get("status") == "succeeded" else None


async def sync_account(supabase, business: dict, row: dict, initial: bool = False, latest_refresh: Optional[str] = None) -> int:
    """Import new transactions for one connected account. `latest_refresh`
    is the refresh Stripe just finished (from the webhook); after importing
    we remember it, so next time only newer/changed transactions come back."""
    fc_id = row.get("stripe_account_id")
    if not fc_id or row.get("status") != "active":
        return 0
    if latest_refresh is None:
        try:
            latest_refresh = await _latest_refresh_id(fc_id)
        except HTTPException:
            latest_refresh = None
    txns = await _list_transactions(fc_id, row.get("last_transaction_refresh"))
    mapped = [m for m in (_map_txn(t) for t in txns) if m]

    if initial and mapped:
        # Reconnected bank: Stripe gives old transactions new ids — skip the
        # ones we already have for this account.
        existing = (
            supabase.table("rv_bank_transactions").select("posted_date, amount, normalized_description")
            .eq("business_id", business["id"]).eq("bank_account_id", row["id"]).execute()
        ).data or []
        seen = {(str(e["posted_date"]), round(float(e["amount"]), 2), e.get("normalized_description")) for e in existing}
        mapped = [m for m in mapped if (m["posted_date"], m["amount"], bank_core.normalize_description(m["description"])) not in seen]

    result = await bank_core.import_transactions(
        supabase, business, mapped, source="stripe", account_ref=row["id"], bank_account_id=row["id"],
    )
    # A transaction Stripe later voids didn't happen — keep the row, don't count it
    for vid in _void_ids(txns):
        supabase.table("rv_bank_transactions").update({"status": "ignored", "updated_at": _now()})\
            .eq("business_id", business["id"]).eq("dedupe_key", f"stripe:{vid}").execute()

    update = {"last_synced_at": _now()}
    if latest_refresh:
        update["last_transaction_refresh"] = latest_refresh
    supabase.table("rv_bank_accounts").update(update).eq("id", row["id"]).execute()
    return result["imported"]


async def handle_refreshed_transactions(supabase, fc_account: dict):
    """Webhook: Stripe finished a transaction refresh for this account."""
    rows = supabase.table("rv_bank_accounts").select("*").eq("stripe_account_id", fc_account.get("id")).execute().data or []
    if not rows:
        print(f"[connect] refresh for unknown account {fc_account.get('id')}")
        return
    row = rows[0]
    refresh = fc_account.get("transaction_refresh") or {}
    if refresh.get("status") and refresh.get("status") != "succeeded":
        print(f"[connect] refresh {refresh.get('id')} status {refresh.get('status')}")
        return
    biz = supabase.table("businesses").select("*").eq("id", row["business_id"]).execute().data
    if not biz:
        return
    await sync_account(supabase, biz[0], row, latest_refresh=refresh.get("id"))


STALE_AFTER_HOURS = 12


def _hours_since(ts: Optional[str]) -> float:
    if not ts:
        return 1e9
    try:
        then = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


async def sync_stale_accounts(supabase=None, max_age_hours: float = STALE_AFTER_HOURS) -> dict:
    """Safety net for the daily sync: don't rely only on Stripe's
    refreshed_transactions webhook. For every connected account not synced in
    `max_age_hours`, import whatever Stripe already has, then ask Stripe for a
    fresh refresh (its webhook, or the next run, brings that in)."""
    supabase = supabase or get_supabase()
    rows = (
        supabase.table("rv_bank_accounts").select("*").eq("source", "stripe").eq("status", "active").execute()
    ).data or []
    done = {"checked": len(rows), "synced": 0, "imported": 0, "skipped_plan": 0, "failed": 0}
    for row in rows:
        if _hours_since(row.get("last_synced_at")) < max_age_hours:
            continue
        biz = supabase.table("businesses").select("*").eq("id", row["business_id"]).execute().data
        if not biz:
            continue
        if not ent.has_feature(ent.account_for_business(supabase, biz[0]), "bank_connection"):
            done["skipped_plan"] += 1
            continue
        try:
            done["imported"] += await sync_account(supabase, biz[0], row)
            done["synced"] += 1
        except Exception as e:
            done["failed"] += 1
            print(f"[bank-sync] {row['id']} import failed: {getattr(e, 'detail', e)}")
        try:
            await ent.stripe_request("POST", f"financial_connections/accounts/{row['stripe_account_id']}/refresh", {"features[]": "transactions"})
        except Exception as e:  # e.g. a refresh is already running
            print(f"[bank-sync] {row['id']} refresh request: {getattr(e, 'detail', e)}")
    print(f"[bank-sync] {done}")
    return done


@connect_routes.get("/bank/accounts")
async def list_accounts(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    rows = (
        supabase.table("rv_bank_accounts").select("*").eq("business_id", business["id"])
        .order("created_at").execute()
    ).data or []
    return [to_account(r) for r in rows]


def _store_account(supabase, business_id: str, account_id: str) -> dict:
    rows = supabase.table("rv_bank_accounts").select("*").eq("id", account_id).eq("business_id", business_id).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail="Account not found")
    return rows[0]


@connect_routes.post("/bank/accounts/{account_id}/sync")
async def sync_now(account_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _role = bank_store(supabase, current_user)
    row = _store_account(supabase, business["id"], account_id)
    if row.get("source") != "stripe" or row.get("status") != "active":
        raise HTTPException(status_code=400, detail="Only connected banks can sync.")
    try:
        await ent.stripe_request("POST", f"financial_connections/accounts/{row['stripe_account_id']}/refresh", {"features[]": "transactions"})
    except HTTPException as e:
        # e.g. a refresh is already running — just import what's there
        print(f"[connect] refresh request: {e.detail}")
    imported = await sync_account(supabase, business, row)
    return {"imported": imported}


@connect_routes.post("/bank/accounts/{account_id}/disconnect")
async def disconnect(account_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, role = bank_store(supabase, current_user)
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only the store owner can disconnect a bank.")
    row = _store_account(supabase, business["id"], account_id)
    if row.get("source") == "stripe" and row.get("stripe_account_id") and row.get("status") == "active":
        for path in ("unsubscribe", "disconnect"):
            try:
                data = {"features[]": "transactions"} if path == "unsubscribe" else None
                await ent.stripe_request("POST", f"financial_connections/accounts/{row['stripe_account_id']}/{path}", data)
            except HTTPException as e:
                print(f"[connect] {path} failed: {e.detail}")
    supabase.table("rv_bank_accounts").update({"status": "disconnected", "updated_at": _now()}).eq("id", account_id).execute()
    # Transactions already imported stay — nothing is deleted.
    return to_account(_store_account(supabase, business["id"], account_id))
