from fastapi import APIRouter, HTTPException, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import os
import httpx
import base64 as b64lib
import time
import random

from supabase import create_client, Client

receipt_routes = APIRouter(prefix="/api", tags=["receiptvault"])

_supabase_client = None


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
        "createdAt": str(row.get("created_at", "")),
    }


def to_receipt(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "businessId": row.get("business_id"),
        "filePath": row.get("file_path"),
        "originalName": row.get("original_name"),
        "uploadedAt": str(row.get("uploaded_at", "")),
        "sentAt": str(row.get("sent_at")) if row.get("sent_at") else None,
    }


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
    result = supabase.table("businesses").select("*").eq("user_id", current_user.user.id).execute()
    if not result.data:
        return {"setupRequired": "business"}
    business = result.data[0]
    out = to_business(business)
    if not business.get("accountant_email") or not business.get("send_frequency") or not business.get("send_day"):
        out["setupRequired"] = "accountant"
    return out


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
    result = supabase.table("businesses").insert(data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create business")
    return to_business(result.data[0])


class AccountantConfigBody(BaseModel):
    accountantEmail: str
    sendFrequency: str
    sendDay: int
    sendEnabled: Optional[int] = 1


@receipt_routes.post("/setup/accountant")
async def setup_accountant(body: AccountantConfigBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = supabase.table("businesses").select("id").eq("user_id", current_user.user.id).execute()
    if not biz.data:
        raise HTTPException(status_code=400, detail="Business profile must be created first")
    business_id = biz.data[0]["id"]
    data = {
        "accountant_email": body.accountantEmail,
        "send_frequency": body.sendFrequency,
        "send_day": body.sendDay,
        "send_enabled": body.sendEnabled or 1,
    }
    result = supabase.table("businesses").update(data).eq("id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to update accountant config")
    return to_business(result.data[0])


@receipt_routes.get("/receipts")
async def list_receipts(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = supabase.table("businesses").select("id").eq("user_id", current_user.user.id).execute()
    if not biz.data:
        raise HTTPException(status_code=400, detail="Business not found")
    result = supabase.table("receipts").select("*").eq("business_id", biz.data[0]["id"]).execute()
    return [to_receipt(r) for r in result.data]


@receipt_routes.get("/receipts/{receipt_id}")
async def get_receipt(receipt_id: str):
    supabase = get_supabase()
    result = supabase.table("receipts").select("*").eq("id", receipt_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return to_receipt(result.data[0])


@receipt_routes.post("/receipts", status_code=201)
async def upload_receipt(request: Request, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = supabase.table("businesses").select("id").eq("user_id", current_user.user.id).execute()
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
    elif "multipart/form-data" in content_type:
        form = await request.form()
        file_obj = form.get("file")
        if not file_obj:
            raise HTTPException(status_code=400, detail="file is required")
        content = await file_obj.read()
        ext = os.path.splitext(file_obj.filename)[1].lower() or ".jpg"
        mime = file_obj.content_type or f"image/{ext[1:]}"
        original_name = file_obj.filename
    else:
        raise HTTPException(status_code=400, detail="Unsupported content type")

    unique = f"{int(time.time() * 1000)}-{random.randint(0, 999999999)}{ext}"
    supabase.storage.from_("receipts").upload(unique, content, {"content-type": mime})
    file_url = f"{supabase_url}/storage/v1/object/public/receipts/{unique}"

    receipt = supabase.table("receipts").insert({
        "business_id": business_id,
        "file_path": file_url,
        "original_name": original_name,
    }).execute()
    if not receipt.data:
        raise HTTPException(status_code=500, detail="Failed to create receipt record")
    return JSONResponse(status_code=201, content=to_receipt(receipt.data[0]))


@receipt_routes.delete("/receipts/{receipt_id}")
async def delete_receipt(receipt_id: str, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = supabase.table("businesses").select("id").eq("user_id", current_user.user.id).execute()
    if not biz.data:
        raise HTTPException(status_code=400, detail="Business not found")
    business_id = biz.data[0]["id"]
    result = supabase.table("receipts").delete().eq("id", receipt_id).eq("business_id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return {"message": "Receipt deleted"}


@receipt_routes.post("/send-now")
async def send_now(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = supabase.table("businesses").select("*").eq("user_id", current_user.user.id).execute()
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

    async with httpx.AsyncClient() as client:
        edge_res = await client.post(
            "https://wzcuzyouymauokijaqjk.supabase.co/functions/v1/send-receipts",
            json={
                "businessId": business["id"],
                "accountantEmail": business["accountant_email"],
                "businessName": business["business_name"],
                "receipts": [to_receipt(r) for r in unsent.data],
            },
        )
    if not edge_res.is_success:
        raise HTTPException(status_code=500, detail=f"Send failed: {edge_res.text}")
    result = edge_res.json()
    return {"ok": True, "sent": result.get("sent", len(unsent.data))}