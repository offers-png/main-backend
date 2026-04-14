"""
GigLedger — /gig/ routes to add to your existing FastAPI main.py
Add these imports and routes to main-backend-k32m.onrender.com
"""

from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date
import os
import httpx
import stripe
from supabase import create_client

# ── Supabase & Stripe clients ──────────────────────────────────────────────────
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")  # set after creating product
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

gig_router = APIRouter(tags=["GigLedger"])


# ── Pydantic models ────────────────────────────────────────────────────────────
class CreateUserBody(BaseModel):
    email: str

class IncomeEntry(BaseModel):
    user_id: str
    platform: str  # doordash, uber, etsy, upwork, fiverr, lyft, instacart, amazon_flex, other
    amount: float
    date: str  # YYYY-MM-DD
    notes: Optional[str] = None

class ExpenseEntry(BaseModel):
    user_id: str
    category: str  # gas, phone, supplies, food, equipment, other
    amount: float
    date: str
    notes: Optional[str] = None

class AIAdviceBody(BaseModel):
    user_id: str

class CheckoutBody(BaseModel):
    user_id: str
    email: str


# ── Tax calculation helpers ────────────────────────────────────────────────────
def calculate_bracket_tax(taxable_income: float, filing_status: str) -> float:
    if filing_status == "married":
        brackets = [
            (23200, 0.10), (94300, 0.12), (201050, 0.22),
            (383900, 0.24), (487450, 0.32), (731200, 0.35), (float("inf"), 0.37)
        ]
    else:  # single or head_of_household
        brackets = [
            (11600, 0.10), (47150, 0.12), (100525, 0.22),
            (191950, 0.24), (243725, 0.32), (609350, 0.35), (float("inf"), 0.37)
        ]
    tax = 0.0
    prev = 0.0
    for limit, rate in brackets:
        if taxable_income <= prev:
            break
        taxable_at_rate = min(taxable_income, limit) - prev
        tax += taxable_at_rate * rate
        prev = limit
    return tax


def calculate_tax_estimate(annual_net_profit: float, filing_status: str = "single") -> dict:
    if annual_net_profit <= 0:
        return {"annual_estimate": 0, "quarterly_payment": 0, "weekly_set_aside": 0, "effective_rate": 0}

    se_taxable = annual_net_profit * 0.9235
    se_tax = se_taxable * 0.153
    se_deduction = se_tax * 0.5
    agi = annual_net_profit - se_deduction

    standard_deduction = 30000 if filing_status == "married" else 15000
    taxable_income = max(0.0, agi - standard_deduction)
    income_tax = calculate_bracket_tax(taxable_income, filing_status)

    total_tax = se_tax + income_tax
    return {
        "annual_estimate": round(total_tax, 2),
        "quarterly_payment": round(total_tax / 4, 2),
        "weekly_set_aside": round(total_tax / 52, 2),
        "effective_rate": round((total_tax / annual_net_profit * 100), 1),
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

@gig_router.post("/user")
async def create_user(body: CreateUserBody):
    existing = supabase.table("gig_users").select("*").eq("email", body.email).execute()
    if existing.data:
        return existing.data[0]
    result = supabase.table("gig_users").insert({"email": body.email}).execute()
    user = result.data[0]
    # Create default tax settings
    supabase.table("tax_settings").insert({"user_id": user["id"]}).execute()
    return user


@gig_router.get("/user/{email}")
async def get_user(email: str):
    result = supabase.table("gig_users").select("*").eq("email", email).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="User not found")
    user = result.data[0]

    year = datetime.now().year
    income = supabase.table("income_entries").select("amount").eq("user_id", user["id"]).gte("date", f"{year}-01-01").execute()
    expenses = supabase.table("expense_entries").select("amount").eq("user_id", user["id"]).gte("date", f"{year}-01-01").execute()

    total_income = sum(r["amount"] for r in income.data) if income.data else 0
    total_expenses = sum(r["amount"] for r in expenses.data) if expenses.data else 0
    net = total_income - total_expenses

    tax = calculate_tax_estimate(net)
    return {**user, "total_income_ytd": total_income, "total_expenses_ytd": total_expenses, "tax_estimate": tax}


@gig_router.post("/income")
async def add_income(entry: IncomeEntry):
    now = datetime.now()
    year, month = now.year, now.month

    # Free tier: 5 income entries per month
    user = supabase.table("gig_users").select("plan").eq("id", entry.user_id).execute()
    if not user.data:
        raise HTTPException(status_code=404, detail="User not found")

    plan = user.data[0]["plan"]
    if plan == "free":
        month_count = supabase.table("income_entries").select("id", count="exact") \
            .eq("user_id", entry.user_id) \
            .gte("date", f"{year}-{month:02d}-01") \
            .lt("date", f"{year}-{month:02d}-31") \
            .execute()
        if (month_count.count or 0) >= 5:
            raise HTTPException(status_code=403, detail="Free limit: 5 income entries/month. Upgrade to Pro.")

    result = supabase.table("income_entries").insert({
        "user_id": entry.user_id,
        "platform": entry.platform,
        "amount": entry.amount,
        "date": entry.date,
        "notes": entry.notes,
    }).execute()
    return result.data[0]


@gig_router.post("/expense")
async def add_expense(entry: ExpenseEntry):
    user = supabase.table("gig_users").select("plan").eq("id", entry.user_id).execute()
    if not user.data:
        raise HTTPException(status_code=404, detail="User not found")

    if user.data[0]["plan"] == "free":
        raise HTTPException(status_code=403, detail="Expense tracking is a Pro feature. Upgrade to unlock.")

    result = supabase.table("expense_entries").insert({
        "user_id": entry.user_id,
        "category": entry.category,
        "amount": entry.amount,
        "date": entry.date,
        "notes": entry.notes,
    }).execute()
    return result.data[0]


@gig_router.get("/dashboard/{user_id}")
async def get_dashboard(user_id: str):
    now = datetime.now()
    year, month = now.year, now.month
    # Week start (Monday)
    week_start = now.date().strftime("%Y-%m-%d")
    week_day = now.weekday()
    from datetime import timedelta
    week_start = (now.date() - timedelta(days=week_day)).strftime("%Y-%m-%d")

    # Fetch all income
    income_all = supabase.table("income_entries").select("*").eq("user_id", user_id) \
        .gte("date", f"{year}-01-01").order("date", desc=True).execute()
    expenses_all = supabase.table("expense_entries").select("*").eq("user_id", user_id) \
        .gte("date", f"{year}-01-01").order("date", desc=True).execute()

    income_data = income_all.data or []
    expense_data = expenses_all.data or []

    # Aggregations
    total_income_year = sum(r["amount"] for r in income_data)
    total_income_month = sum(r["amount"] for r in income_data if r["date"].startswith(f"{year}-{month:02d}"))
    total_income_week = sum(r["amount"] for r in income_data if r["date"] >= week_start)

    total_expenses_year = sum(r["amount"] for r in expense_data)
    total_expenses_month = sum(r["amount"] for r in expense_data if r["date"].startswith(f"{year}-{month:02d}"))

    net_profit_year = total_income_year - total_expenses_year
    net_profit_month = total_income_month - total_expenses_month

    # Tax settings
    tax_settings = supabase.table("tax_settings").select("*").eq("user_id", user_id).execute()
    filing_status = tax_settings.data[0]["filing_status"] if tax_settings.data else "single"
    tax = calculate_tax_estimate(net_profit_year, filing_status)

    # Platform breakdown
    platform_totals = {}
    for r in income_data:
        p = r["platform"]
        platform_totals[p] = platform_totals.get(p, 0) + r["amount"]
    platform_breakdown = [{"platform": k, "amount": round(v, 2)} for k, v in sorted(platform_totals.items(), key=lambda x: -x[1])]

    # Recent 10 transactions (income + expenses merged and sorted)
    recent = []
    for r in income_data[:20]:
        recent.append({**r, "type": "income"})
    for r in expense_data[:20]:
        recent.append({**r, "type": "expense"})
    recent.sort(key=lambda x: x["date"], reverse=True)
    recent = recent[:10]

    return {
        "income": {
            "week": round(total_income_week, 2),
            "month": round(total_income_month, 2),
            "year": round(total_income_year, 2),
        },
        "expenses": {
            "month": round(total_expenses_month, 2),
            "year": round(total_expenses_year, 2),
        },
        "net_profit": {
            "month": round(net_profit_month, 2),
            "year": round(net_profit_year, 2),
        },
        "tax": tax,
        "set_aside_this_week": round(total_income_week * 0.25, 2),
        "platform_breakdown": platform_breakdown,
        "recent_transactions": recent,
    }


@gig_router.post("/ai-advice")
async def get_ai_advice(body: AIAdviceBody):
    user = supabase.table("gig_users").select("*").eq("id", body.user_id).execute()
    if not user.data:
        raise HTTPException(status_code=404, detail="User not found")

    user_data = user.data[0]
    plan = user_data["plan"]

    # Rate limit free users: 1 tip per week
    if plan == "free":
        # Check if they got advice this week
        from datetime import timedelta
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        # Use a simple check via tax_settings updated_at as a proxy (or add a separate table)
        # For simplicity, store last_advice_at in gig_users — check if it exists
        last_advice = user_data.get("last_advice_at")
        if last_advice:
            last_advice_dt = datetime.fromisoformat(last_advice.replace("Z", ""))
            if (datetime.now() - last_advice_dt).days < 7:
                raise HTTPException(status_code=403, detail="Free users get 1 AI tip per week. Upgrade for unlimited.")

    # Get dashboard data for context
    now = datetime.now()
    year, month = now.year, now.month
    income_month = supabase.table("income_entries").select("amount,platform").eq("user_id", body.user_id).gte("date", f"{year}-{month:02d}-01").execute()
    expenses_month = supabase.table("expense_entries").select("amount").eq("user_id", body.user_id).gte("date", f"{year}-{month:02d}-01").execute()

    total_income = sum(r["amount"] for r in (income_month.data or []))
    total_expenses = sum(r["amount"] for r in (expenses_month.data or []))
    platforms = list({r["platform"] for r in (income_month.data or [])})
    set_aside = total_income * 0.25

    platform_str = ", ".join(platforms) if platforms else "various platforms"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 200,
                "system": "You are a friendly financial advisor specializing in gig economy workers. Give practical, specific, actionable advice in 2-3 sentences. Never give legal advice. Always be encouraging.",
                "messages": [
                    {
                        "role": "user",
                        "content": f"This gig worker made ${total_income:.2f} this month across {platform_str}. Their expenses are ${total_expenses:.2f}. They have set aside ${set_aside:.2f} for taxes. Give them one specific piece of financial advice for this week."
                    }
                ]
            },
            timeout=30,
        )

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="AI advice unavailable")

    advice_text = response.json()["content"][0]["text"]

    # Update last_advice_at
    supabase.table("gig_users").update({"last_advice_at": datetime.now().isoformat()}).eq("id", body.user_id).execute()

    return {"advice": advice_text}


@gig_router.post("/checkout")
async def create_checkout(body: CheckoutBody):
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        customer_email=body.email,
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url="https://gigledger.dealdily.com/dashboard?upgraded=true",
        cancel_url="https://gigledger.dealdily.com/dashboard?canceled=true",
        metadata={"user_id": body.user_id},
    )
    return {"url": session.url}


@gig_router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("metadata", {}).get("user_id")
        customer_id = session.get("customer")
        sub_id = session.get("subscription")
        if user_id:
            supabase.table("gig_users").update({
                "plan": "pro",
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": sub_id,
            }).eq("id", user_id).execute()

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        customer_id = sub.get("customer")
        supabase.table("gig_users").update({"plan": "free", "stripe_subscription_id": None}) \
            .eq("stripe_customer_id", customer_id).execute()

    return {"received": True}


# ── In your main.py, add: ──────────────────────────────────────────────────────
# from gigledger_routes import gig_router
# app.include_router(gig_router)
