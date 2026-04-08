from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import stripe
import os
import traceback

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

checkout_routes = APIRouter()

PLAN_MAP = {
    "24h": 24 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
    "30d": 30 * 24 * 60 * 60 * 1000,
    "lifetime": None,
}

PRICE_MAP = {
    "24h": 200,
    "7d": 1000,
    "30d": 3000,
    "lifetime": 10000,
}

class CreateLinkBody(BaseModel):
    plan: str = "7d"


# =========================
# VERIFY SESSION (GET KEY)
# =========================
@checkout_routes.get("/verify-session")
def verify_session(session_id: str):
    try:
        if not stripe.api_key:
            raise HTTPException(status_code=500, detail="Missing STRIPE_SECRET_KEY")

        session = stripe.checkout.Session.retrieve(session_id)

        if session.status != "complete" or session.payment_status != "paid":
            raise HTTPException(status_code=400, detail="Payment not completed")

        import uuid
        api_key = f"ka_{uuid.uuid4().hex}"

        plan = None
        if session.metadata and "plan" in session.metadata:
            plan = session.metadata["plan"]

        return {
            "status": "success",
            "api_key": api_key,
            "plan": plan
        }

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# =========================
# HEALTH CHECK
# =========================
@checkout_routes.get("/")
def checkout_root():
    return {"service": "checkout running"}


# =========================
# CREATE CHECKOUT LINK
# =========================
@checkout_routes.post("/create-link")
def create_link(body: CreateLinkBody):
    try:
        plan = body.plan

        if plan not in PRICE_MAP:
            raise HTTPException(status_code=400, detail="Invalid plan")

        if not stripe.api_key:
            raise HTTPException(status_code=500, detail="Missing STRIPE_SECRET_KEY")

        price = PRICE_MAP[plan]

        print("STRIPE KEY PREFIX:", stripe.api_key[:8] if stripe.api_key else "NONE")

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"API Access ({plan})"
                        },
                        "unit_amount": price,
                    },
                    "quantity": 1,
                }
            ],
            success_url="https://one-time-checkout.onrender.com/success.html?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://one-time-checkout.onrender.com/cancel.html",
            metadata={"plan": plan},
        )

        return {"checkout_url": session.url}

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
