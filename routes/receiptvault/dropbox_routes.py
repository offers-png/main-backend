"""
Phase 4 — Accountant Dropbox & Trusted Payment Links.

Flow:
  1. Owner calls POST /api/dropbox/generate -> gets a single-use token/link.
  2. Owner shares that link with their accountant (outside this system).
  3. Accountant opens GET /api/dropbox/{token} (no auth) to see the drop page.
  4. Accountant submits GET's uploads + three numbers via
     POST /api/dropbox/{token}/submit (no auth, single use).
  5. We attach the correct federal/state payment links SERVER-SIDE (never
     trusting anything in the accountant's submission), mark the token used,
     and notify the owner immediately (SMS + email).
"""

import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from supabase import create_client

from routes.receiptvault.routes import get_current_user, resolve_role

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
SUPABASE_KEY = (os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind6Y3V6eW91eW1hdW9raWphcWprIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM5NDUyMDAsImV4cCI6MjA4OTUyMTIwMH0.fDuyCZGrCbL9Obd7l6FDnNd5AB-AUytp-3S60KwwKvM")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://receipts.dealdily.com")
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY", "")
TELNYX_FROM = os.getenv("TELNYX_FROM_NUMBER", "+13156252025")
SEND_EMAIL_URL = f"{SUPABASE_URL}/functions/v1/send-invoice"

TOKEN_TTL_DAYS = 30

dropbox_routes = APIRouter(prefix="/api/dropbox", tags=["dropbox"])
feedback_routes = APIRouter(prefix="/api", tags=["feedback"])


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Generate ─────────────────────────────────────────────────────────────

class GenerateDropboxBody(BaseModel):
    label: Optional[str] = None


@dropbox_routes.post("/generate")
async def generate_dropbox_link(body: GenerateDropboxBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, role = resolve_role(supabase, current_user.user.id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    if role not in ("owner", "bookkeeper"):
        raise HTTPException(status_code=403, detail="Only the owner or bookkeeper can generate a dropbox link")

    token = secrets.token_urlsafe(24)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)).isoformat()

    result = supabase.table("rv_dropbox_tokens").insert({
        "business_id": business["id"],
        "token": token,
        "created_by": current_user.user.id,
        "expires_at": expires_at,
        "label": body.label,
    }).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create dropbox link")

    return {
        "token": token,
        "url": f"{FRONTEND_URL}/dropbox/{token}",
        "expiresAt": expires_at,
    }


# ── Fetch / validate a token (accountant-facing, no auth) ──────────────────

def _get_valid_token_row(supabase, token: str) -> dict:
    rows = supabase.table("rv_dropbox_tokens").select("*").eq("token", token).execute()
    if not rows.data:
        raise HTTPException(status_code=404, detail="This dropbox link is invalid.")
    row = rows.data[0]
    if row.get("used_at"):
        raise HTTPException(status_code=410, detail="This dropbox link has already been used.")
    expires_at = row.get("expires_at")
    if expires_at and expires_at < datetime.now(timezone.utc).isoformat():
        raise HTTPException(status_code=410, detail="This dropbox link has expired.")
    return row


@dropbox_routes.get("/{token}")
async def get_dropbox(token: str):
    supabase = get_supabase()
    row = _get_valid_token_row(supabase, token)
    biz = supabase.table("businesses").select("business_name, owner_name").eq("id", row["business_id"]).execute()
    business_name = biz.data[0]["business_name"] if biz.data else ""
    return {
        "businessName": business_name,
        "label": row.get("label"),
        "expiresAt": row.get("expires_at"),
    }


# ── Submit (accountant-facing, no auth, single use) ─────────────────────────

@dropbox_routes.post("/{token}/submit")
async def submit_dropbox(token: str, request: Request):
    supabase = get_supabase()
    row = _get_valid_token_row(supabase, token)
    business_id = row["business_id"]

    form = await request.form()

    def _to_amount(field: str) -> float:
        raw = form.get(field)
        if raw is None or str(raw).strip() == "":
            raise HTTPException(status_code=400, detail=f"{field} is required")
        try:
            return round(float(raw), 2)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{field} must be a number")

    federal_owed = _to_amount("federalOwed")
    state_owed = _to_amount("stateOwed")
    local_owed = _to_amount("localOwed")

    # Accept one or many files under "files"
    uploaded_files = []
    file_items = form.getlist("files") if hasattr(form, "getlist") else []
    for f in file_items:
        if not hasattr(f, "read"):
            continue
        content = await f.read()
        ext = os.path.splitext(f.filename or "")[1] or ".bin"
        object_path = f"dropbox-submissions/{business_id}/{token}/{int(time.time() * 1000)}-{secrets.token_hex(4)}{ext}"
        supabase.storage.from_("receipts").upload(
            object_path, content, {"content-type": f.content_type or "application/octet-stream"}
        )
        uploaded_files.append({
            "name": f.filename,
            "path": f"{SUPABASE_URL}/storage/v1/object/public/receipts/{object_path}",
        })

    # ── Attach the correct payment links SERVER-SIDE ────────────────────────
    # The accountant's submission has no free-text or link field — nothing
    # they send can influence which payment link gets shown.
    biz = supabase.table("businesses").select("*").eq("id", business_id).execute()
    business = biz.data[0] if biz.data else {}
    state_code = (business.get("state") or "").strip().upper()

    links = supabase.table("rv_payment_links").select("*").execute().data or []
    federal_link = next((l["url"] for l in links if l["jurisdiction_type"] == "federal"), None)
    state_link = next((l["url"] for l in links if l["jurisdiction_type"] == "state" and l["state_code"] == state_code), None)

    supabase.table("rv_dropbox_tokens").update({
        "used_at": datetime.now(timezone.utc).isoformat(),
        "federal_owed": federal_owed,
        "state_owed": state_owed,
        "local_owed": local_owed,
        "uploaded_files": uploaded_files,
        "federal_link": federal_link,
        "state_link": state_link,
    }).eq("id", row["id"]).execute()

    await _notify_owner(business, federal_owed, state_owed, local_owed, federal_link, state_link, len(uploaded_files))

    return {"ok": True}


async def _notify_owner(business: dict, federal_owed: float, state_owed: float, local_owed: float,
                         federal_link: Optional[str], state_link: Optional[str], file_count: int):
    biz_name = business.get("business_name", "your business")
    owner_email = business.get("owner_email")
    owner_phone = business.get("owner_phone")
    total_owed = federal_owed + state_owed + local_owed

    if owner_phone and TELNYX_API_KEY:
        sms_body = (
            f"ReceiptVault: Your accountant submitted {file_count} document(s) for {biz_name}. "
            f"Federal ${federal_owed:.2f}, State ${state_owed:.2f}, Local ${local_owed:.2f}. "
            f"Check your email for payment links."
        )
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    "https://api.telnyx.com/v2/messages",
                    headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
                    json={"from": TELNYX_FROM, "to": owner_phone, "text": sms_body},
                )
        except Exception as e:
            print(f"[dropbox] owner SMS failed: {e}")

    if owner_email:
        links_html = ""
        if federal_link:
            links_html += f'<p>Federal (${federal_owed:.2f} owed): <a href="{federal_link}">Pay via IRS Direct Pay</a></p>'
        if state_link:
            links_html += f'<p>State (${state_owed:.2f} owed): <a href="{state_link}">Pay your state Dept. of Revenue</a></p>'
        if local_owed:
            links_html += f'<p>Local: ${local_owed:.2f} owed (pay through your local jurisdiction)</p>'

        html = f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto">
  <div style="background:#1a6b3a;padding:24px 32px;border-radius:12px 12px 0 0">
    <h1 style="color:white;margin:0;font-size:20px">ReceiptVault</h1>
  </div>
  <div style="background:#f2faf5;padding:24px 32px">
    <p>Your accountant just submitted {file_count} document(s) for <b>{biz_name}</b>.</p>
    <p><b>Total owed: ${total_owed:.2f}</b></p>
    {links_html}
    <p style="color:#9c9c96;font-size:12px;margin-top:24px">These are the official payment links we maintain &mdash; your accountant's submission cannot change them.</p>
  </div>
</div>"""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    SEND_EMAIL_URL,
                    headers={"apikey": SUPABASE_KEY, "Content-Type": "application/json"},
                    json={"to": owner_email, "from": "ReceiptVault", "subject": "Your accountant submitted your tax documents", "html": html},
                )
        except Exception as e:
            print(f"[dropbox] owner email failed: {e}")


# ── Feedback (optional, never blocking) ─────────────────────────────────────

class FeedbackBody(BaseModel):
    message: str


@feedback_routes.post("/feedback")
async def submit_feedback(body: FeedbackBody, current_user=Depends(get_current_user)):
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    try:
        supabase = get_supabase()
        business, _role = resolve_role(supabase, current_user.user.id)
        supabase.table("rv_feedback").insert({
            "business_id": business["id"] if business else None,
            "user_id": current_user.user.id,
            "message": message,
        }).execute()
    except Exception as e:
        # Feedback must never block the user — log and report success anyway.
        print(f"[feedback] failed to store feedback: {e}")
    return {"ok": True}
