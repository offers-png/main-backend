"""
GigLedger Mileage Tracking Routes
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import os
from supabase import create_client

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

IRS_RATE_2026 = 0.70

mileage_router = APIRouter(tags=["Mileage"])


class TripStart(BaseModel):
    user_id: str
    platform: str
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None


class TripEnd(BaseModel):
    trip_id: str
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None
    miles: float


class ManualTrip(BaseModel):
    user_id: str
    platform: str
    miles: float
    date: str
    notes: Optional[str] = None


@mileage_router.post("/gig/trip/start")
async def start_trip(body: TripStart):
    result = supabase.table("mileage_entries").insert({
        "user_id": body.user_id,
        "platform": body.platform,
        "status": "active",
        "start_time": datetime.now().isoformat(),
        "start_lat": body.start_lat,
        "start_lng": body.start_lng,
        "miles": 0,
        "deduction_value": 0,
        "date": datetime.now().date().isoformat(),
    }).execute()
    return result.data[0]


@mileage_router.post("/gig/trip/end")
async def end_trip(body: TripEnd):
    deduction = round(body.miles * IRS_RATE_2026, 2)
    result = supabase.table("mileage_entries").update({
        "status": "completed",
        "end_time": datetime.now().isoformat(),
        "end_lat": body.end_lat,
        "end_lng": body.end_lng,
        "miles": round(body.miles, 2),
        "deduction_value": deduction,
    }).eq("id", body.trip_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Trip not found")
    return {**result.data[0], "irs_rate": IRS_RATE_2026, "deduction_value": deduction}


@mileage_router.post("/gig/trip/manual")
async def add_manual_trip(body: ManualTrip):
    deduction = round(body.miles * IRS_RATE_2026, 2)
    result = supabase.table("mileage_entries").insert({
        "user_id": body.user_id,
        "platform": body.platform,
        "status": "completed",
        "miles": round(body.miles, 2),
        "deduction_value": deduction,
        "date": body.date,
        "notes": body.notes,
        "start_time": datetime.now().isoformat(),
        "end_time": datetime.now().isoformat(),
    }).execute()
    return {**result.data[0], "irs_rate": IRS_RATE_2026}


@mileage_router.get("/gig/mileage/{user_id}")
async def get_mileage(user_id: str):
    now = datetime.now()
    year, month = now.year, now.month
    trips = supabase.table("mileage_entries").select("*").eq("user_id", user_id).eq("status", "completed").gte("date", f"{year}-01-01").order("date", desc=True).execute()
    data = trips.data or []
    total_miles = sum(t["miles"] for t in data)
    total_deduction = sum(t["deduction_value"] for t in data)
    month_data = [t for t in data if t["date"].startswith(f"{year}-{month:02d}")]
    active = supabase.table("mileage_entries").select("*").eq("user_id", user_id).eq("status", "active").execute()
    return {
        "total_miles_year": round(total_miles, 1),
        "total_deduction_year": round(total_deduction, 2),
        "total_miles_month": round(sum(t["miles"] for t in month_data), 1),
        "total_deduction_month": round(sum(t["deduction_value"] for t in month_data), 2),
        "irs_rate": IRS_RATE_2026,
        "recent_trips": data[:10],
        "active_trip": active.data[0] if active.data else None,
    }


@mileage_router.delete("/gig/trip/{trip_id}")
async def delete_trip(trip_id: str):
    supabase.table("mileage_entries").delete().eq("id", trip_id).execute()
    return {"deleted": True}
