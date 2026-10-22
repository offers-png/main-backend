"""
Store list / add-a-store / all-stores summary.

  GET  /api/stores          -> every store this login can switch to
  POST /api/stores          -> owner adds another store (same fields as setup)
  GET  /api/stores/summary  -> total in / out / profit per owned store
"""

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from routes.receiptvault.routes import get_current_user, get_supabase, to_business, generate_referral_code
from routes.receiptvault.stores import list_stores_for_user, owned_stores

store_routes = APIRouter(prefix="/api", tags=["stores"])


@store_routes.get("/stores")
async def list_stores(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    return list_stores_for_user(supabase, current_user.user.id)


class NewStoreBody(BaseModel):
    businessName: str
    businessAddress: str
    ownerName: Optional[str] = None
    ownerAddress: Optional[str] = None
    state: Optional[str] = None
    taxId: Optional[str] = None


@store_routes.post("/stores", status_code=201)
async def create_store(body: NewStoreBody, current_user=Depends(get_current_user)):
    """An owner adds another store. Only people who already own a store can
    use this; brand-new signups create their first store via /setup/business."""
    from routes.receiptvault.entitlements import check_can_add_store, sync_store_addon_quantity

    supabase = get_supabase()
    user_id = current_user.user.id
    owned = owned_stores(supabase, user_id)
    if not owned:
        raise HTTPException(status_code=400, detail="Set up your first store before adding another.")
    active_count = len([b for b in owned if not b.get("archived_at")])
    if not (body.businessName or "").strip() or not (body.businessAddress or "").strip():
        raise HTTPException(status_code=400, detail="Store name and address are required.")

    check_can_add_store(supabase, user_id, active_count)

    first = owned[0]
    data = {
        "user_id": user_id,
        "business_name": body.businessName.strip(),
        "business_address": body.businessAddress.strip(),
        # Owner details default to the first store's so the owner doesn't retype them
        "owner_name": body.ownerName or first.get("owner_name"),
        "owner_address": body.ownerAddress or first.get("owner_address"),
        "owner_email": current_user.user.email,
        "owner_phone": first.get("owner_phone"),
        "state": (body.state or first.get("state") or "").strip().upper()[:2] or None,
        "tax_id": body.taxId,
        "week_start_day": first.get("week_start_day") or 0,
        "referral_code": generate_referral_code(supabase),
    }
    result = supabase.table("businesses").insert(data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Couldn't add the store. Please try again.")

    # Bill the extra store as an add-on on the owner's subscription
    await sync_store_addon_quantity(supabase, user_id)

    out = to_business(result.data[0])
    out["role"] = "owner"
    out["setupRequired"] = "accountant"
    return out


@store_routes.get("/stores/summary")
async def stores_summary(start: Optional[str] = None, end: Optional[str] = None, current_user=Depends(get_current_user)):
    """Money in / out / profit for every store the user owns (owners with
    2+ stores see this on the dashboard). Defaults to this month so far."""
    from routes.receiptvault.pnl_routes import _totals_for_period

    supabase = get_supabase()
    owned = owned_stores(supabase, current_user.user.id)
    if start and end:
        try:
            datetime.strptime(start, "%Y-%m-%d")
            datetime.strptime(end, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="start and end must be YYYY-MM-DD")
    else:
        today = date.today()
        start, end = today.replace(day=1).isoformat(), today.isoformat()

    stores = []
    for b in owned:
        total_in, total_out, _ = _totals_for_period(supabase, b["id"], start, end, "category")
        stores.append({
            "id": b["id"],
            "businessName": b.get("business_name"),
            "totalIn": round(total_in, 2),
            "totalOut": round(total_out, 2),
            "profit": round(total_in - total_out, 2),
        })
    return {
        "periodStart": start,
        "periodEnd": end,
        "stores": stores,
        "totals": {
            "totalIn": round(sum(s["totalIn"] for s in stores), 2),
            "totalOut": round(sum(s["totalOut"] for s in stores), 2),
            "profit": round(sum(s["profit"] for s in stores), 2),
        },
    }


def _owned_store_or_404(supabase, user_id: str, store_id: str) -> dict:
    for b in owned_stores(supabase, user_id):
        if b["id"] == store_id:
            return b
    raise HTTPException(status_code=404, detail="Store not found")


@store_routes.post("/stores/{store_id}/archive")
async def archive_store(store_id: str, current_user=Depends(get_current_user)):
    """Archive (never delete) an extra store: its data stays, it becomes
    view-only and stops counting toward the bill."""
    from routes.receiptvault.entitlements import sync_store_addon_quantity, active_owned_stores
    supabase = get_supabase()
    user_id = current_user.user.id
    store = _owned_store_or_404(supabase, user_id, store_id)
    if not store.get("archived_at"):
        if len(active_owned_stores(supabase, user_id)) <= 1:
            raise HTTPException(status_code=400, detail="You need at least one open store.")
        supabase.table("businesses").update({"archived_at": datetime.utcnow().isoformat()}).eq("id", store_id).execute()
        await sync_store_addon_quantity(supabase, user_id)
    return {"ok": True}


@store_routes.post("/stores/{store_id}/restore")
async def restore_store(store_id: str, current_user=Depends(get_current_user)):
    from routes.receiptvault.entitlements import check_can_add_store, sync_store_addon_quantity, active_owned_stores
    supabase = get_supabase()
    user_id = current_user.user.id
    store = _owned_store_or_404(supabase, user_id, store_id)
    if store.get("archived_at"):
        check_can_add_store(supabase, user_id, len(active_owned_stores(supabase, user_id)))
        supabase.table("businesses").update({"archived_at": None}).eq("id", store_id).execute()
        await sync_store_addon_quantity(supabase, user_id)
    return {"ok": True}
