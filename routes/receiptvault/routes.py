from fastapi import APIRouter, HTTPException, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import os
import httpx
import base64 as b64lib
import time
import random
import json
from datetime import datetime, timedelta

from supabase import create_client, Client

receipt_routes = APIRouter(prefix="/api", tags=["receiptvault"])

_supabase_client = None

CATEGORIES = ["Inventory", "Meals & Entertainment", "Travel", "Office Supplies", "Utilities", "Software & Subscriptions", "Advertising", "Vehicle & Fuel", "Equipment", "Other"]

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


def get_business_for_user(supabase, user_id: str):
    """Get business for owner OR active team member."""
    result = supabase.table("businesses").select("*").eq("user_id", user_id).execute()
    if result.data:
        return result.data[0]
    member = supabase.table("business_users").select("business_id").eq("user_id", user_id).eq("status", "active").execute()
    if member.data:
        biz = supabase.table("businesses").select("*").eq("id", member.data[0]["business_id"]).execute()
        if biz.data:
            return biz.data[0]
    return None


def resolve_role(supabase, user_id: str):
    """Returns (business, role). role is 'owner' | 'manager' | 'bookkeeper' | 'employee'.
    Returns (None, None) if user has no business."""
    result = supabase.table("businesses").select("*").eq("user_id", user_id).execute()
    if result.data:
        return result.data[0], "owner"
    member = supabase.table("business_users").select("business_id, role").eq("user_id", user_id).eq("status", "active").execute()
    if member.data:
        biz = supabase.table("businesses").select("*").eq("id", member.data[0]["business_id"]).execute()
        if biz.data:
            return biz.data[0], member.data[0].get("role") or "employee"
    return None, None


ALL_MODULES = ["receipts", "money_box", "mileage", "invoices", "inventory"]


def resolve_module_access(supabase, user_id: str, module: str):
    """Returns the business dict if this user can access `module`.
    The owner always has every module. Team members only have what the
    owner explicitly checked off for them on the Team page.
    Raises 403 if they're on the team but don't have this module, 404 if
    they have no business relationship at all."""
    biz = supabase.table("businesses").select("*").eq("user_id", user_id).execute()
    if biz.data:
        return biz.data[0]  # owner: full access, always
    member = supabase.table("business_users").select("business_id, modules").eq("user_id", user_id).eq("status", "active").execute()
    if member.data:
        modules = member.data[0].get("modules") or []
        if module not in modules:
            raise HTTPException(status_code=403, detail=f"You don't have access to this. Ask the owner to add it on the Team page.")
        b = supabase.table("businesses").select("*").eq("id", member.data[0]["business_id"]).execute()
        if b.data:
            return b.data[0]
    raise HTTPException(status_code=404, detail="Business not found")


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


async def require_active_subscription(current_user=Depends(get_current_user)):
    """Hard-lockout gate. Raises 402 if the business has no paid plan and
    no active trial. Applies to the business the CURRENT user belongs to —
    whether they're the owner or a team member — since team members don't
    have their own billing state, only the owner's business does.

    Deliberately permissive if no business is found yet (e.g. a team
    member mid-invite-acceptance, or a stale token) — that's a 404
    elsewhere, not a billing problem, so we let it pass through here.
    """
    supabase = get_supabase()
    business, _role = resolve_role(supabase, current_user.user.id)
    if business is None:
        return
    plan = business.get("plan")
    trial_ends_at = business.get("trial_ends_at")
    in_trial = bool(trial_ends_at and trial_ends_at > datetime.utcnow().isoformat())
    if not plan and not in_trial:
        raise HTTPException(
            status_code=402,
            detail="Your free trial has ended. Subscribe to keep using ReceiptVault.",
        )


def to_business(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "userId": row.get("user_id"),
        "businessName": row.get("business_name"),
        "businessAddress": row.get("business_address"),
        "ownerName": row.get("owner_name"),
        "ownerAddress": row.get("owner_address"),
        "accountantEmail": row.get("accountant_email"),
        "sendFrequency": row.get("send_frequency"),
        "sendDay": row.get("send_day"),
        "sendEnabled": row.get("send_enabled", 1),
        "ownerPhone": row.get("owner_phone"),
        "taxId": row.get("tax_id"),
        "weekStartDay": row.get("week_start_day", 0),
        "createdAt": str(row.get("created_at", "")),
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
        "notes": row.get("notes"),
        "uploadedAt": str(row.get("uploaded_at", "")),
        "sentAt": str(row.get("sent_at")) if row.get("sent_at") else None,
    }


async def extract_receipt_data(image_base64: str, mime_type: str) -> dict:
    """Use Claude Vision to extract merchant, amount, date, category from receipt image."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("[extract] ANTHROPIC_API_KEY not set on Render — skipping OCR, fields will be blank")
        return {}

    # Claude Vision only supports images, not PDFs
    if "pdf" in mime_type:
        return {}

    # Normalize mime — in-app camera uploads send "image/jpg", which the API
    # rejects (it only accepts image/jpeg, png, gif, webp)
    mime_type = (mime_type or "").lower().strip()
    if mime_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        mime_type = "image/png" if "png" in mime_type else "image/jpeg"

    # Downscale large camera captures — the API rejects images over 5MB,
    # and phone cameras routinely exceed that. Resizing also speeds up OCR.
    try:
        raw = b64lib.b64decode(image_base64)
        if len(raw) > 1_500_000:  # anything over ~1.5MB gets resized
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
  "category": "one of: {', '.join(CATEGORIES)}"
}}

CRITICAL RULES:
- "merchant" = the SELLER/VENDOR/SUPPLIER name at the TOP of the invoice (e.g. PepsiCo, Heffron Distributing, Restaurant Depot, Walmart, Husted Dairy)
- NEVER use the buyer/customer name (e.g. Shamrock Market, Fat Boys) as the merchant
- On distributor invoices: "Send payment to" or the company logo at top = the merchant
- amount = the final TOTAL DUE or TOTAL amount (a number, no $ sign)
- receipt_date = YYYY-MM-DD format, or null
- category must be exactly one of the provided options
- For beverage distributors, food suppliers, dairy: use "Inventory"
- For restaurant supply stores: use "Inventory"  
- For office/store supplies (bags, cups, t-shirts): use "Office Supplies"
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
                                {
                                    "type": "image",
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
        return parsed
    except Exception as ocr_err:
        print(f"OCR error: {ocr_err}")
        return {}


@receipt_routes.get("/user")
async def get_user(current_user=Depends(get_current_user)):
    return {
        "id": current_user.user.id,
        "email": current_user.user.email,
        "user_metadata": current_user.user.user_metadata,
        "created_at": str(current_user.user.created_at),
    }


@receipt_routes.get("/business")
async def get_business(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    user_id = current_user.user.id

    # First check if user owns a business
    result = supabase.table("businesses").select("*").eq("user_id", user_id).execute()
    if result.data:
        business = result.data[0]
        out = to_business(business)
        out["role"] = "owner"
        out["modules"] = ALL_MODULES
        if not business.get("accountant_email") or not business.get("send_frequency") or not business.get("send_day"):
            out["setupRequired"] = "accountant"
        return out

    # Check if user is an active team member
    member = supabase.table("business_users").select("*").eq("user_id", user_id).eq("status", "active").execute()
    if member.data:
        business_id = member.data[0]["business_id"]
        biz = supabase.table("businesses").select("*").eq("id", business_id).execute()
        if biz.data:
            out = to_business(biz.data[0])
            out["role"] = member.data[0].get("role", "employee")
            out["modules"] = member.data[0].get("modules") or []
            out["isTeamMember"] = True
            return out

    # Auto-link: check if there is a pending invite for this user's email
    user_email = current_user.user.email
    if user_email:
        pending = supabase.table("business_users").select("*").eq("email", user_email).execute()
        if pending.data:
            invite = pending.data[0]
            # Update placeholder user_id to real UUID and activate
            supabase.table("business_users").update({
                "user_id": user_id,
                "status": "active",
                "joined_at": datetime.utcnow().isoformat(),
            }).eq("id", invite["id"]).execute()
            # Now return their business
            biz = supabase.table("businesses").select("*").eq("id", invite["business_id"]).execute()
            if biz.data:
                out = to_business(biz.data[0])
                out["role"] = invite.get("role", "employee")
                out["modules"] = invite.get("modules") or []
                out["isTeamMember"] = True
                return out

    return {"setupRequired": "business"}


class BusinessProfileBody(BaseModel):
    businessName: str
    businessAddress: str
    ownerName: str
    ownerAddress: Optional[str] = None
    taxId: Optional[str] = None
    weekStartDay: Optional[int] = None


@receipt_routes.post("/setup/business")
async def setup_business(body: BusinessProfileBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    user_id = current_user.user.id

    # Does this user already own a business?
    owned = supabase.table("businesses").select("id").eq("user_id", user_id).execute()

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
        if body.weekStartDay is not None:
            data["week_start_day"] = body.weekStartDay
        result = supabase.table("businesses").update(data).eq("id", owned.data[0]["id"]).execute()
    else:
        # Not an owner. If they're already a team member somewhere, they don't
        # get to create/edit a business profile — only the owner can.
        member = supabase.table("business_users").select("id").eq("user_id", user_id).eq("status", "active").execute()
        if member.data:
            raise HTTPException(status_code=403, detail="Only the business owner can edit the business profile.")
        # Brand new user with no business and no team membership — normal
        # first-time signup flow. They become the owner of a new business.
        # Start their 7-day trial now — without this, a fresh signup would
        # have no plan and no trial, and hard-lockout enforcement would
        # block them before they ever got to use the product.
        data = {
            "user_id": user_id,
            "business_name": body.businessName,
            "business_address": body.businessAddress,
            "owner_name": body.ownerName,
            "owner_address": body.ownerAddress,
            "trial_ends_at": (datetime.utcnow() + timedelta(days=7)).isoformat(),
        }
        if body.taxId is not None:
            data["tax_id"] = body.taxId
        if body.weekStartDay is not None:
            data["week_start_day"] = body.weekStartDay
        result = supabase.table("businesses").insert(data).execute()

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
    owned = supabase.table("businesses").select("id").eq("user_id", current_user.user.id).execute()
    if not owned.data:
        raise HTTPException(status_code=403, detail="Only the business owner can edit accountant settings.")
    biz = type("R", (), {"data": [{"id": owned.data[0]["id"]}]})()
    if not biz.data:
        raise HTTPException(status_code=400, detail="Business profile must be created first")
    business_id = biz.data[0]["id"]
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


@receipt_routes.get("/receipts", dependencies=[Depends(require_active_subscription)])
async def list_receipts(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business = resolve_module_access(supabase, current_user.user.id, "receipts")
    result = supabase.table("receipts").select("*").eq("business_id", business["id"]).order("uploaded_at", desc=True).execute()
    return [to_receipt(r) for r in result.data]


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
    return to_receipt(result.data[0])


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
    ocr_data = await extract_receipt_data(b64lib.b64encode(content).decode("utf-8"), mime)
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

    if update_data:
        updated = supabase.table("receipts").update(update_data).eq("id", receipt_id).execute()
        return to_receipt(updated.data[0])
    return to_receipt(row)


class UpdateReceiptBody(BaseModel):
    merchant: Optional[str] = None
    amount: Optional[float] = None
    receiptDate: Optional[str] = None
    category: Optional[str] = None
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
        update_data["category"] = body.category
    if body.notes is not None:
        update_data["notes"] = body.notes

    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = supabase.table("receipts").update(update_data).eq("id", receipt_id).eq("business_id", biz.data[0]["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return to_receipt(result.data[0])


@receipt_routes.post("/receipts", status_code=201, dependencies=[Depends(require_active_subscription)])
async def upload_receipt(request: Request, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business = resolve_module_access(supabase, current_user.user.id, "receipts")
    business_id = business["id"]

    supabase_url = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
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

    # Upload to Supabase Storage
    unique = f"{int(time.time() * 1000)}-{random.randint(0, 999999999)}{ext}"
    supabase.storage.from_("receipts").upload(unique, content, {"content-type": mime})
    file_url = f"{supabase_url}/storage/v1/object/public/receipts/{unique}"

    # Run OCR via Claude Vision (non-blocking — if it fails, receipt still saves)
    ocr_data = await extract_receipt_data(ocr_base64, mime)

    # ── Duplicate check ─────────────────────────────────────────────────
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

    # Owner/manager/bookkeeper uploads auto-approve; employee uploads need approval
    _, uploader_role = resolve_role(supabase, current_user.user.id)
    approval_status = "approved" if uploader_role in AUTO_APPROVE_ROLES else "pending"

    insert_payload = {
        "business_id": business_id,
        "file_path": file_url,
        "original_name": original_name,
        "submitted_by": current_user.user.id,
        "approval_status": approval_status,
    }

    if ocr_data.get("merchant"):
        insert_payload["merchant"] = ocr_data["merchant"]
    if ocr_data.get("amount") is not None:
        insert_payload["amount"] = float(ocr_data["amount"])
    if ocr_data.get("receipt_date"):
        insert_payload["receipt_date"] = ocr_data["receipt_date"]
    if ocr_data.get("category"):
        insert_payload["category"] = ocr_data["category"]

    receipt = supabase.table("receipts").insert(insert_payload).execute()
    if not receipt.data:
        raise HTTPException(status_code=500, detail="Failed to create receipt record")
    return JSONResponse(status_code=201, content=to_receipt(receipt.data[0]))


@receipt_routes.delete("/receipts/{receipt_id}", dependencies=[Depends(require_active_subscription)])
async def delete_receipt(receipt_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business = resolve_module_access(supabase, current_user.user.id, "receipts")
    business_id = business["id"]
    result = supabase.table("receipts").delete().eq("id", receipt_id).eq("business_id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return {"message": "Receipt deleted"}



@receipt_routes.post("/resend-all", dependencies=[Depends(require_active_subscription)])
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

    # Mark ALL receipts as unsent
    supabase.table("receipts").update({"sent_at": None}).eq("business_id", business["id"]).execute()

    # Now call send_now logic
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

    from datetime import datetime, timezone
    sent_at = datetime.now(timezone.utc).isoformat()
    for r in all_receipts.data:
        supabase.table("receipts").update({"sent_at": sent_at}).eq("id", r["id"]).execute()

    async def send_in_background():
        try:
            from pdf_generator import generate_cover_pdf
            from excel_generator import build_excel
            period_label = datetime.now().strftime("%B %Y") + " Expenses"
            biz_name = business.get("business_name", "")
            safe_name = biz_name.replace(" ", "_")
            month_str = datetime.now().strftime("%Y-%m")

            # Generate PDF cover sheet
            pdf_bytes = await generate_cover_pdf(
                business_name=biz_name,
                owner_name=business.get("owner_name", ""),
                accountant_email=business.get("accountant_email", ""),
                receipts=receipts,
                period_label=period_label,
            )
            pdf_base64 = b64lib.b64encode(pdf_bytes).decode("utf-8")
            pdf_filename = f"ReceiptVault_{safe_name}_{month_str}_RESEND.pdf"

            # Generate Excel workbook
            excel_bytes = build_excel(
                business_name=biz_name,
                period_label=period_label,
                receipts=receipts,
                gross_income=0.0,
            )
            excel_base64 = b64lib.b64encode(excel_bytes).decode("utf-8")
            excel_filename = f"ReceiptVault_{safe_name}_{month_str}_RESEND.xlsx"

            async with httpx.AsyncClient(timeout=120.0) as client:
                await client.post(
                    "https://wzcuzyouymauokijaqjk.supabase.co/functions/v1/send-receipts",
                    json={
                        "businessId": business["id"],
                        "accountantEmail": business["accountant_email"],
                        "businessName": biz_name,
                        "receipts": receipts,
                        "pdfBase64": pdf_base64,
                        "pdfFilename": pdf_filename,
                        "excelBase64": excel_base64,
                        "excelFilename": excel_filename,
                    },
                )
        except Exception as e:
            print(f"Background resend error: {e}")

    import asyncio
    asyncio.create_task(send_in_background())
    return {"ok": True, "sent": sent_count}

@receipt_routes.post("/send-now", dependencies=[Depends(require_active_subscription)])
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

    # Mark all as sent immediately so UI updates fast
    from datetime import datetime, timezone
    sent_at = datetime.now(timezone.utc).isoformat()
    for r in unsent.data:
        supabase.table("receipts").update({"sent_at": sent_at}).eq("id", r["id"]).execute()

    # Fire PDF generation + email in background — don't wait for it
    async def send_in_background():
        try:
            from pdf_generator import generate_cover_pdf
            from excel_generator import build_excel
            period_label = datetime.now().strftime("%B %Y") + " Expenses"
            biz_name = business.get("business_name", "")
            safe_name = biz_name.replace(" ", "_")
            month_str = datetime.now().strftime("%Y-%m")

            # Generate PDF cover sheet
            pdf_bytes = await generate_cover_pdf(
                business_name=biz_name,
                owner_name=business.get("owner_name", ""),
                accountant_email=business.get("accountant_email", ""),
                receipts=receipts,
                period_label=period_label,
            )
            pdf_base64 = b64lib.b64encode(pdf_bytes).decode("utf-8")
            pdf_filename = f"ReceiptVault_{safe_name}_{month_str}.pdf"

            # Generate Excel workbook
            excel_bytes = build_excel(
                business_name=biz_name,
                period_label=period_label,
                receipts=receipts,
                gross_income=0.0,
            )
            excel_base64 = b64lib.b64encode(excel_bytes).decode("utf-8")
            excel_filename = f"ReceiptVault_{safe_name}_{month_str}.xlsx"

            async with httpx.AsyncClient(timeout=120.0) as client:
                await client.post(
                    "https://wzcuzyouymauokijaqjk.supabase.co/functions/v1/send-receipts",
                    json={
                        "businessId": business["id"],
                        "accountantEmail": business["accountant_email"],
                        "businessName": biz_name,
                        "receipts": receipts,
                        "pdfBase64": pdf_base64,
                        "pdfFilename": pdf_filename,
                        "excelBase64": excel_base64,
                        "excelFilename": excel_filename,
                    },
                )

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
            print(f"Background send error: {e}")

    asyncio.create_task(send_in_background())

    # Return immediately — email sends in background
    return {"ok": True, "sent": sent_count}
