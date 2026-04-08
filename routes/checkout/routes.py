from fastapi import APIRouter
from pydantic import BaseModel

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
async def create_link(body: CreateLinkBody):
    plan = body.plan
    price = PRICE_MAP.get(plan)

    if plan not in PLAN_MAP:
        return {"error": "Invalid plan"}

    return {
        "message": "create-link route wired",
        "plan": plan,
        "price": price
    }
