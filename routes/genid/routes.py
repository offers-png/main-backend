from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
import os

genid_routes = APIRouter()

# -------- Models --------

class IssueRequest(BaseModel):
    wallet: str
    email: str | None = None

class LookupRequest(BaseModel):
    genid: str

class VerifyRequest(BaseModel):
    genid: str


# -------- Routes --------

@genid_routes.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "genid"
    }


@genid_routes.post("/issue")
async def issue_genid(data: IssueRequest):
    """
    Placeholder:
    Replace with your Supabase + mint logic
    """
    return {
        "success": True,
        "genid": f"GEN-{data.wallet[-6:]}",
        "wallet": data.wallet
    }


@genid_routes.post("/lookup")
async def lookup_genid(data: LookupRequest):
    """
    Replace with database lookup
    """
    return {
        "found": True,
        "genid": data.genid,
        "status": "active"
    }


@genid_routes.post("/verify")
async def verify_genid(data: VerifyRequest):
    """
    Replace with verification logic
    """
    return {
        "verified": True,
        "genid": data.genid
    }


@genid_routes.post("/embed")
async def embed_payload(request: Request):
    body = await request.json()
    return {
        "success": True,
        "received": body
    }


@genid_routes.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """
    Add Stripe signature verification here
    """
    payload = await request.body()

    return {
        "received": True
    }


@genid_routes.post("/stripe/session")
async def create_checkout():
    """
    Replace with Stripe checkout session creation
    """
    return {
        "checkout_url": "https://checkout.stripe.com/test"
    }
