import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import create_client
from routes.receiptvault.routes import get_current_user
import httpx

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

team_routes = APIRouter(prefix="/api", tags=["team"])

def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def to_member(row):
    return {
        "id": row.get("id"),
        "businessId": row.get("business_id"),
        "userId": row.get("user_id"),
        "email": row.get("email"),
        "role": row.get("role", "employee"),
        "status": row.get("status", "active"),
        "invitedAt": str(row.get("invited_at", "")),
        "joinedAt": str(row.get("joined_at", "")) if row.get("joined_at") else None,
    }

class InviteMemberBody(BaseModel):
    email: str
    role: Optional[str] = "employee"  # employee | manager

class UpdateMemberBody(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None

async def get_business(current_user):
    supabase = get_supabase()
    biz = supabase.table("businesses").select("*").eq("user_id", current_user.user.id).execute()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")
    return biz.data[0]

@team_routes.get("/team")
async def list_team(current_user=Depends(get_current_user)):
    business = await get_business(current_user)
    supabase = get_supabase()
    rows = supabase.table("business_users").select("*")\
        .eq("business_id", business["id"]).order("created_at").execute()
    return [to_member(r) for r in (rows.data or [])]

@team_routes.post("/team/invite")
async def invite_member(body: InviteMemberBody, current_user=Depends(get_current_user)):
    business = await get_business(current_user)
    supabase = get_supabase()

    # Check if already invited
    existing = supabase.table("business_users").select("id")\
        .eq("business_id", business["id"]).eq("email", body.email).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail="This person is already on your team")

    # Create pending invite record
    result = supabase.table("business_users").insert({
        "business_id": business["id"],
        "user_id": f"pending_{body.email}",
        "email": body.email,
        "role": body.role or "employee",
        "status": "invited",
    }).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create invite")

    # Send invite email via Resend
    biz_name = business.get("business_name", "Your employer")
    invite_link = f"https://receipts.dealdily.com/auth?invite={business['id']}&email={body.email}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={
                    "from": f"ReceiptVault <invites@dealdily.com>",
                    "to": [body.email],
                    "subject": f"You've been invited to join {biz_name} on ReceiptVault",
                    "html": f"""
                    <div style="font-family:sans-serif;max-width:500px;margin:0 auto">
                      <div style="background:#1a6b3a;padding:24px 32px;border-radius:12px 12px 0 0">
                        <h1 style="color:white;margin:0;font-size:20px">ReceiptVault</h1>
                      </div>
                      <div style="background:#f2faf5;padding:24px 32px">
                        <p>You've been invited to join <b>{biz_name}</b> on ReceiptVault as a <b>{body.role}</b>.</p>
                        <p>Once you sign up, you can submit receipts for approval.</p>
                        <a href="{invite_link}" style="display:inline-block;background:#1a6b3a;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;margin-top:8px">Accept Invitation</a>
                        <p style="color:#9c9c96;font-size:12px;margin-top:24px">ReceiptVault · receipts.dealdily.com</p>
                      </div>
                    </div>
                    """,
                }
            )
    except Exception as e:
        print(f"Invite email failed: {e}")

    return to_member(result.data[0])

@team_routes.patch("/team/{member_id}")
async def update_member(member_id: str, body: UpdateMemberBody, current_user=Depends(get_current_user)):
    business = await get_business(current_user)
    supabase = get_supabase()
    update_data = {}
    if body.role is not None: update_data["role"] = body.role
    if body.status is not None: update_data["status"] = body.status
    result = supabase.table("business_users").update(update_data)\
        .eq("id", member_id).eq("business_id", business["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Member not found")
    return to_member(result.data[0])

@team_routes.delete("/team/{member_id}")
async def remove_member(member_id: str, current_user=Depends(get_current_user)):
    business = await get_business(current_user)
    supabase = get_supabase()
    supabase.table("business_users").delete()\
        .eq("id", member_id).eq("business_id", business["id"]).execute()
    return {"ok": True}

# ── Receipt approval endpoints ────────────────────────────────────────────────

@team_routes.get("/receipts/pending-approval")
async def pending_approvals(current_user=Depends(get_current_user)):
    business = await get_business(current_user)
    supabase = get_supabase()
    rows = supabase.table("receipts").select("*")\
        .eq("business_id", business["id"])\
        .eq("approval_status", "pending").execute()
    return rows.data or []

@team_routes.post("/receipts/{receipt_id}/approve")
async def approve_receipt(receipt_id: str, current_user=Depends(get_current_user)):
    business = await get_business(current_user)
    supabase = get_supabase()
    supabase.table("receipts").update({
        "approval_status": "approved",
        "approved_by": current_user.user.id,
    }).eq("id", receipt_id).eq("business_id", business["id"]).execute()
    return {"ok": True}

@team_routes.post("/receipts/{receipt_id}/reject")
async def reject_receipt(receipt_id: str, current_user=Depends(get_current_user)):
    business = await get_business(current_user)
    supabase = get_supabase()
    supabase.table("receipts").update({
        "approval_status": "rejected",
        "approved_by": current_user.user.id,
    }).eq("id", receipt_id).eq("business_id", business["id"]).execute()
    return {"ok": True}
