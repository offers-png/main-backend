from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import stripe
import os
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

checkout_routes = APIRouter()

PLAN_MAP = {
    "24h": timedelta(hours=24),
    "7d":  timedelta(days=7),
    "30d": timedelta(days=30),
    "lifetime": None,
}

PRICE_MAP = {
    "24h":      200,
    "7d":      1000,
    "30d":     3000,
    "lifetime":10000,
}


def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=500, detail="Missing Supabase credentials")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


@checkout_routes.get("/")
def checkout_root():
    return {"service": "checkout running"}


class CreateLinkBody(BaseModel):
    plan: str = "7d"

@checkout_routes.post("/create-link")
def create_link(body: CreateLinkBody):
    try:
        plan = body.plan
        if plan not in PRICE_MAP:
            raise HTTPException(status_code=400, detail="Invalid plan")
        if not stripe.api_key:
            raise HTTPException(status_code=500, detail="Missing STRIPE_SECRET_KEY")
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"API Access ({plan})"},
                    "unit_amount": PRICE_MAP[plan]
                },
                "quantity": 1
            }],
            success_url="https://one-time-checkout.onrender.com/success.html?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://one-time-checkout.onrender.com/cancel.html",
            metadata={"plan": plan},
        )
        return {"checkout_url": session.url}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@checkout_routes.get("/verify-session")
def verify_session(session_id: str):
    try:
        if not stripe.api_key:
            raise HTTPException(status_code=500, detail="Missing STRIPE_SECRET_KEY")
        supabase = get_supabase()

        # Idempotent — return existing key if already issued
        existing = supabase.table("api_keys").select("*").eq("session_id", session_id).execute()
        if existing.data:
            row = existing.data[0]
            return {"status": "success", "api_key": row["key"], "plan": row["plan"], "expires_at": row["expires_at"]}

        # Verify with Stripe
        session = stripe.checkout.Session.retrieve(session_id)
        if session.status != "complete" or session.payment_status != "paid":
            raise HTTPException(status_code=400, detail="Payment not completed")

        plan = session.metadata.get("plan", "7d") if session.metadata else "7d"
        api_key = f"ka_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc)
        delta = PLAN_MAP.get(plan)
        expires_at = (now + delta).isoformat() if delta else None

        supabase.table("api_keys").insert({
            "key": api_key,
            "session_id": session_id,
            "plan": plan,
            "created_at": now.isoformat(),
            "expires_at": expires_at,
            "active": True
        }).execute()

        return {"status": "success", "api_key": api_key, "plan": plan, "expires_at": expires_at}

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@checkout_routes.get("/validate-key")
def validate_key(api_key: str):
    try:
        supabase = get_supabase()
        result = supabase.table("api_keys").select("*").eq("key", api_key).execute()

        if not result.data:
            raise HTTPException(status_code=401, detail="Invalid API key")

        row = result.data[0]

        if not row["active"]:
            raise HTTPException(status_code=403, detail="API key disabled")

        if row["expires_at"]:
            expires = datetime.fromisoformat(row["expires_at"])
            if datetime.now(timezone.utc) > expires:
                raise HTTPException(status_code=403, detail="API key expired")

        supabase.table("api_keys").update({"used_count": row["used_count"] + 1}).eq("key", api_key).execute()

        return {"valid": True, "plan": row["plan"], "expires_at": row["expires_at"], "used_count": row["used_count"] + 1}

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
