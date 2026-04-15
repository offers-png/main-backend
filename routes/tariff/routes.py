from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel
from typing import Optional, List
import os
import httpx
from datetime import datetime, timedelta
import stripe

router = APIRouter(prefix="/tariff", tags=["tariff"])

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
STRIPE_TARIFF_PRICE_ID = os.environ.get("STRIPE_TARIFF_PRICE_ID", "")

stripe.api_key = STRIPE_SECRET_KEY

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    email: str
    business_name: Optional[str] = None
    business_type: Optional[str] = None

class AddProductRequest(BaseModel):
    user_id: str
    product_name: str
    supplier_country: str
    current_cost: float
    selling_price: float
    units_per_month: int = 0
    category: str = "general"
    notes: Optional[str] = None

class AIAnalysisRequest(BaseModel):
    user_id: str

class ReadAlertsRequest(BaseModel):
    alert_ids: List[str]

class CheckoutRequest(BaseModel):
    user_id: str
    email: str

class ApplyPriceRequest(BaseModel):
    product_id: str

# ─── Helpers ──────────────────────────────────────────────────────────────────

async def sb_get(path: str):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SUPABASE_HEADERS)
        return r.json()

async def sb_post(path: str, data: dict):
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=SUPABASE_HEADERS, json=data)
        return r.json()

async def sb_patch(path: str, data: dict):
    async with httpx.AsyncClient() as client:
        h = {**SUPABASE_HEADERS, "Prefer": "return=representation"}
        r = await client.patch(f"{SUPABASE_URL}/rest/v1/{path}", headers=h, json=data)
        return r.json()

async def get_tariff_rates() -> dict:
    rates_raw = await sb_get("tariff_rates?select=country,category,rate")
    rates = {}
    for row in rates_raw:
        country = row["country"]
        category = row["category"]
        rate = float(row["rate"])
        if country not in rates:
            rates[country] = {}
        rates[country][category] = rate
    return rates

def calculate_product_impact(
    current_cost: float,
    selling_price: float,
    supplier_country: str,
    category: str,
    units_per_month: int,
    tariff_rates: dict
) -> dict:
    country_rates = tariff_rates.get(supplier_country, tariff_rates.get("Other", {}))
    tariff_rate = country_rates.get(category, country_rates.get("general", 10.0))

    cost_with_tariff = current_cost * (1 + tariff_rate / 100)

    if selling_price <= 0:
        selling_price = 0.01

    original_margin = ((selling_price - current_cost) / selling_price) * 100
    current_margin = ((selling_price - cost_with_tariff) / selling_price) * 100

    margin_ratio = original_margin / 100
    if margin_ratio >= 1:
        margin_ratio = 0.999
    recommended_price = cost_with_tariff / (1 - margin_ratio) if margin_ratio < 1 else selling_price

    monthly_impact = (cost_with_tariff - current_cost) * units_per_month

    return {
        "tariff_rate": tariff_rate,
        "cost_with_tariff": round(cost_with_tariff, 2),
        "original_margin_pct": round(original_margin, 1),
        "current_margin_pct": round(current_margin, 1),
        "recommended_price": round(recommended_price, 2),
        "price_increase_needed": round(recommended_price - selling_price, 2),
        "monthly_tariff_burden": round(monthly_impact, 2)
    }


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/user")
async def create_user(req: CreateUserRequest):
    # Check if user exists
    existing = await sb_get(f"tariff_users?email=eq.{req.email}&select=*")
    if existing:
        return existing[0]

    user_data = {
        "email": req.email,
        "business_name": req.business_name,
        "business_type": req.business_type,
        "plan": "free"
    }
    result = await sb_post("tariff_users", user_data)
    if isinstance(result, list) and result:
        return result[0]
    raise HTTPException(status_code=400, detail="Failed to create user")


@router.get("/user/{email}")
async def get_user(email: str):
    users = await sb_get(f"tariff_users?email=eq.{email}&select=*")
    if not users:
        raise HTTPException(status_code=404, detail="User not found")
    user = users[0]

    unread = await sb_get(f"tariff_alerts?user_id=eq.{user['id']}&read=eq.false&select=id")
    user["unread_alert_count"] = len(unread)
    return user


@router.post("/product")
async def add_product(req: AddProductRequest):
    # Check free plan product limit
    user_list = await sb_get(f"tariff_users?id=eq.{req.user_id}&select=plan")
    if not user_list:
        raise HTTPException(status_code=404, detail="User not found")
    user = user_list[0]

    if user["plan"] == "free":
        existing_products = await sb_get(f"tariff_products?user_id=eq.{req.user_id}&select=id")
        if len(existing_products) >= 5:
            raise HTTPException(status_code=402, detail="Free plan limit: 5 products. Upgrade to Pro for unlimited.")

    tariff_rates = await get_tariff_rates()
    impact = calculate_product_impact(
        req.current_cost, req.selling_price, req.supplier_country,
        req.category, req.units_per_month, tariff_rates
    )

    product_data = {
        "user_id": req.user_id,
        "product_name": req.product_name,
        "supplier_country": req.supplier_country,
        "current_cost": req.current_cost,
        "selling_price": req.selling_price,
        "units_per_month": req.units_per_month,
        "category": req.category,
        "notes": req.notes
    }
    result = await sb_post("tariff_products", product_data)

    if isinstance(result, list) and result:
        product = result[0]
        # Create alert if margin dropped significantly
        if impact["current_margin_pct"] < 15:
            alert_data = {
                "user_id": req.user_id,
                "product_id": product["id"],
                "alert_type": "margin_warning",
                "new_rate": impact["tariff_rate"],
                "message": f"{req.product_name} margin is {impact['current_margin_pct']}% after {impact['tariff_rate']}% tariff — consider raising price to ${impact['recommended_price']}",
                "read": False
            }
            await sb_post("tariff_alerts", alert_data)
        return {**product, **impact}

    raise HTTPException(status_code=400, detail="Failed to add product")


@router.get("/products/{user_id}")
async def get_products(user_id: str):
    products = await sb_get(f"tariff_products?user_id=eq.{user_id}&select=*&order=created_at.desc")
    if not products:
        return {"products": [], "summary": {"total_monthly_burden": 0, "total_margin_loss": 0, "avg_original_margin": 0, "avg_current_margin": 0}}

    tariff_rates = await get_tariff_rates()
    enriched = []
    total_burden = 0
    total_margin_loss = 0
    orig_margins = []
    curr_margins = []

    for p in products:
        impact = calculate_product_impact(
            float(p["current_cost"]), float(p["selling_price"]),
            p["supplier_country"], p["category"] or "general",
            p["units_per_month"] or 0, tariff_rates
        )
        enriched.append({**p, **impact})
        total_burden += impact["monthly_tariff_burden"]
        total_margin_loss += impact["price_increase_needed"] * (p["units_per_month"] or 0)
        orig_margins.append(impact["original_margin_pct"])
        curr_margins.append(impact["current_margin_pct"])

    return {
        "products": enriched,
        "summary": {
            "total_monthly_burden": round(total_burden, 2),
            "total_margin_loss": round(total_burden, 2),
            "avg_original_margin": round(sum(orig_margins) / len(orig_margins), 1) if orig_margins else 0,
            "avg_current_margin": round(sum(curr_margins) / len(curr_margins), 1) if curr_margins else 0,
            "product_count": len(enriched)
        }
    }


@router.get("/dashboard/{user_id}")
async def get_dashboard(user_id: str):
    products_data = await get_products(user_id)
    products = products_data["products"]
    summary = products_data["summary"]

    alerts = await sb_get(f"tariff_alerts?user_id=eq.{user_id}&read=eq.false&select=*&order=created_at.desc&limit=10")

    # Top 3 most impacted by dollar amount
    sorted_products = sorted(products, key=lambda x: x.get("monthly_tariff_burden", 0), reverse=True)
    top_impacted = sorted_products[:3]

    # Products at risk (margin < 15%)
    at_risk_count = len([p for p in products if p.get("current_margin_pct", 100) < 15])

    # Potential recovery = sum of all recommended price increases * units
    potential_recovery = sum(
        max(0, p.get("price_increase_needed", 0)) * (p.get("units_per_month") or 0)
        for p in products
    )

    # Business health score (0-100)
    avg_curr = summary["avg_current_margin"]
    health_score = min(100, max(0, int(avg_curr * 2.5)))

    return {
        "total_monthly_burden": summary["total_monthly_burden"],
        "avg_original_margin": summary["avg_original_margin"],
        "avg_current_margin": summary["avg_current_margin"],
        "at_risk_count": at_risk_count,
        "potential_recovery": round(potential_recovery, 2),
        "top_impacted": top_impacted,
        "unread_alerts": alerts,
        "health_score": health_score,
        "product_count": summary["product_count"]
    }


@router.post("/ai-analysis")
async def ai_analysis(req: AIAnalysisRequest):
    # Check user plan + usage
    user_list = await sb_get(f"tariff_users?id=eq.{req.user_id}&select=*")
    if not user_list:
        raise HTTPException(status_code=404, detail="User not found")
    user = user_list[0]

    if user["plan"] == "free":
        one_week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
        recent_usage = await sb_get(f"tariff_ai_usage?user_id=eq.{req.user_id}&used_at=gte.{one_week_ago}&select=id")
        if len(recent_usage) >= 1:
            raise HTTPException(status_code=402, detail="Free plan: 1 AI analysis per week. Upgrade to Pro for unlimited.")

    products_data = await get_products(req.user_id)
    products = products_data["products"]
    summary = products_data["summary"]

    if not products:
        raise HTTPException(status_code=400, detail="Add products first before running analysis.")

    product_lines = []
    for p in products:
        product_lines.append(
            f"- {p['product_name']} (from {p['supplier_country']}, category: {p.get('category','general')}): "
            f"cost ${p['current_cost']}, selling at ${p['selling_price']}, "
            f"{p.get('units_per_month',0)} units/month, "
            f"tariff rate {p.get('tariff_rate',0)}%, "
            f"margin dropped from {p.get('original_margin_pct',0)}% to {p.get('current_margin_pct',0)}%, "
            f"monthly tariff burden ${p.get('monthly_tariff_burden',0)}"
        )

    product_text = "\n".join(product_lines)
    total_burden = summary["total_monthly_burden"]
    orig_margin = summary["avg_original_margin"]
    curr_margin = summary["avg_current_margin"]

    user_prompt = f"""This small business owner ({user.get('business_name','a small business')}, type: {user.get('business_type','retail')}) has these products:

{product_text}

Their total monthly tariff burden is ${total_burden:.2f}. Their average margin has dropped from {orig_margin}% to {curr_margin}%.

Give them 3 specific recommendations:
1) Which products to raise prices on first and by exactly how much (give dollar amounts).
2) Which supplier countries to consider switching away from and what to switch to.
3) One immediate action to protect cash flow this month.

Be specific with dollar amounts and percentages. Be direct and practical."""

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "system": "You are a pricing consultant for small business owners dealing with tariff impacts. Give specific, actionable, numbers-based advice. Be direct and practical. Never give legal advice. Format your response with clear numbered sections.",
                "messages": [{"role": "user", "content": user_prompt}]
            }
        )

    if r.status_code != 200:
        raise HTTPException(status_code=500, detail="AI analysis failed")

    analysis_text = r.json()["content"][0]["text"]

    # Log usage
    await sb_post("tariff_ai_usage", {"user_id": req.user_id})

    return {
        "analysis": analysis_text,
        "generated_at": datetime.utcnow().isoformat(),
        "total_burden": total_burden,
        "avg_margin_before": orig_margin,
        "avg_margin_after": curr_margin
    }


@router.post("/checkout")
async def create_checkout(req: CheckoutRequest):
    try:
        session = stripe.checkout.Session.create(
            customer_email=req.email,
            payment_method_types=["card"],
            line_items=[{"price": STRIPE_TARIFF_PRICE_ID, "quantity": 1}],
            mode="subscription",
            success_url="https://tarifftrack.netlify.app/dashboard?upgraded=true",
            cancel_url="https://tarifftrack.netlify.app/dashboard?cancelled=true",
            metadata={"user_id": req.user_id}
        )
        return {"checkout_url": session.url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("metadata", {}).get("user_id")
        customer_id = session.get("customer")
        sub_id = session.get("subscription")
        if user_id:
            await sb_patch(
                f"tariff_users?id=eq.{user_id}",
                {"plan": "pro", "stripe_customer_id": customer_id, "stripe_subscription_id": sub_id}
            )

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        sub_id = sub["id"]
        users = await sb_get(f"tariff_users?stripe_subscription_id=eq.{sub_id}&select=id")
        if users:
            await sb_patch(f"tariff_users?id=eq.{users[0]['id']}", {"plan": "free"})

    return {"status": "ok"}


@router.get("/alerts/{user_id}")
async def get_alerts(user_id: str):
    alerts = await sb_get(f"tariff_alerts?user_id=eq.{user_id}&select=*,tariff_products(product_name)&order=read.asc,created_at.desc&limit=50")
    return {"alerts": alerts}


@router.post("/alerts/read")
async def mark_alerts_read(req: ReadAlertsRequest):
    for alert_id in req.alert_ids:
        await sb_patch(f"tariff_alerts?id=eq.{alert_id}", {"read": True})
    return {"status": "ok", "marked": len(req.alert_ids)}


@router.post("/product/apply-price")
async def apply_price(req: ApplyPriceRequest):
    result = await sb_patch(f"tariff_products?id=eq.{req.product_id}", {"price_updated": True})
    return {"status": "ok"}
