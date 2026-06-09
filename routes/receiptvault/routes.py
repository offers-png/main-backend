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
        return {}

    # Claude Vision only supports images, not PDFs
    if "pdf" in mime_type:
        return {}

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
                    "model": "claude-haiku-4-5-20251001",
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
    except Exception:
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
        if not business.get("accountant_email") or not business.get("send_frequency") or not business.get("send_day"):
            out["setupRequired"] = "accountant"
        return out

    # Check if user is a team member of another business
    member = supabase.table("business_users").select("*").eq("user_id", user_id).eq("status", "active").execute()
    if member.data:
        business_id = member.data[0]["business_id"]
        biz = supabase.table("businesses").select("*").eq("id", business_id).execute()
        if biz.data:
            out = to_business(biz.data[0])
            out["role"] = member.data[0].get("role", "employee")
            out["isTeamMember"] = True
            return out

    return {"setupRequired": "business"}


class BusinessProfileBody(BaseModel):
    businessName: str
    businessAddress: str
    ownerName: str
    ownerAddress: Optional[str] = None


@receipt_routes.post("/setup/business")
async def setup_business(body: BusinessProfileBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    data = {
        "user_id": current_user.user.id,
        "business_name": body.businessName,
        "business_address": body.businessAddress,
        "owner_name": body.ownerName,
        "owner_address": body.ownerAddress,
    }
    existing = type("BizR", (), {"data": ([{"id": _b["id"]}] if (_b := get_business_for_user(supabase, current_user.user.id)) else [])})()
    if existing.data:
        result = supabase.table("businesses").update(data).eq("id", existing.data[0]["id"]).execute()
    else:
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
    biz_data = get_business_for_user(supabase, current_user.user.id)
    biz = type("R", (), {"data": [{"id": biz_data["id"]}] if biz_data else []})()
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


@receipt_routes.get("/receipts")
async def list_receipts(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz_data = get_business_for_user(supabase, current_user.user.id)
    biz = type("R", (), {"data": [{"id": biz_data["id"]}] if biz_data else []})()
    if not biz.data:
        raise HTTPException(status_code=400, detail="Business not found")
    result = supabase.table("receipts").select("*").eq("business_id", biz.data[0]["id"]).order("uploaded_at", desc=True).execute()
    return [to_receipt(r) for r in result.data]


@receipt_routes.get("/receipts/{receipt_id}")
async def get_receipt(receipt_id: str):
    supabase = get_supabase()
    result = supabase.table("receipts").select("*").eq("id", receipt_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return to_receipt(result.data[0])


class UpdateReceiptBody(BaseModel):
    merchant: Optional[str] = None
    amount: Optional[float] = None
    receiptDate: Optional[str] = None
    category: Optional[str] = None
    notes: Optional[str] = None


@receipt_routes.patch("/receipts/{receipt_id}")
async def update_receipt(receipt_id: str, body: UpdateReceiptBody, current_user=Depends(get_current_user)):
    """Allow users to manually correct OCR-extracted fields."""
    supabase = get_supabase()
    biz_data = get_business_for_user(supabase, current_user.user.id)
    biz = type("R", (), {"data": [{"id": biz_data["id"]}] if biz_data else []})()
    if not biz.data:
        raise HTTPException(status_code=400, detail="Business not found")
    
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


@receipt_routes.post("/receipts", status_code=201)
async def upload_receipt(request: Request, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz_data = get_business_for_user(supabase, current_user.user.id)
    biz = type("R", (), {"data": [{"id": biz_data["id"]}] if biz_data else []})()
    if not biz.data:
        raise HTTPException(status_code=400, detail="Business not found")
    business_id = biz.data[0]["id"]

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

    # Check if uploader is owner or team member
    is_owner = type("BizR", (), {"data": ([{"id": _b["id"]}] if (_b := get_business_for_user(supabase, current_user.user.id)) else [])})()
    approval_status = "approved" if is_owner.data else "pending"

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


@receipt_routes.delete("/receipts/{receipt_id}")
async def delete_receipt(receipt_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz_data = get_business_for_user(supabase, current_user.user.id)
    biz = type("R", (), {"data": [{"id": biz_data["id"]}] if biz_data else []})()
    if not biz.data:
        raise HTTPException(status_code=400, detail="Business not found")
    business_id = biz.data[0]["id"]
    result = supabase.table("receipts").delete().eq("id", receipt_id).eq("business_id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return {"message": "Receipt deleted"}



@receipt_routes.post("/resend-all")
async def resend_all(current_user=Depends(get_current_user)):
    """Mark all receipts as unsent then send them again."""
    supabase = get_supabase()
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

@receipt_routes.post("/send-now")
async def send_now(current_user=Depends(get_current_user)):
    import asyncio
    supabase = get_supabase()
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
