from fastapi import APIRouter
from pydantic import BaseModel
import stripe
import os

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

@checkout_routes.get("/")
def checkout_root():
    return {"service": "checkout running"}
@checkout_routes.post("/create-link")
def create_link(body: CreateLinkBody):
    plan = body.plan

    if plan not in PRICE_MAP:
        return {"error": "Invalid plan"}

    price = PRICE_MAP[plan]

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
        success_url="https://yourdomain.com/success",
        cancel_url="https://yourdomain.com/cancel",
        metadata={"plan": plan},
    )

    return {
        "checkout_url": session.url
    }
    return {
        "message": "create-link route wired",
        "plan": plan,
        "price": price
    }
