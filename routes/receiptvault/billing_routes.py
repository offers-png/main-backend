import os
import json
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import create_client
from routes.receiptvault.routes import get_current_user
import httpx

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
SUPABASE_KEY = (os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind6Y3V6eW91eW1hdW9raWphcWprIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM5NDUyMDAsImV4cCI6MjA4OTUyMTIwMH0.fDuyCZGrCbL9Obd7l6FDnNd5AB-AUytp-3S60KwwKvM")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://receipts.dealdily.com")

PLANS = {
    "price_1ThalqHDiwSWl43o0HLDGwiH": {"plan": "starter", "member_limit": 10, "amount": 1999},
    "price_1ThalsHDiwSWl43oOv91jwa0": {"plan": "growth",  "member_limit": 25, "amount": 3499},
    "price_1ThalvHDiwSWl43olnKArHIQ": {"plan": "business","member_limit": 100,"amount": 5999},
}

billing_routes = APIRouter(prefix="/api", tags=["billing"])

def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def stripe_headers():
    return {
        "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

class CreateCheckoutBody(BaseModel):
    price_id: str

@billing_routes.get("/billing/plans")
async def get_plans():
    return [
        {"price_id": "price_1ThalqHDiwSWl43o0HLDGwiH", "plan": "starter",  "name": "Starter",  "price": 19.99, "member_limit": 10,  "features": ["Up to 10 team members", "Receipt OCR", "Mileage tracking", "Invoice creation", "Accountant email reports"]},
        {"price_id": "price_1ThalsHDiwSWl43oOv91jwa0", "plan": "growth",   "name": "Growth",   "price": 34.99, "member_limit": 25,  "features": ["Up to 25 team members", "Everything in Starter", "Manager & bookkeeper roles", "Priority support"]},
        {"price_id": "price_1ThalvHDiwSWl43olnKArHIQ", "plan": "business", "name": "Business", "price": 59.99, "member_limit": 100, "features": ["Up to 100 team members", "Everything in Growth", "Dedicated onboarding"]},
    ]

@billing_routes.post("/billing/checkout")
async def create_checkout(body: CreateCheckoutBody, current_user=Depends(get_current_user)):
    if body.price_id not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")

    supabase = get_supabase()
    biz = supabase.table("businesses").select("*").eq("user_id", current_user.user.id).execute()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")
    business = biz.data[0]

    # Create or retrieve Stripe customer
    stripe_customer_id = business.get("stripe_customer_id")
    if not stripe_customer_id:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.stripe.com/v1/customers",
                headers=stripe_headers(),
                data={"email": current_user.user.email, "metadata[business_id]": business["id"]},
            )
            customer = r.json()
            stripe_customer_id = customer["id"]
            supabase.table("businesses").update({"stripe_customer_id": stripe_customer_id}).eq("id", business["id"]).execute()

    # Create Stripe Checkout session
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            headers=stripe_headers(),
            data={
                "customer": stripe_customer_id,
                "mode": "subscription",
                "line_items[0][price]": body.price_id,
                "line_items[0][quantity]": "1",
                "subscription_data[trial_period_days]": "7",
                "success_url": f"{FRONTEND_URL}/?checkout=success",
                "cancel_url": f"{FRONTEND_URL}/pricing",
                "metadata[business_id]": business["id"],
                "metadata[price_id]": body.price_id,
            },
        )
        session = r.json()

    if "error" in session:
        raise HTTPException(status_code=400, detail=session["error"]["message"])

    return {"url": session["url"]}

@billing_routes.post("/billing/portal")
async def billing_portal(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = supabase.table("businesses").select("stripe_customer_id").eq("user_id", current_user.user.id).execute()
    if not biz.data or not biz.data[0].get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="No billing account found")
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.stripe.com/v1/billing_portal/sessions",
            headers=stripe_headers(),
            data={"customer": biz.data[0]["stripe_customer_id"], "return_url": f"{FRONTEND_URL}/settings"},
        )
        portal = r.json()
    return {"url": portal["url"]}

@billing_routes.get("/billing/status")
async def billing_status(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    biz = supabase.table("businesses").select("plan,member_limit,trial_ends_at,stripe_customer_id,stripe_subscription_id").eq("user_id", current_user.user.id).execute()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")
    b = biz.data[0]
    trial_ends_at = b.get("trial_ends_at")
    now = datetime.utcnow().isoformat()
    in_trial = bool(trial_ends_at and trial_ends_at > now)
    is_active = bool(b.get("plan") or in_trial)
    return {
        "plan": b.get("plan"),
        "member_limit": b.get("member_limit", 1),
        "trial_ends_at": trial_ends_at,
        "in_trial": in_trial,
        "is_active": is_active,
        "has_subscription": bool(b.get("stripe_subscription_id")),
    }

@billing_routes.post("/billing/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    # Verify webhook signature if secret is set
    if STRIPE_WEBHOOK_SECRET:
        import hmac, hashlib, time
        parts = {k: v for part in sig.split(",") for k, v in [part.split("=", 1)]}
        ts = parts.get("t", "0")
        signed = f"{ts}.{payload.decode()}"
        expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, parts.get("v1", "")):
            raise HTTPException(status_code=400, detail="Invalid signature")

    event = json.loads(payload)
    supabase = get_supabase()

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        business_id = session.get("metadata", {}).get("business_id")
        price_id = session.get("metadata", {}).get("price_id")
        subscription_id = session.get("subscription")
        if business_id and price_id in PLANS:
            plan_info = PLANS[price_id]
            supabase.table("businesses").update({
                "plan": plan_info["plan"],
                "member_limit": plan_info["member_limit"],
                "stripe_subscription_id": subscription_id,
                "trial_ends_at": None,
            }).eq("id", business_id).execute()

    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
        sub = event["data"]["object"]
        customer_id = sub.get("customer")
        if customer_id:
            supabase.table("businesses").update({
                "plan": None,
                "member_limit": 1,
                "stripe_subscription_id": None,
            }).eq("stripe_customer_id", customer_id).execute()

    elif event["type"] == "customer.subscription.updated":
        sub = event["data"]["object"]
        customer_id = sub.get("customer")
        new_price_id = sub.get("items", {}).get("data", [{}])[0].get("price", {}).get("id")
        if customer_id and new_price_id in PLANS:
            plan_info = PLANS[new_price_id]
            supabase.table("businesses").update({
                "plan": plan_info["plan"],
                "member_limit": plan_info["member_limit"],
            }).eq("stripe_customer_id", customer_id).execute()

    return {"ok": True}
