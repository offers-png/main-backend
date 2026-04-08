from fastapi import APIRouter, Request

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

@checkout_routes.get("/")
def checkout_root():
    return {"service": "checkout running"}

@checkout_routes.post("/create-link")
async def create_link(request: Request):
    body = await request.json()
    plan = body.get("plan", "7d")
    price = PRICE_MAP.get(plan)

    if plan not in PLAN_MAP:
        return {"error": "Invalid plan"}

    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    base_url = f"{proto}://{host}"

    return {
        "message": "create-link route wired",
        "plan": plan,
        "price": price,
        "base_url": base_url
    }
