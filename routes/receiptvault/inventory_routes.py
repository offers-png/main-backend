import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import create_client
from routes.receiptvault.routes import get_current_user

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
SUPABASE_KEY = (os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind6Y3V6eW91eW1hdW9raWphcWprIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM5NDUyMDAsImV4cCI6MjA4OTUyMTIwMH0.fDuyCZGrCbL9Obd7l6FDnNd5AB-AUytp-3S60KwwKvM")

inventory_routes = APIRouter(prefix="/api", tags=["inventory"])

def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_business_for_user(supabase, user_id: str):
    """OWNER + MANAGER access to inventory."""
    result = supabase.table("businesses").select("*").eq("user_id", user_id).execute()
    if result.data:
        return result.data[0]
    member = supabase.table("business_users").select("business_id, role").eq("user_id", user_id).eq("status", "active").execute()
    if member.data and member.data[0].get("role") in ("manager",):
        biz = supabase.table("businesses").select("*").eq("id", member.data[0]["business_id"]).execute()
        if biz.data:
            return biz.data[0]
    return None


def to_item(row):
    return {
        "id": row.get("id"),
        "businessId": row.get("business_id"),
        "name": row.get("name"),
        "sku": row.get("sku"),
        "category": row.get("category"),
        "quantity": float(row.get("quantity") or 0),
        "unit": row.get("unit", "units"),
        "costPrice": float(row.get("cost_price") or 0),
        "sellPrice": float(row.get("sell_price") or 0),
        "lowStockThreshold": float(row.get("low_stock_threshold") or 5),
        "notes": row.get("notes"),
        "isLowStock": float(row.get("quantity") or 0) <= float(row.get("low_stock_threshold") or 5),
        "createdAt": str(row.get("created_at", "")),
        "updatedAt": str(row.get("updated_at", "")),
    }

class CreateItemBody(BaseModel):
    name: str
    sku: Optional[str] = None
    category: Optional[str] = None
    quantity: float = 0
    unit: Optional[str] = "units"
    costPrice: Optional[float] = 0
    sellPrice: Optional[float] = 0
    lowStockThreshold: Optional[float] = 5
    notes: Optional[str] = None

class UpdateItemBody(BaseModel):
    name: Optional[str] = None
    sku: Optional[str] = None
    category: Optional[str] = None
    unit: Optional[str] = None
    costPrice: Optional[float] = None
    sellPrice: Optional[float] = None
    lowStockThreshold: Optional[float] = None
    notes: Optional[str] = None

class AdjustStockBody(BaseModel):
    type: str  # 'in' | 'out' | 'adjustment'
    quantity: float
    notes: Optional[str] = None

async def get_business_id(current_user) -> str:
    supabase = get_supabase()
    biz = type("BizR", (), {"data": ([{"id": _b["id"]}] if (_b := get_business_for_user(supabase, current_user.user.id)) else [])})()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")
    return biz.data[0]["id"]

@inventory_routes.get("/inventory")
async def list_inventory(current_user=Depends(get_current_user)):
    business_id = await get_business_id(current_user)
    supabase = get_supabase()
    rows = supabase.table("inventory_items").select("*").eq("business_id", business_id)\
        .order("name").execute()
    return [to_item(r) for r in (rows.data or [])]

@inventory_routes.post("/inventory")
async def create_item(body: CreateItemBody, current_user=Depends(get_current_user)):
    business_id = await get_business_id(current_user)
    supabase = get_supabase()
    data = {
        "business_id": business_id,
        "name": body.name,
        "sku": body.sku,
        "category": body.category,
        "quantity": body.quantity,
        "unit": body.unit or "units",
        "cost_price": body.costPrice or 0,
        "sell_price": body.sellPrice or 0,
        "low_stock_threshold": body.lowStockThreshold or 5,
        "notes": body.notes,
    }
    result = supabase.table("inventory_items").insert(data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create item")
    # Log initial stock transaction
    if body.quantity > 0:
        supabase.table("inventory_transactions").insert({
            "business_id": business_id,
            "item_id": result.data[0]["id"],
            "type": "in",
            "quantity": body.quantity,
            "notes": "Initial stock",
        }).execute()
    return to_item(result.data[0])

@inventory_routes.patch("/inventory/{item_id}")
async def update_item(item_id: str, body: UpdateItemBody, current_user=Depends(get_current_user)):
    business_id = await get_business_id(current_user)
    supabase = get_supabase()
    update_data = {"updated_at": datetime.utcnow().isoformat()}
    if body.name is not None: update_data["name"] = body.name
    if body.sku is not None: update_data["sku"] = body.sku
    if body.category is not None: update_data["category"] = body.category
    if body.unit is not None: update_data["unit"] = body.unit
    if body.costPrice is not None: update_data["cost_price"] = body.costPrice
    if body.sellPrice is not None: update_data["sell_price"] = body.sellPrice
    if body.lowStockThreshold is not None: update_data["low_stock_threshold"] = body.lowStockThreshold
    if body.notes is not None: update_data["notes"] = body.notes
    result = supabase.table("inventory_items").update(update_data)\
        .eq("id", item_id).eq("business_id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Item not found")
    return to_item(result.data[0])

@inventory_routes.post("/inventory/{item_id}/adjust")
async def adjust_stock(item_id: str, body: AdjustStockBody, current_user=Depends(get_current_user)):
    business_id = await get_business_id(current_user)
    supabase = get_supabase()
    item = supabase.table("inventory_items").select("*").eq("id", item_id)\
        .eq("business_id", business_id).execute()
    if not item.data:
        raise HTTPException(status_code=404, detail="Item not found")
    current_qty = float(item.data[0].get("quantity") or 0)
    if body.type == "in":
        new_qty = current_qty + body.quantity
    elif body.type == "out":
        new_qty = max(0, current_qty - body.quantity)
    else:  # adjustment
        new_qty = body.quantity
    supabase.table("inventory_items").update({
        "quantity": new_qty, "updated_at": datetime.utcnow().isoformat()
    }).eq("id", item_id).execute()
    supabase.table("inventory_transactions").insert({
        "business_id": business_id, "item_id": item_id,
        "type": body.type, "quantity": body.quantity, "notes": body.notes,
    }).execute()
    updated = supabase.table("inventory_items").select("*").eq("id", item_id).execute()
    return to_item(updated.data[0])

@inventory_routes.delete("/inventory/{item_id}")
async def delete_item(item_id: str, current_user=Depends(get_current_user)):
    business_id = await get_business_id(current_user)
    supabase = get_supabase()
    supabase.table("inventory_transactions").delete().eq("item_id", item_id).execute()
    supabase.table("inventory_items").delete().eq("id", item_id)\
        .eq("business_id", business_id).execute()
    return {"ok": True}

@inventory_routes.get("/inventory/{item_id}/history")
async def item_history(item_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    rows = supabase.table("inventory_transactions").select("*").eq("item_id", item_id)\
        .order("created_at", desc=True).limit(50).execute()
    return rows.data or []
