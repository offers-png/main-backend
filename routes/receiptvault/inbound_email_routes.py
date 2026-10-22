"""
Email receipt forwarding — Growth and Business.

Every store gets a private address r-<token>@<INBOUND_EMAIL_DOMAIN>
(default in.receiptkept.com). The subdomain's MX record points at Resend
Inbound, which POSTs an `email.received` webhook here. We then:

  1. verify the Svix signature (RESEND_INBOUND_WEBHOOK_SECRET)
  2. find the store by token (unknown tokens are ignored)
  3. skip anything over 20 MB, rate-limit per store, never process twice
  4. save each PDF/image attachment as a receipt (AI fills merchant,
     amount, date, category) — or, with no attachment, the email itself
  5. match each new receipt to bank charges (inside save_receipt)

  GET  /api/inbound/address         -> this store's address (created on first view)
  POST /api/inbound/address/rotate  -> new address (old one stops working)
  GET  /api/inbound/recent          -> last forwarded emails
  POST /api/inbound/resend          -> Resend webhook (public, signed)
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from routes.receiptvault.routes import get_current_user, get_supabase as _rv_get_supabase, save_receipt, to_receipt
from routes.receiptvault.stores import resolve_store
from routes.receiptvault import entitlements as ent

inbound_routes = APIRouter(prefix="/api", tags=["inbound-email"])        # store-facing (gated)
inbound_webhook_routes = APIRouter(prefix="/api", tags=["inbound-email"])  # Resend (public)

MAX_TOTAL_BYTES = 20 * 1024 * 1024
RATE_PER_HOUR = 30
RATE_PER_DAY = 200
TOLERANCE_SECONDS = 300
SKIP_INLINE_UNDER = 15 * 1024  # inline logos / tracking pixels
RECEIPT_TYPES = ("application/pdf", "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "image/heic")


def get_supabase():
    return _rv_get_supabase()


def inbound_domain() -> str:
    return os.getenv("INBOUND_EMAIL_DOMAIN", "in.receiptkept.com").lower()


def address_for(token: str) -> str:
    return f"r-{token}@{inbound_domain()}"


def _new_token() -> str:
    # lowercase letters + digits only: safe in every mail system, hard to guess
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _store_row(supabase, business_id: str) -> Optional[dict]:
    rows = supabase.table("rv_inbound_addresses").select("*").eq("business_id", business_id).execute().data
    return rows[0] if rows else None


def _ensure_address(supabase, business_id: str) -> dict:
    row = _store_row(supabase, business_id)
    if row:
        return row
    return supabase.table("rv_inbound_addresses").insert({"business_id": business_id, "token": _new_token()}).execute().data[0]


def _staff_store(supabase, current_user):
    business, role, _m = resolve_store(supabase, current_user.user.id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    if role not in ("owner", "manager", "bookkeeper"):
        raise HTTPException(status_code=403, detail="Only the owner, a manager or the bookkeeper can see the forwarding address.")
    return business, role


@inbound_routes.get("/inbound/address")
async def get_address(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _role = _staff_store(supabase, current_user)
    row = _ensure_address(supabase, business["id"])
    return {"address": address_for(row["token"]), "createdAt": str(row.get("created_at") or "")}


@inbound_routes.post("/inbound/address/rotate")
async def rotate_address(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, role = _staff_store(supabase, current_user)
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can change the forwarding address.")
    row = _ensure_address(supabase, business["id"])
    token = _new_token()
    supabase.table("rv_inbound_addresses").update({"token": token, "rotated_at": datetime.now(timezone.utc).isoformat()}).eq("id", row["id"]).execute()
    return {"address": address_for(token)}


@inbound_routes.get("/inbound/recent")
async def recent(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, _role = _staff_store(supabase, current_user)
    rows = (
        supabase.table("rv_inbound_emails").select("*").eq("business_id", business["id"])
        .order("created_at", desc=True).limit(10).execute()
    ).data or []
    return [{
        "id": r["id"], "from": r.get("from_address"), "subject": r.get("subject"), "status": r.get("status"),
        "receiptsCreated": r.get("receipts_created") or 0, "createdAt": str(r.get("created_at") or ""),
    } for r in rows]


# ── Webhook ─────────────────────────────────────────────────────────────────

def verify_svix(payload: bytes, headers, secret: str, now: Optional[float] = None) -> bool:
    msg_id = headers.get("svix-id") or headers.get("webhook-id")
    ts = headers.get("svix-timestamp") or headers.get("webhook-timestamp")
    sigs = headers.get("svix-signature") or headers.get("webhook-signature") or ""
    if not (msg_id and ts and sigs and secret):
        return False
    try:
        if abs((now or time.time()) - int(ts)) > TOLERANCE_SECONDS:
            return False
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except Exception:
        return False
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + payload, hashlib.sha256).digest()).decode()
    for part in sigs.split(" "):
        _v, _, sig = part.partition(",")
        if sig and hmac.compare_digest(sig, expected):
            return True
    return False


_TOKEN_RE = None


def tokens_in(addresses) -> list:
    global _TOKEN_RE
    if _TOKEN_RE is None:
        _TOKEN_RE = re.compile(r"r-([a-z0-9]{8,32})@" + re.escape(inbound_domain()), re.I)
    out = []
    for a in addresses or []:
        text = a if isinstance(a, str) else (a.get("email") or a.get("address") or "")
        out += [m.lower() for m in _TOKEN_RE.findall(text)]
    return list(dict.fromkeys(out))


async def _resend_get(path: str) -> dict:
    key = os.getenv("RESEND_API_KEY", "")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"https://api.resend.com/{path}", headers={"Authorization": f"Bearer {key}"})
    if r.status_code >= 400:
        raise RuntimeError(f"Resend {path} -> {r.status_code}")
    return r.json()


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        r = await client.get(url)
    if r.status_code >= 400:
        raise RuntimeError(f"download failed {r.status_code}")
    return r.content


async def _html_to_pdf(html: str) -> Optional[bytes]:
    """Optional: PDFShift (already used for accountant PDFs) turns the
    email into a PDF so it looks like a receipt. Without a key we keep the
    email as an .html file instead."""
    key = os.getenv("PDFSHIFT_API_KEY")
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post("https://api.pdfshift.io/v3/convert/pdf", headers={"X-API-Key": key}, json={"source": html})
        return r.content if r.status_code == 200 else None
    except Exception as e:
        print(f"[inbound] pdfshift failed: {e}")
        return None


def _log(supabase, data: dict) -> dict:
    return supabase.table("rv_inbound_emails").insert(data).execute().data[0]


def _rate_limited(supabase, business_id: str) -> bool:
    now = datetime.now(timezone.utc)
    rows = (
        supabase.table("rv_inbound_emails").select("created_at").eq("business_id", business_id)
        .gte("created_at", (now - timedelta(days=1)).isoformat()).execute()
    ).data or []
    hour_ago = (now - timedelta(hours=1)).isoformat()
    return len(rows) >= RATE_PER_DAY or sum(1 for r in rows if str(r["created_at"]) >= hour_ago) >= RATE_PER_HOUR


@inbound_webhook_routes.post("/inbound/resend")
async def resend_inbound(request: Request):
    payload = await request.body()
    secret = os.getenv("RESEND_INBOUND_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(status_code=500, detail="Inbound email not configured")
    if not verify_svix(payload, request.headers, secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    import json
    event = json.loads(payload)
    if event.get("type") != "email.received":
        return {"ok": True, "ignored": "event"}
    data = event.get("data") or {}
    email_id = data.get("email_id") or data.get("id")
    if not email_id:
        return {"ok": True, "ignored": "no id"}

    supabase = get_supabase()
    # Find the store from the webhook's recipients (no API call for junk)
    tokens = tokens_in((data.get("to") or []) + (data.get("cc") or []) + (data.get("received_for") or []))
    if not tokens:
        return {"ok": True, "ignored": "no address"}
    addr = None
    for t in tokens:
        rows = supabase.table("rv_inbound_addresses").select("*").eq("token", t).execute().data
        if rows:
            addr = rows[0]
            break
    if not addr:
        return {"ok": True, "ignored": "unknown address"}  # only valid tokens are accepted
    business_id = addr["business_id"]

    if supabase.table("rv_inbound_emails").select("id").eq("resend_email_id", email_id).execute().data:
        return {"ok": True, "ignored": "duplicate"}

    biz_rows = supabase.table("businesses").select("*").eq("id", business_id).execute().data
    if not biz_rows:
        return {"ok": True, "ignored": "store gone"}
    business = biz_rows[0]
    base_log = {
        "business_id": business_id, "resend_email_id": email_id,
        "from_address": str(data.get("from") or "")[:200], "subject": str(data.get("subject") or "")[:300],
    }

    account = ent.account_for_business(supabase, business)
    if not ent.has_feature(account, "email_forwarding"):
        _log(supabase, {**base_log, "status": "not_on_plan"})
        return {"ok": True, "ignored": "plan"}
    if business_id in ent.read_only_store_ids(supabase, str(business["user_id"]), account):
        _log(supabase, {**base_log, "status": "store_read_only"})
        return {"ok": True, "ignored": "read only"}
    if _rate_limited(supabase, business_id):
        _log(supabase, {**base_log, "status": "rate_limited"})
        return {"ok": True, "ignored": "rate limited"}

    attachments_meta = data.get("attachments") or []
    if sum(int(a.get("size") or 0) for a in attachments_meta) > MAX_TOTAL_BYTES:
        _log(supabase, {**base_log, "status": "too_large"})
        return {"ok": True, "ignored": "too large"}

    log = _log(supabase, {**base_log, "status": "processing"})
    created = []
    try:
        receipt_files = []
        if attachments_meta:
            listing = await _resend_get(f"emails/receiving/{email_id}/attachments")
            for a in listing.get("data") or []:
                ctype = (a.get("content_type") or "").lower()
                size = int(a.get("size") or 0)
                if ctype not in RECEIPT_TYPES:
                    continue
                if a.get("content_disposition") == "inline" and size < SKIP_INLINE_UNDER:
                    continue
                receipt_files.append(a)
        total = 0
        for a in receipt_files:
            content = await _download(a["download_url"])
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                break
            name = a.get("filename") or f"attachment{os.path.splitext(a.get('content_type') or '')[1]}"
            ext = os.path.splitext(name)[1].lower() or (".pdf" if "pdf" in a.get("content_type", "") else ".jpg")
            row = await save_receipt(
                supabase, business, content=content, original_name=f"email-{email_id[:8]}-{name}", ext=ext,
                mime=a.get("content_type") or "application/octet-stream", submitted_by="email:forwarding",
                notes=f"Forwarded by email from {base_log['from_address']}", check_duplicate_name=False,
            )
            created.append(row)

        if not created:
            # No usable attachment: the email itself is the receipt
            email = await _resend_get(f"emails/receiving/{email_id}")
            html = email.get("html") or ""
            text = email.get("text") or ""
            if html or text:
                pdf = await _html_to_pdf(html) if html else None
                if pdf:
                    content, ext, mime = pdf, ".pdf", "application/pdf"
                else:
                    # Plain text, never raw HTML, so nothing from the email
                    # can run when someone opens the stored file.
                    from routes.receiptvault.routes import _email_text
                    body_text = _email_text(base64.b64encode(html.encode("utf-8")).decode()) if html else text
                    content, ext, mime = body_text.encode("utf-8"), ".txt", "text/plain"
                subject = re.sub(r"[^A-Za-z0-9 _-]+", "", base_log["subject"])[:40].strip() or "email"
                row = await save_receipt(
                    supabase, business, content=content, original_name=f"email-{email_id[:8]}-{subject}{ext}", ext=ext,
                    mime=mime, submitted_by="email:forwarding",
                    notes=f"Forwarded by email from {base_log['from_address']}", check_duplicate_name=False,
                )
                created.append(row)
        supabase.table("rv_inbound_emails").update({"status": "processed" if created else "no_receipt", "receipts_created": len(created)}).eq("id", log["id"]).execute()
    except Exception as e:
        print(f"[inbound] processing {email_id} failed: {e}")
        supabase.table("rv_inbound_emails").update({"status": "failed", "error": str(e)[:500], "receipts_created": len(created)}).eq("id", log["id"]).execute()
    return {"ok": True, "receipts": [to_receipt(r) for r in created]}
