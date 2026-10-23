from fastapi import APIRouter, HTTPException, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import os
import httpx
import base64 as b64lib
import time
import random
import string
import json
import stripe
from datetime import datetime, timedelta

from supabase import create_client, Client

# Reuses the same email-sending helper and Saleh's own owner_email — rather
# than duplicating a second copy of _send_email/_wrap, so there's only one
# place that knows how ReceiptVault sends transactional email.
from routes.receiptvault.trial_emails import _send_email, _wrap, FRONTEND_URL
ADMIN_NOTIFICATION_EMAIL = os.getenv("ADMIN_NOTIFICATION_EMAIL", "saleh852@gmail.com")

# Read independently rather than importing from billing_routes.py, which
# already imports get_current_user FROM this file — importing back from it
# here would create a circular import.
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")


receipt_routes = APIRouter(prefix="/api", tags=["receiptvault"])

_supabase_client = None

from routes.receiptvault.files import sign_receipts  # noqa: E402  (private bucket -> signed links)

# One shared category list with Schedule C lines — see categories.py.
from routes.receiptvault.categories import (  # noqa: E402
    CATEGORY_NAMES as CATEGORIES,
    SCHEDULE_C_LINES,
    CATEGORY_TO_LINE as CATEGORY_TO_SCHEDULE_C_LINE,
    normalize_category,
    line_for,
    prompt_category_list,
    public_categories,
)

def get_supabase() -> Client:
    global _supabase_client
    if _supabase_client is None:
        supabase_url = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
        supabase_key = (
            os.getenv("SUPABASE_ANON_KEY")
            or os.getenv("SUPABASE_KEY")
            or os.getenv("SUPABASE_SERVICE_KEY")
        )
        if not supabase_key:
            raise HTTPException(status_code=500, detail="Supabase key not configured")
        _supabase_client = create_client(supabase_url, supabase_key)
    return _supabase_client


from routes.receiptvault.stores import resolve_store, ALL_MODULES  # noqa: E402


def get_business_for_user(supabase, user_id: str):
    """The active store (X-Business-Id) for an owner OR active team member."""
    business, _role, _modules = resolve_store(supabase, user_id)
    return business


def resolve_role(supabase, user_id: str):
    """Returns (business, role) for the active store. role is 'owner' |
    'manager' | 'bookkeeper' | 'employee'. (None, None) if user has no store."""
    business, role, _modules = resolve_store(supabase, user_id)
    return business, role


def resolve_module_access(supabase, user_id: str, module: str):
    """Returns the active store if this user can access `module` there.
    The owner always has every module. Team members only have what the
    owner explicitly checked off for them on the Team page.
    Raises 403 if they're on the team but don't have this module, 404 if
    they have no business relationship at all."""
    business, role, modules = resolve_store(supabase, user_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    if role != "owner" and module not in (modules or []):
        raise HTTPException(status_code=403, detail="You don't have access to this. Ask the owner to add it on the Team page.")
    return business


def require_owner_store(supabase, user_id: str):
    """The active store, only if this user OWNS it."""
    business, role, _modules = resolve_store(supabase, user_id)
    if not business or role != "owner":
        return None
    return business


# Roles allowed to auto-approve their own submissions
AUTO_APPROVE_ROLES = {"owner", "manager", "bookkeeper"}
# Roles allowed to approve/reject others' submissions
APPROVER_ROLES = {"owner", "manager"}


async def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    token = authorization.replace("Bearer ", "")
    try:
        supabase = get_supabase()
        user = supabase.auth.get_user(token)
        return user
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")


async def require_active_subscription(request: Request, current_user=Depends(get_current_user)):
    """Store gate (replaces the old trial lockout — free accounts never hit
    a wall). Reading is always allowed; changes are blocked only on a store
    that is view-only because the owner's plan doesn't pay for it (e.g. 3
    stores after downgrading to Free) or because it's archived."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    from routes.receiptvault.entitlements import check_store_writable
    supabase = get_supabase()
    business, role = resolve_role(supabase, current_user.user.id)
    if business is None:
        return
    check_store_writable(supabase, business, role)


require_writable_store = require_active_subscription


def plan_feature(feature: str, writes_only: bool = False):
    """Dependency: the active store's owner plan must include `feature`.
    writes_only=True lets people still VIEW old data after a downgrade
    (e.g. invoices) while blocking new changes."""
    async def dep(request: Request, current_user=Depends(get_current_user)):
        if writes_only and request.method in ("GET", "HEAD", "OPTIONS"):
            return
        from routes.receiptvault.entitlements import require_feature
        supabase = get_supabase()
        business, role = resolve_role(supabase, current_user.user.id)
        if business is None:
            return
        require_feature(supabase, business, feature, role)
    return dep


def to_business(row: dict, referral_count: int = 0) -> dict:
    return {
        "id": row.get("id"),
        "userId": row.get("user_id"),
        "businessName": row.get("business_name"),
        "businessAddress": row.get("business_address"),
        "ownerName": row.get("owner_name"),
        "ownerAddress": row.get("owner_address"),
        "state": row.get("state"),
        "accountantEmail": row.get("accountant_email"),
        "sendFrequency": row.get("send_frequency"),
        "sendDay": row.get("send_day"),
        "sendEnabled": row.get("send_enabled", 1),
        "ownerPhone": row.get("owner_phone"),
        "taxId": row.get("tax_id"),
        "weekStartDay": row.get("week_start_day", 0),
        "createdAt": str(row.get("created_at", "")),
        "referralCode": row.get("referral_code"),
        "referralCount": referral_count,
        "zelleContact": row.get("zelle_contact"),
        "cashappTag": row.get("cashapp_tag"),
        "paypalLink": row.get("paypal_link"),
        "chimeTag": row.get("chime_tag"),
    }


def to_receipt(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "businessId": row.get("business_id"),
        "filePath": row.get("file_path"),
        "originalName": row.get("original_name"),
        "merchant": row.get("merchant"),
        "amount": row.get("amount"),
        "receiptDate": row.get("receipt_date"),
        "category": row.get("category"),
        "scheduleCLine": row.get("schedule_c_line"),
        "notes": row.get("notes"),
        "uploadedAt": str(row.get("uploaded_at", "")),
        "sentAt": str(row.get("sent_at")) if row.get("sent_at") else None,
    }


MAX_RECEIPT_DATE_DRIFT_DAYS = 366  # ~1 year, covers leap years


def _sanity_check_receipt_date(receipt_date: Optional[str], reference_date: datetime) -> Optional[str]:
    """Reject an OCR-extracted date that's implausibly far (either direction)
    from the upload date, rather than writing it straight to the database.
    A receipt misread as 2005 should never make it into a 2026 report."""
    if not receipt_date:
        return None
    try:
        parsed = datetime.strptime(str(receipt_date)[:10], "%Y-%m-%d")
    except ValueError:
        print(f"[extract] rejecting unparsable OCR receipt_date: {receipt_date!r}")
        return None
    drift_days = abs((reference_date - parsed).days)
    if drift_days > MAX_RECEIPT_DATE_DRIFT_DAYS:
        print(f"[extract] rejecting OCR receipt_date {receipt_date!r} — {drift_days} days from upload date, exceeds sanity threshold")
        return None
    return receipt_date


def _email_text(b64: str) -> str:
    """Plain text from an emailed receipt's HTML (tags, styles and scripts
    stripped) for the AI to read."""
    import re as _re
    import html as _html
    raw = b64lib.b64decode(b64).decode("utf-8", errors="replace")
    raw = _re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", raw)
    raw = _re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", raw)
    raw = _re.sub(r"<[^>]+>", " ", raw)
    raw = _html.unescape(raw)
    raw = _re.sub(r"\n\s*\n+", "\n", raw)
    return _re.sub(r"[ \t\r\f\v]+", " ", raw).strip()


async def extract_receipt_data(image_base64: str, mime_type: str, reference_date: Optional[datetime] = None) -> dict:
    """Use Claude Vision to extract merchant, amount, date, category from receipt image.
    reference_date is the upload date to sanity-check the extracted date against —
    defaults to now, but callers re-running OCR on an existing receipt should pass
    the receipt's original uploaded_at instead."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("[extract] ANTHROPIC_API_KEY not set on Render — skipping OCR, fields will be blank")
        return {}

    is_pdf = "pdf" in (mime_type or "").lower()
    # Emailed receipts with no attachment arrive as the email's text/HTML
    is_text = (mime_type or "").lower().startswith("text/")

    # Normalize mime — in-app camera uploads send "image/jpg", which the API
    # rejects (it only accepts image/jpeg, png, gif, webp)
    mime_type = (mime_type or "").lower().strip()
    if is_pdf:
        mime_type = "application/pdf"
    elif is_text:
        pass
    elif mime_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        mime_type = "image/png" if "png" in mime_type else "image/jpeg"

    # Downscale large camera captures — the API rejects images over 5MB,
    # and phone cameras routinely exceed that. Resizing also speeds up OCR.
    try:
        raw = b64lib.b64decode(image_base64)
        if not is_pdf and not is_text and len(raw) > 1_500_000:  # anything over ~1.5MB gets resized
            from io import BytesIO
            from PIL import Image, ImageOps

            img = Image.open(BytesIO(raw))
            img = ImageOps.exif_transpose(img)  # respect phone rotation
            img.thumbnail((2000, 2000))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=85)
            image_base64 = b64lib.b64encode(buf.getvalue()).decode("utf-8")
            mime_type = "image/jpeg"
            print(f"[extract] resized image {len(raw)} -> {buf.tell()} bytes")
    except Exception as resize_err:
        print(f"[extract] resize failed ({resize_err}) — sending original")

    prompt = f"""You are an expert receipt and invoice parser for a convenience store owner.

Extract the following from this receipt/invoice image and return ONLY valid JSON:
{{
  "merchant": "name of the company SELLING the goods (the vendor/supplier, NOT the buyer)",
  "amount": 12.99,
  "receipt_date": "YYYY-MM-DD",
  "category": "one of the category names listed below"
}}

CATEGORIES (pick exactly one name):
{prompt_category_list()}

CRITICAL RULES:
- "merchant" = the SELLER/VENDOR/SUPPLIER name at the TOP of the invoice (e.g. PepsiCo, Heffron Distributing, Restaurant Depot, Walmart, Husted Dairy)
- NEVER use the buyer/customer name (e.g. Shamrock Market, Fat Boys) as the merchant
- On distributor invoices: "Send payment to" or the company logo at top = the merchant
- amount = the final TOTAL DUE or TOTAL amount (a number, no $ sign)
- receipt_date = YYYY-MM-DD format, or null
- category must be exactly one of the names above
- For beverage distributors, food suppliers, dairy, restaurant supply stores: use "Inventory"
- For bags, cups, cleaning and store supplies: use "Supplies"
- For gas/fuel: use "Vehicle & Fuel"
- If you cannot find a value, use null
- Return ONLY the JSON object, no explanation, no markdown"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 256,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "RECEIPT / INVOICE EMAIL TEXT:\n" + _email_text(image_base64)[:30000]}
                                if is_text else
                                {
                                    "type": "document" if is_pdf else "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": mime_type,
                                        "data": image_base64,
                                    },
                                },
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                },
            )
        if res.status_code != 200:
            print(f"[extract] Anthropic API error {res.status_code}: {res.text[:300]}")
            return {}
        data = res.json()
        text = data["content"][0]["text"].strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text.strip())
        # The Schedule C line always follows from the one shared category list
        parsed["category"] = normalize_category(parsed.get("category"))
        parsed["schedule_c_line"] = line_for(parsed.get("category"))
        parsed["receipt_date"] = _sanity_check_receipt_date(parsed.get("receipt_date"), reference_date or datetime.utcnow())
        return parsed
    except Exception as ocr_err:
        print(f"OCR error: {ocr_err}")
        return {}


@receipt_routes.get("/categories")
async def list_categories():
    """The one shared category list, each with its Schedule C line."""
    return public_categories()


@receipt_routes.get("/user")
async def get_user(current_user=Depends(get_current_user)):
    return {
        "id": current_user.user.id,
        "email": current_user.user.email,
        "user_metadata": current_user.user.user_metadata,
        "created_at": str(current_user.user.created_at),
    }


def business_payload(supabase, business: dict, role: str, modules: list, user_id: str) -> dict:
    """/api/business shape for one store, shared by /business and /stores."""
    from routes.receiptvault.stores import owned_stores
    referral_count = 0
    if role == "owner":
        referral_rows = supabase.table("referrals").select("id").eq("referrer_business_id", business["id"]).execute()
        referral_count = len(referral_rows.data or [])
    out = to_business(business, referral_count)
    out["role"] = role
    out["modules"] = list(ALL_MODULES) if role == "owner" else (modules or [])
    if role == "owner":
        out["storeCount"] = len(owned_stores(supabase, user_id))
        if not business.get("accountant_email") or not business.get("send_frequency") or not business.get("send_day"):
            out["setupRequired"] = "accountant"
    else:
        out["isTeamMember"] = True
    return out


@receipt_routes.get("/business")
async def get_business(current_user=Depends(get_current_user)):
    """The active store (X-Business-Id header, else the user's first store)."""
    supabase = get_supabase()
    user_id = current_user.user.id

    business, role, modules = resolve_store(supabase, user_id)
    if business:
        return business_payload(supabase, business, role, modules, user_id)

    # Auto-link: activate any pending invites sent to this user's email
    user_email = current_user.user.email
    if user_email:
        pending = (
            supabase.table("business_users").select("*")
            .eq("email", user_email).eq("status", "invited")
            .execute()
        )
        for invite in (pending.data or []):
            supabase.table("business_users").update({
                "user_id": user_id,
                "status": "active",
                "joined_at": datetime.utcnow().isoformat(),
            }).eq("id", invite["id"]).execute()
        if pending.data:
            biz = supabase.table("businesses").select("*").eq("id", pending.data[0]["business_id"]).execute()
            if biz.data:
                invite = pending.data[0]
                return business_payload(supabase, biz.data[0], invite.get("role") or "employee", invite.get("modules") or [], user_id)

    return {"setupRequired": "business"}


class BusinessProfileBody(BaseModel):
    businessName: str
    businessAddress: str
    ownerName: str
    ownerAddress: Optional[str] = None
    taxId: Optional[str] = None
    state: Optional[str] = None
    weekStartDay: Optional[int] = None
    referralCode: Optional[str] = None
    zelleContact: Optional[str] = None
    cashappTag: Optional[str] = None
    paypalLink: Optional[str] = None
    chimeTag: Optional[str] = None


def generate_referral_code(supabase) -> str:
    """8-char uppercase alphanumeric code, excluding ambiguous characters
    (0/O, 1/I/L) so people can read them back over text/phone without
    confusion. Retries on the rare collision."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    for _ in range(10):
        code = "".join(random.choices(alphabet, k=8))
        existing = supabase.table("businesses").select("id").eq("referral_code", code).execute()
        if not existing.data:
            return code
    # Astronomically unlikely, but fail loudly rather than silently reuse a code
    raise HTTPException(status_code=500, detail="Could not generate a unique referral code, please try again")


async def apply_referral_reward(supabase, referrer: dict):
    """Rewards the REFERRER (not the new signup) with 1 month free for each
    friend who signs up with their code. Paying owners get a one-time
    100%-off coupon on their subscription now; Free owners get it saved as
    'pending_coupon' and applied automatically when they subscribe.
    Requires the coupon "referral-1-month-free" (100% off, duration=once)
    to exist in Stripe."""
    from routes.receiptvault.entitlements import get_account
    account = get_account(supabase, str(referrer["user_id"]))
    sub_id = account.get("stripe_subscription_id")
    if account.get("plan") != "free" and sub_id:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.stripe.com/v1/subscriptions/{sub_id}",
                headers={
                    "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"discounts[0][coupon]": os.getenv("STRIPE_REFERRAL_COUPON", "referral-1-month-free")},
            )
            if r.status_code < 400:
                return "stripe_coupon"
            # Don't block the referred signup over a Stripe hiccup
            print(f"[referral] Failed to apply Stripe coupon for business {referrer['id']}: {r.text}")
    return "pending_coupon"


@receipt_routes.post("/setup/business")
async def setup_business(body: BusinessProfileBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    user_id = current_user.user.id

    # Editing the active store if this user owns it; a brand-new owner
    # (no stores, no memberships) creates their first store below.
    # Additional stores go through POST /api/stores instead.
    active_store, active_role, _m = resolve_store(supabase, user_id)
    owned = type("R", (), {"data": [active_store] if active_store and active_role == "owner" else []})()

    if body.weekStartDay is not None and not (0 <= body.weekStartDay <= 6):
        raise HTTPException(status_code=400, detail="weekStartDay must be 0-6 (0=Sunday)")

    if owned.data:
        # Owner editing their own existing business. NEVER include user_id in
        # this update — it previously did, which meant any call here silently
        # reassigned ownership of the business to whoever called it.
        data = {
            "business_name": body.businessName,
            "business_address": body.businessAddress,
            "owner_name": body.ownerName,
            "owner_address": body.ownerAddress,
        }
        if body.taxId is not None:
            data["tax_id"] = body.taxId
        if body.state is not None:
            data["state"] = body.state.strip().upper()[:2] or None
        if body.weekStartDay is not None:
            data["week_start_day"] = body.weekStartDay
        if body.zelleContact is not None:
            data["zelle_contact"] = body.zelleContact or None
        if body.cashappTag is not None:
            data["cashapp_tag"] = body.cashappTag or None
        if body.paypalLink is not None:
            data["paypal_link"] = body.paypalLink or None
        if body.chimeTag is not None:
            data["chime_tag"] = body.chimeTag or None
        result = supabase.table("businesses").update(data).eq("id", owned.data[0]["id"]).execute()
    else:
        # Not an owner. If they're already a team member somewhere, they don't
        # get to create/edit a business profile — only the owner can.
        if active_store is not None:
            raise HTTPException(status_code=403, detail="Only the business owner can edit the business profile.")
        # Brand new user with no business and no team membership — normal
        # first-time signup flow. They become the owner of a new business
        # on the Free plan (no trial: Free never expires).
        data = {
            "user_id": user_id,
            "business_name": body.businessName,
            "business_address": body.businessAddress,
            "owner_name": body.ownerName,
            "owner_address": body.ownerAddress,
            "owner_email": current_user.user.email,
            "referral_code": generate_referral_code(supabase),
        }
        if body.taxId is not None:
            data["tax_id"] = body.taxId
        if body.state is not None:
            data["state"] = body.state.strip().upper()[:2] or None
        if body.weekStartDay is not None:
            data["week_start_day"] = body.weekStartDay
        if body.zelleContact is not None:
            data["zelle_contact"] = body.zelleContact or None
        if body.cashappTag is not None:
            data["cashapp_tag"] = body.cashappTag or None
        if body.paypalLink is not None:
            data["paypal_link"] = body.paypalLink or None
        if body.chimeTag is not None:
            data["chime_tag"] = body.chimeTag or None

        # If they signed up via someone's referral link, validate the code
        # and reward the referrer — never the new signup. Silently ignore
        # invalid/unknown codes rather than blocking signup over it.
        referrer = None
        if body.referralCode:
            referrer_lookup = (
                supabase.table("businesses").select("*").eq("referral_code", body.referralCode.strip().upper()).execute()
            )
            if referrer_lookup.data:
                referrer = referrer_lookup.data[0]
                data["referred_by_code"] = body.referralCode.strip().upper()

        result = supabase.table("businesses").insert(data).execute()
        from routes.receiptvault.entitlements import get_account
        get_account(supabase, user_id)  # owner's billing account, on Free

        # Notify Saleh directly whenever a new business signs up — without
        # this, the only way to know is checking the database by hand,
        # which is how the Hamooda trial went unnoticed until it was too
        # late to bring him back. Fire-and-forget: a notification failure
        # should never block the signup itself from completing.
        if result.data:
            try:
                await _send_email(
                    to=ADMIN_NOTIFICATION_EMAIL,
                    subject=f"New ReceiptKept signup: {body.businessName}",
                    html=_wrap(f"""
                    <p><b>{body.businessName}</b> just signed up.</p>
                    <p>Owner: {body.ownerName}<br>
                    Email: {current_user.user.email}<br>
                    Plan: Free</p>
                    """),
                )
            except Exception as e:
                print(f"[signup] admin notification failed: {e}")

            # Welcome email to the customer themselves. Kept deliberately simple: no jargon, one thing to do next
            # (scan a receipt), matching the same zero-effort framing as the
            # rest of the app. Same fire-and-forget rule: never block signup.
            try:
                await _send_email(
                    to=current_user.user.email,
                    subject="Welcome to ReceiptKept",
                    html=_wrap(f"""
                    <p>Hi {body.ownerName or 'there'},</p>
                    <p><b>{body.businessName}</b> is set up on the Free plan &mdash; 10 receipt scans a month, free forever.</p>
                    <p>Take a photo of a receipt and ReceiptKept handles the rest &mdash; no typing, no
                    spreadsheets, no learning curve.</p>
                    <a href="{FRONTEND_URL}" style="display:inline-block;background:#1a6b3a;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;margin-top:8px">Scan Your First Receipt</a>
                    """),
                )
            except Exception as e:
                print(f"[signup] welcome email failed: {e}")

        if result.data and referrer:
            reward_type = await apply_referral_reward(supabase, referrer)
            if reward_type:
                supabase.table("referrals").insert({
                    "referrer_business_id": referrer["id"],
                    "referred_business_id": result.data[0]["id"],
                    "referral_code": data["referred_by_code"],
                    "reward_type": reward_type,
                }).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to save business")
    return to_business(result.data[0])


class AccountantConfigBody(BaseModel):
    accountantEmail: str
    sendFrequency: str
    sendDay: int
    sendEnabled: Optional[int] = 1
    ownerPhone: Optional[str] = None


@receipt_routes.post("/setup/accountant")
async def setup_accountant(body: AccountantConfigBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    store = require_owner_store(supabase, current_user.user.id)
    if not store:
        raise HTTPException(status_code=403, detail="Only the business owner can edit accountant settings.")
    business_id = store["id"]
    data = {
        "accountant_email": body.accountantEmail,
        "send_frequency": body.sendFrequency,
        "send_day": body.sendDay,
        "send_enabled": body.sendEnabled or 1,
    }
    if body.ownerPhone:
        data["owner_phone"] = body.ownerPhone
    result = supabase.table("businesses").update(data).eq("id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to update accountant config")
    return to_business(result.data[0])


class PaymentOptionsBody(BaseModel):
    zelle: Optional[str] = None
    cashApp: Optional[str] = None
    paypal: Optional[str] = None
    chime: Optional[str] = None


@receipt_routes.post("/settings/payment-options")
async def save_payment_options(body: PaymentOptionsBody, current_user=Depends(get_current_user)):
    """Matches the frontend's POST /api/settings/payment-options exactly —
    that route didn't exist here before (only /api/setup/business accepted
    these fields), which is why saving from the Settings page 404'd."""
    supabase = get_supabase()
    store = require_owner_store(supabase, current_user.user.id)
    if not store:
        raise HTTPException(status_code=404, detail="Business not found")
    business_id = store["id"]
    data = {
        "zelle_contact": body.zelle or None,
        "cashapp_tag": body.cashApp or None,
        "paypal_link": body.paypal or None,
        "chime_tag": body.chime or None,
    }
    result = supabase.table("businesses").update(data).eq("id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to save payment options")
    return to_business(result.data[0])


@receipt_routes.get("/receipts", dependencies=[Depends(require_active_subscription)])
async def list_receipts(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business = resolve_module_access(supabase, current_user.user.id, "receipts")
    result = supabase.table("receipts").select("*").eq("business_id", business["id"]).order("uploaded_at", desc=True).execute()
    return sign_receipts(supabase, [to_receipt(r) for r in result.data])


@receipt_routes.get("/receipts/{receipt_id}", dependencies=[Depends(require_active_subscription)])
async def get_receipt(receipt_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business = get_business_for_user(supabase, current_user.user.id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    result = (
        supabase.table("receipts")
        .select("*")
        .eq("id", receipt_id)
        .eq("business_id", business["id"])
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return sign_receipts(supabase, [to_receipt(result.data[0])])[0]


@receipt_routes.post("/receipts/{receipt_id}/extract", dependencies=[Depends(require_active_subscription)])
async def reextract_receipt(receipt_id: str, current_user=Depends(get_current_user)):
    """Re-run Claude OCR on an existing receipt and fill any empty fields.
    Use this to backfill receipts uploaded while extraction was failing."""
    supabase = get_supabase()
    business = get_business_for_user(supabase, current_user.user.id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    result = (
        supabase.table("receipts")
        .select("*")
        .eq("id", receipt_id)
        .eq("business_id", business["id"])
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    row = result.data[0]

    # Download the file from storage using the object path from file_path
    file_path = row.get("file_path", "")
    marker = "/receipts/"
    if marker not in file_path:
        raise HTTPException(status_code=400, detail="Cannot resolve storage path")
    object_path = file_path.split(marker, 1)[1]
    try:
        content = supabase.storage.from_("receipts").download(object_path)
    except Exception as dl_err:
        raise HTTPException(status_code=500, detail=f"Could not download file: {dl_err}")

    ext = os.path.splitext(object_path)[1].lower()
    mime = "application/pdf" if ext == ".pdf" else f"image/{(ext[1:] or 'jpeg').replace('jpg', 'jpeg')}"
    upload_reference_date = datetime.utcnow()
    uploaded_at = row.get("uploaded_at")
    if uploaded_at:
        try:
            upload_reference_date = datetime.fromisoformat(str(uploaded_at).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    ocr_data = await extract_receipt_data(b64lib.b64encode(content).decode("utf-8"), mime, reference_date=upload_reference_date)
    if not ocr_data:
        raise HTTPException(status_code=502, detail="Extraction returned nothing — check Render logs for [extract] errors")

    update_data = {}
    if not row.get("merchant") and ocr_data.get("merchant"):
        update_data["merchant"] = ocr_data["merchant"]
    if row.get("amount") is None and ocr_data.get("amount") is not None:
        update_data["amount"] = float(ocr_data["amount"])
    if not row.get("receipt_date") and ocr_data.get("receipt_date"):
        update_data["receipt_date"] = ocr_data["receipt_date"]
    if not row.get("category") and ocr_data.get("category"):
        update_data["category"] = ocr_data["category"]
    if not row.get("schedule_c_line") and ocr_data.get("schedule_c_line"):
        update_data["schedule_c_line"] = ocr_data["schedule_c_line"]

    if update_data:
        updated = supabase.table("receipts").update(update_data).eq("id", receipt_id).execute()
        return sign_receipts(supabase, [to_receipt(updated.data[0])])[0]
    return sign_receipts(supabase, [to_receipt(row)])[0]


class UpdateReceiptBody(BaseModel):
    merchant: Optional[str] = None
    amount: Optional[float] = None
    receiptDate: Optional[str] = None
    category: Optional[str] = None
    scheduleCLine: Optional[str] = None
    notes: Optional[str] = None


@receipt_routes.patch("/receipts/{receipt_id}", dependencies=[Depends(require_active_subscription)])
async def update_receipt(receipt_id: str, body: UpdateReceiptBody, current_user=Depends(get_current_user)):
    """Allow users to manually correct OCR-extracted fields."""
    supabase = get_supabase()
    business = resolve_module_access(supabase, current_user.user.id, "receipts")
    biz = type("R", (), {"data": [{"id": business["id"]}]})()
    
    update_data = {}
    if body.merchant is not None:
        update_data["merchant"] = body.merchant
    if body.amount is not None:
        update_data["amount"] = body.amount
    if body.receiptDate is not None:
        update_data["receipt_date"] = body.receiptDate
    if body.category is not None:
        update_data["category"] = normalize_category(body.category)
        update_data["schedule_c_line"] = line_for(update_data["category"])
    if body.scheduleCLine is not None:
        if body.scheduleCLine not in SCHEDULE_C_LINES:
            raise HTTPException(status_code=400, detail=f"scheduleCLine must be one of: {', '.join(SCHEDULE_C_LINES)}")
        update_data["schedule_c_line"] = body.scheduleCLine
    if body.notes is not None:
        update_data["notes"] = body.notes

    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = supabase.table("receipts").update(update_data).eq("id", receipt_id).eq("business_id", biz.data[0]["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return sign_receipts(supabase, [to_receipt(result.data[0])])[0]


@receipt_routes.post("/receipts", status_code=201, dependencies=[Depends(require_active_subscription)])
async def upload_receipt(request: Request, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business = resolve_module_access(supabase, current_user.user.id, "receipts")
    business_id = business["id"]
    from routes.receiptvault.entitlements import check_scan_allowed
    _b, uploader_role_for_limit = resolve_role(supabase, current_user.user.id)
    check_scan_allowed(supabase, business, uploader_role_for_limit)

    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        body = await request.json()
        image_base64 = body.get("imageBase64")
        filename = body.get("filename")
        if not image_base64 or not filename:
            raise HTTPException(status_code=400, detail="imageBase64 and filename are required")
        content = b64lib.b64decode(image_base64)
        ext = os.path.splitext(filename)[1].lower() or ".jpg"
        mime = "application/pdf" if ext == ".pdf" else f"image/{ext[1:]}"
        original_name = filename
        ocr_base64 = image_base64
    elif "multipart/form-data" in content_type:
        form = await request.form()
        file_obj = form.get("file")
        if not file_obj:
            raise HTTPException(status_code=400, detail="file is required")
        content = await file_obj.read()
        ext = os.path.splitext(file_obj.filename)[1].lower() or ".jpg"
        mime = file_obj.content_type or f"image/{ext[1:]}"
        original_name = file_obj.filename
        ocr_base64 = b64lib.b64encode(content).decode("utf-8")
    else:
        raise HTTPException(status_code=400, detail="Unsupported content type")

    # Owner/manager/bookkeeper uploads auto-approve; employee uploads need approval
    _, uploader_role = resolve_role(supabase, current_user.user.id)
    row = await save_receipt(
        supabase, business, content=content, original_name=original_name, ext=ext, mime=mime,
        submitted_by=current_user.user.id,
        approval_status="approved" if uploader_role in AUTO_APPROVE_ROLES else "pending",
        ocr_base64=ocr_base64,
    )
    return JSONResponse(status_code=201, content=sign_receipts(supabase, [to_receipt(row)])[0])


async def save_receipt(supabase, business: dict, *, content: bytes, original_name: str, ext: str, mime: str,
                       submitted_by: str, approval_status: str = "approved", ocr_base64: Optional[str] = None,
                       notes: Optional[str] = None, check_duplicate_name: bool = True) -> dict:
    """Store the file, read it with AI, save the receipt row, then try to
    match it to a bank charge. Raises 409 on a duplicate file name."""
    business_id = business["id"]
    supabase_url = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")

    # ── Duplicate check (before spending storage + an AI call) ──────────
    if check_duplicate_name:
        existing = (
            supabase.table("receipts")
            .select("id, original_name")
            .eq("business_id", business_id)
            .eq("original_name", original_name)
            .execute()
        )
        if existing.data:
            raise HTTPException(
                status_code=409,
                detail=f"A receipt named '{original_name}' already exists. Delete the old one first or rename the file."
            )

    # Upload to Supabase Storage
    unique = f"{int(time.time() * 1000)}-{random.randint(0, 999999999)}{ext}"
    supabase.storage.from_("receipts").upload(unique, content, {"content-type": mime})
    file_url = f"{supabase_url}/storage/v1/object/public/receipts/{unique}"

    # Read it with AI (if that fails, the receipt still saves)
    ocr_data = await extract_receipt_data(ocr_base64 or b64lib.b64encode(content).decode("utf-8"), mime)

    insert_payload = {
        "business_id": business_id,
        "file_path": file_url,
        "original_name": original_name,
        "submitted_by": submitted_by,
        "approval_status": approval_status,
    }
    if notes:
        insert_payload["notes"] = notes
    if ocr_data.get("merchant"):
        insert_payload["merchant"] = ocr_data["merchant"]
    if ocr_data.get("amount") is not None:
        try:
            insert_payload["amount"] = float(ocr_data["amount"])
        except (TypeError, ValueError):
            pass
    if ocr_data.get("receipt_date"):
        insert_payload["receipt_date"] = ocr_data["receipt_date"]
    if ocr_data.get("category"):
        insert_payload["category"] = ocr_data["category"]
    if ocr_data.get("schedule_c_line"):
        insert_payload["schedule_c_line"] = ocr_data["schedule_c_line"]

    receipt = supabase.table("receipts").insert(insert_payload).execute()
    if not receipt.data:
        raise HTTPException(status_code=500, detail="Failed to create receipt record")
    row = receipt.data[0]

    # Match the new receipt to a bank charge (Basic and up)
    try:
        from routes.receiptvault.entitlements import account_for_business, has_feature
        if has_feature(account_for_business(supabase, business), "auto_categorize"):
            from routes.receiptvault.bank_core import match_receipt
            match_receipt(supabase, business_id, row)
    except Exception as e:
        print(f"[receipts] matching after upload failed: {e}")
    return row


@receipt_routes.delete("/receipts/{receipt_id}", dependencies=[Depends(require_active_subscription)])
async def delete_receipt(receipt_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business = resolve_module_access(supabase, current_user.user.id, "receipts")
    business_id = business["id"]
    result = supabase.table("receipts").delete().eq("id", receipt_id).eq("business_id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return {"message": "Receipt deleted"}



@receipt_routes.post("/resend-all", dependencies=[Depends(require_active_subscription), Depends(plan_feature("accountant"))])
async def resend_all(current_user=Depends(get_current_user)):
    """Mark all receipts as unsent then send them again."""
    supabase = get_supabase()
    _, _role = resolve_role(supabase, current_user.user.id)
    if _role not in ("owner", "bookkeeper"):
        raise HTTPException(status_code=403, detail="Only the owner or bookkeeper can send to the accountant")
    biz_data = get_business_for_user(supabase, current_user.user.id)
    biz = type("R", (), {"data": [biz_data] if biz_data else []})()
    if not biz.data:
        raise HTTPException(status_code=400, detail="Business not found")
    business = biz.data[0]
    if not business.get("accountant_email"):
        raise HTTPException(status_code=400, detail="No accountant email configured")

    # Clear sent_at AND any stale error on ALL receipts (this is a resend)
    supabase.table("receipts").update({"sent_at": None, "send_error": None}).eq("business_id", business["id"]).execute()

    all_receipts = (
        supabase.table("receipts")
        .select("*")
        .eq("business_id", business["id"])
        .execute()
    )
    if not all_receipts.data:
        return {"ok": True, "sent": 0}

    receipts = [to_receipt(r) for r in all_receipts.data]
    sent_count = len(receipts)
    receipt_ids = [r["id"] for r in all_receipts.data]

    async def send_in_background():
        from datetime import datetime, timezone
        try:
            from pdf_generator import generate_cover_pdf
            from excel_generator import build_excel
            from routes.receiptvault.money_routes import sum_ledger_income
            from routes.receiptvault.dropbox_routes import create_dropbox_token
            period_label = datetime.now().strftime("%B %Y") + " Expenses"
            biz_name = business.get("business_name", "")
            safe_name = biz_name.replace(" ", "_")
            month_str = datetime.now().strftime("%Y-%m")
            month_start = datetime.now().replace(day=1).strftime("%Y-%m-%d")
            today_str = datetime.now().strftime("%Y-%m-%d")
            gross_income = sum_ledger_income(supabase, business["id"], month_start, today_str)

            dropbox_url = None
            try:
                dropbox_token = create_dropbox_token(supabase, business["id"], created_by=current_user.user.id, label=f"Resend — {period_label}")
                dropbox_url = dropbox_token["url"]
            except Exception as dropbox_err:
                print(f"[resend-all] dropbox token creation failed — sending without one: {dropbox_err}")

            # Generate PDF cover sheet
            pdf_bytes = await generate_cover_pdf(
                business_name=biz_name,
                owner_name=business.get("owner_name", ""),
                accountant_email=business.get("accountant_email", ""),
                receipts=receipts,
                period_label=period_label,
                bank_transactions=_bank_charges_for_report(supabase, business["id"]),
            )
            pdf_base64 = b64lib.b64encode(pdf_bytes).decode("utf-8")
            pdf_filename = f"ReceiptVault_{safe_name}_{month_str}_RESEND.pdf"

            # Generate Excel workbook
            excel_bytes = build_excel(
                business_name=biz_name,
                period_label=period_label,
                receipts=receipts,
                gross_income=gross_income,
            )
            excel_base64 = b64lib.b64encode(excel_bytes).decode("utf-8")
            excel_filename = f"ReceiptVault_{safe_name}_{month_str}_RESEND.xlsx"

            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    "https://wzcuzyouymauokijaqjk.supabase.co/functions/v1/send-receipts",
                    headers={"X-Relay-Secret": os.getenv("EMAIL_RELAY_SECRET", "")},
                    json={
                        "businessId": business["id"],
                        "accountantEmail": business["accountant_email"],
                        "businessName": biz_name,
                        "receipts": receipts,
                        "pdfBase64": pdf_base64,
                        "pdfFilename": pdf_filename,
                        "excelBase64": excel_base64,
                        "excelFilename": excel_filename,
                        "dropboxUrl": dropbox_url,
                    },
                )

            if resp.status_code == 200:
                sent_at = datetime.now(timezone.utc).isoformat()
                for rid in receipt_ids:
                    supabase.table("receipts").update({"sent_at": sent_at, "send_error": None}).eq("id", rid).execute()
            else:
                err_msg = f"Email failed ({resp.status_code}): {resp.text[:500]}"
                print(f"Resend-all send failed: {err_msg}")
                for rid in receipt_ids:
                    supabase.table("receipts").update({"send_error": err_msg}).eq("id", rid).execute()
        except Exception as e:
            err_msg = f"Send error: {e}"
            print(f"Background resend error: {err_msg}")
            for rid in receipt_ids:
                supabase.table("receipts").update({"send_error": err_msg}).eq("id", rid).execute()

    import asyncio
    asyncio.create_task(send_in_background())
    return {"ok": True, "sent": sent_count, "status": "sending"}

@receipt_routes.post("/send-now", dependencies=[Depends(require_active_subscription), Depends(plan_feature("accountant"))])
async def send_now(current_user=Depends(get_current_user)):
    import asyncio
    supabase = get_supabase()
    _, _role = resolve_role(supabase, current_user.user.id)
    if _role not in ("owner", "bookkeeper"):
        raise HTTPException(status_code=403, detail="Only the owner or bookkeeper can send to the accountant")
    biz_data = get_business_for_user(supabase, current_user.user.id)
    biz = type("R", (), {"data": [biz_data] if biz_data else []})()
    if not biz.data:
        raise HTTPException(status_code=400, detail="Business not found")
    business = biz.data[0]
    if not business.get("accountant_email"):
        raise HTTPException(status_code=400, detail="No accountant email configured")

    unsent = (
        supabase.table("receipts")
        .select("*")
        .eq("business_id", business["id"])
        .filter("sent_at", "is", "null")
        .execute()
    )
    if not unsent.data:
        return {"ok": True, "sent": 0}

    receipts = [to_receipt(r) for r in unsent.data]
    sent_count = len(receipts)
    receipt_ids = [r["id"] for r in unsent.data]

    # Clear any stale error before retrying
    for rid in receipt_ids:
        supabase.table("receipts").update({"send_error": None}).eq("id", rid).execute()

    # Fire PDF generation + email in background — don't wait for it
    async def send_in_background():
        from datetime import datetime, timezone
        try:
            from pdf_generator import generate_cover_pdf
            from excel_generator import build_excel
            from routes.receiptvault.money_routes import sum_ledger_income
            from routes.receiptvault.dropbox_routes import create_dropbox_token
            period_label = datetime.now().strftime("%B %Y") + " Expenses"
            biz_name = business.get("business_name", "")
            safe_name = biz_name.replace(" ", "_")
            month_str = datetime.now().strftime("%Y-%m")
            month_start = datetime.now().replace(day=1).strftime("%Y-%m-%d")
            today_str = datetime.now().strftime("%Y-%m-%d")
            gross_income = sum_ledger_income(supabase, business["id"], month_start, today_str)

            dropbox_url = None
            try:
                dropbox_token = create_dropbox_token(supabase, business["id"], created_by=current_user.user.id, label=f"Send now — {period_label}")
                dropbox_url = dropbox_token["url"]
            except Exception as dropbox_err:
                print(f"[send-now] dropbox token creation failed — sending without one: {dropbox_err}")

            # Generate PDF cover sheet
            pdf_bytes = await generate_cover_pdf(
                business_name=biz_name,
                owner_name=business.get("owner_name", ""),
                accountant_email=business.get("accountant_email", ""),
                receipts=receipts,
                period_label=period_label,
                bank_transactions=_bank_charges_for_report(supabase, business["id"]),
            )
            pdf_base64 = b64lib.b64encode(pdf_bytes).decode("utf-8")
            pdf_filename = f"ReceiptVault_{safe_name}_{month_str}.pdf"

            # Generate Excel workbook
            excel_bytes = build_excel(
                business_name=biz_name,
                period_label=period_label,
                receipts=receipts,
                gross_income=gross_income,
            )
            excel_base64 = b64lib.b64encode(excel_bytes).decode("utf-8")
            excel_filename = f"ReceiptVault_{safe_name}_{month_str}.xlsx"

            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    "https://wzcuzyouymauokijaqjk.supabase.co/functions/v1/send-receipts",
                    headers={"X-Relay-Secret": os.getenv("EMAIL_RELAY_SECRET", "")},
                    json={
                        "businessId": business["id"],
                        "accountantEmail": business["accountant_email"],
                        "businessName": biz_name,
                        "receipts": receipts,
                        "pdfBase64": pdf_base64,
                        "pdfFilename": pdf_filename,
                        "excelBase64": excel_base64,
                        "excelFilename": excel_filename,
                        "dropboxUrl": dropbox_url,
                    },
                )

            if resp.status_code != 200:
                err_msg = f"Email failed ({resp.status_code}): {resp.text[:500]}"
                print(f"send_now send failed: {err_msg}")
                for rid in receipt_ids:
                    supabase.table("receipts").update({"send_error": err_msg}).eq("id", rid).execute()
                return  # don't mark sent, don't SMS

            sent_at = datetime.now(timezone.utc).isoformat()
            for rid in receipt_ids:
                supabase.table("receipts").update({"sent_at": sent_at, "send_error": None}).eq("id", rid).execute()

            # SMS notify owner
            owner_phone = business.get("owner_phone", "")
            telnyx_key = os.getenv("TELNYX_API_KEY", "")
            if owner_phone and telnyx_key:
                total = sum(float(r.get("amount") or 0) for r in receipts)
                sms_body = (
                    f"ReceiptVault: {len(receipts)} receipt(s) sent to "
                    f"{business['accountant_email']}. "
                    f"Total: ${total:.2f}"
                )
                async with httpx.AsyncClient(timeout=15.0) as sms_client:
                    await sms_client.post(
                        "https://api.telnyx.com/v2/messages",
                        headers={"Authorization": f"Bearer {telnyx_key}"},
                        json={"from": "+13156252025", "to": owner_phone, "text": sms_body},
                    )
        except Exception as e:
            err_msg = f"Send error: {e}"
            print(f"Background send error: {err_msg}")
            for rid in receipt_ids:
                supabase.table("receipts").update({"send_error": err_msg}).eq("id", rid).execute()

    asyncio.create_task(send_in_background())

    # Return immediately — email sends in background
    return {"ok": True, "sent": sent_count, "status": "sending"}


def _bank_charges_for_report(supabase, business_id: str) -> list:
    """This month's business bank charges for the accountant PDF (personal
    and ignored ones left out). Never blocks a send."""
    try:
        from datetime import date as _date
        from routes.receiptvault.bank_core import business_transactions
        today = _date.today()
        rows = business_transactions(supabase, business_id, today.replace(day=1).isoformat(), today.isoformat())
        return [r for r in rows if float(r.get("amount") or 0) < 0]
    except Exception as e:
        print(f"[report] bank charges skipped: {e}")
        return []
