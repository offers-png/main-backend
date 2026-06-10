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


def get_owner_business(supabase, user_id: str):
    """Returns business if user is the OWNER, else None."""
    result = supabase.table("businesses").select("*").eq("user_id", user_id).execute()
    return result.data[0] if result.data else None


def get_member_business(supabase, user_id: str):
    """Returns business if user is an active TEAM MEMBER, else None."""
    member = supabase.table("business_users").select("business_id, role").eq("user_id", user_id).eq("status", "active").execute()
    if member.data:
        biz = supabase.table("businesses").select("*").eq("id", member.data[0]["business_id"]).execute()
        if biz.data:
            return biz.data[0]
    return None


def resolve_access(supabase, user_id: str):
    """Returns (business, is_owner). Raises 404 if no business."""
    biz = get_owner_business(supabase, user_id)
    if biz:
        return biz, True
    biz = get_member_business(supabase, user_id)
    if biz:
        return biz, False
    raise HTTPException(status_code=404, detail="Business not found")


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
        "approvalStatus": row.get("approval_status", "approved"),
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

@mileage_routes.get("/mileage")
async def list_mileage(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, is_owner = resolve_access(supabase, current_user.user.id)

    query = supabase.table("rv_mileage_entries").select("*").eq("business_id", business["id"])
    if not is_owner:
        # Employees only see their own entries
        query = query.eq("user_id", current_user.user.id)
    rows = query.order("date", desc=True).execute()

    entries = [to_entry(r) for r in (rows.data or [])]
    # Deduction totals only count approved entries
    approved = [e for e in entries if e["approvalStatus"] == "approved"]
    total_miles = sum(e["miles"] for e in approved)
    total_deduction = sum(e["deduction"] for e in approved)
    pending_count = sum(1 for e in entries if e["approvalStatus"] == "pending")
    return {
        "entries": entries,
        "totalMiles": round(total_miles, 2),
        "totalDeduction": round(total_deduction, 2),
        "pendingCount": pending_count,
        "irsRate": IRS_RATE,
        "isOwner": is_owner,
    }

@mileage_routes.post("/mileage")
async def create_mileage(body: CreateMileageBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, is_owner = resolve_access(supabase, current_user.user.id)
    data = {
        "business_id": business["id"],
        "user_id": current_user.user.id,
        "date": body.date or str(date.today()),
        "start_location": body.startLocation,
        "end_location": body.endLocation,
        "miles": body.miles,
        "purpose": body.purpose,
        "approval_status": "approved" if is_owner else "pending",
    }
    result = supabase.table("rv_mileage_entries").insert(data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create entry")
    return to_entry(result.data[0])

@mileage_routes.patch("/mileage/{entry_id}")
async def update_mileage(entry_id: str, body: UpdateMileageBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, is_owner = resolve_access(supabase, current_user.user.id)
    update_data = {}
    if body.date is not None: update_data["date"] = body.date
    if body.startLocation is not None: update_data["start_location"] = body.startLocation
    if body.endLocation is not None: update_data["end_location"] = body.endLocation
    if body.miles is not None: update_data["miles"] = body.miles
    if body.purpose is not None: update_data["purpose"] = body.purpose

    query = supabase.table("rv_mileage_entries").update(update_data)\
        .eq("id", entry_id).eq("business_id", business["id"])
    if not is_owner:
        # Employees can only edit their own pending entries
        query = query.eq("user_id", current_user.user.id).eq("approval_status", "pending")
    result = query.execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Entry not found or not editable")
    return to_entry(result.data[0])

@mileage_routes.delete("/mileage/{entry_id}")
async def delete_mileage(entry_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, is_owner = resolve_access(supabase, current_user.user.id)
    query = supabase.table("rv_mileage_entries").delete()\
        .eq("id", entry_id).eq("business_id", business["id"])
    if not is_owner:
        query = query.eq("user_id", current_user.user.id).eq("approval_status", "pending")
    query.execute()
    return {"ok": True}

@mileage_routes.post("/mileage/{entry_id}/approve")
async def approve_mileage(entry_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business = get_owner_business(supabase, current_user.user.id)
    if not business:
        raise HTTPException(status_code=403, detail="Only the owner can approve mileage")
    result = supabase.table("rv_mileage_entries").update({"approval_status": "approved"})\
        .eq("id", entry_id).eq("business_id", business["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Entry not found")
    return to_entry(result.data[0])

@mileage_routes.post("/mileage/{entry_id}/reject")
async def reject_mileage(entry_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business = get_owner_business(supabase, current_user.user.id)
    if not business:
        raise HTTPException(status_code=403, detail="Only the owner can reject mileage")
    result = supabase.table("rv_mileage_entries").update({"approval_status": "rejected"})\
        .eq("id", entry_id).eq("business_id", business["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Entry not found")
    return to_entry(result.data[0])
