import os
from datetime import datetime, date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import create_client
from routes.receiptvault.routes import get_current_user

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
SUPABASE_KEY = (os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind6Y3V6eW91eW1hdW9raWphcWprIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM5NDUyMDAsImV4cCI6MjA4OTUyMTIwMH0.fDuyCZGrCbL9Obd7l6FDnNd5AB-AUytp-3S60KwwKvM")

IRS_RATE = 0.70  # 2026 IRS rate

mileage_routes = APIRouter(prefix="/api", tags=["mileage"])

def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_business_for_user(supabase, user_id: str):
    """OWNER ONLY — employees cannot access this module."""
    result = supabase.table("businesses").select("*").eq("user_id", user_id).execute()
    if result.data:
        return result.data[0]
    return None


def to_entry(row):
    miles = float(row.get("miles") or 0)
    return {
        "id": row.get("id"),
        "businessId": row.get("business_id"),
        "userId": row.get("user_id"),
        "date": str(row.get("date", "")),
        "startLocation": row.get("start_location"),
        "endLocation": row.get("end_location"),
        "miles": miles,
        "purpose": row.get("purpose"),
        "deduction": round(miles * IRS_RATE, 2),
        "createdAt": str(row.get("created_at", "")),
    }

class CreateMileageBody(BaseModel):
    date: Optional[str] = None
    startLocation: Optional[str] = None
    endLocation: Optional[str] = None
    miles: float
    purpose: Optional[str] = None

class UpdateMileageBody(BaseModel):
    date: Optional[str] = None
    startLocation: Optional[str] = None
    endLocation: Optional[str] = None
    miles: Optional[float] = None
    purpose: Optional[str] = None

async def get_business_id(current_user) -> str:
    supabase = get_supabase()
    biz = type("BizR", (), {"data": ([{"id": _b["id"]}] if (_b := get_business_for_user(supabase, current_user.user.id)) else [])})()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")
    return biz.data[0]["id"]

@mileage_routes.get("/mileage")
async def list_mileage(current_user=Depends(get_current_user)):
    business_id = await get_business_id(current_user)
    supabase = get_supabase()
    rows = supabase.table("rv_mileage_entries").select("*")\
        .eq("business_id", business_id)\
        .order("date", desc=True).execute()
    entries = [to_entry(r) for r in (rows.data or [])]
    total_miles = sum(e["miles"] for e in entries)
    total_deduction = sum(e["deduction"] for e in entries)
    return {
        "entries": entries,
        "totalMiles": round(total_miles, 2),
        "totalDeduction": round(total_deduction, 2),
        "irsRate": IRS_RATE,
    }

@mileage_routes.post("/mileage")
async def create_mileage(body: CreateMileageBody, current_user=Depends(get_current_user)):
    business_id = await get_business_id(current_user)
    supabase = get_supabase()
    data = {
        "business_id": business_id,
        "user_id": current_user.user.id,
        "date": body.date or str(date.today()),
        "start_location": body.startLocation,
        "end_location": body.endLocation,
        "miles": body.miles,
        "purpose": body.purpose,
    }
    result = supabase.table("rv_mileage_entries").insert(data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create entry")
    return to_entry(result.data[0])

@mileage_routes.patch("/mileage/{entry_id}")
async def update_mileage(entry_id: str, body: UpdateMileageBody, current_user=Depends(get_current_user)):
    business_id = await get_business_id(current_user)
    supabase = get_supabase()
    update_data = {}
    if body.date is not None: update_data["date"] = body.date
    if body.startLocation is not None: update_data["start_location"] = body.startLocation
    if body.endLocation is not None: update_data["end_location"] = body.endLocation
    if body.miles is not None: update_data["miles"] = body.miles
    if body.purpose is not None: update_data["purpose"] = body.purpose
    result = supabase.table("rv_mileage_entries").update(update_data)\
        .eq("id", entry_id).eq("business_id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Entry not found")
    return to_entry(result.data[0])

@mileage_routes.delete("/mileage/{entry_id}")
async def delete_mileage(entry_id: str, current_user=Depends(get_current_user)):
    business_id = await get_business_id(current_user)
    supabase = get_supabase()
    supabase.table("rv_mileage_entries").delete()\
        .eq("id", entry_id).eq("business_id", business_id).execute()
    return {"ok": True}
