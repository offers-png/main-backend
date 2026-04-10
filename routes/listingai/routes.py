import os
import json
import anthropic
import stripe
from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel
from supabase import create_client, Client

listingai_routes = APIRouter()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
STRIPE_PRICE_ID = os.environ["STRIPE_PRICE_ID"]

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

FREE_GENERATION_LIMIT = 3


class GenerateRequest(BaseModel):
    email: str
    property_details: str

class CreateCheckoutRequest(BaseModel):
    email: str
    success_url: str
    cancel_url: str

class UserCreate(BaseModel):
    email: str


def get_or_create_user(email: str) -> dict:
    res = supabase.table("listingai_users").select("*").eq("email", email).execute()
    if res.data:
        return res.data[0]
    new_user = supabase.table("listingai_users").insert({"email": email}).execute()
    return new_user.data[0]


@listingai_routes.post("/user")
def create_user(body: UserCreate):
    return get_or_create_user(body.email)


@listingai_routes.get("/user/{email}")
def get_user(email: str):
    res = supabase.table("listingai_users").select("*").eq("email", email).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="User not found")
    return res.data[0]


@listingai_routes.post("/generate")
def generate(body: GenerateRequest):
    user = get_or_create_user(body.email)

    if user["plan"] == "free" and user["generations_used"] >= FREE_GENERATION_LIMIT:
        raise HTTPException(
            status_code=402,
            detail="Free limit reached. Please upgrade to Pro for unlimited generations."
        )

    try:
        message = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system="You are an expert real estate copywriter with 20 years experience. You write compelling, accurate property descriptions that sell homes fast.",
            messages=[{
                "role": "user",
                "content": (
                    'Generate three pieces of content for this property. '
                    'Return ONLY valid JSON, no markdown, no backticks. '
                    'Format: {"mls": "...", "social": "...", "email": "..."}\n\n'
                    f"Property details: {body.property_details}"
                )
            }]
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Claude API error: {str(e)}")

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        outputs = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse Claude response as JSON")

    supabase.table("listingai_generations").insert({
        "user_id": user["id"],
        "property_input": body.property_details,
        "mls_output": outputs.get("mls", ""),
        "social_output": outputs.get("social", ""),
        "email_output": outputs.get("email", ""),
    }).execute()

    supabase.table("listingai_users").update({
        "generations_used": user["generations_used"] + 1
    }).eq("id", user["id"]).execute()

    return {
        "mls": outputs.get("mls", ""),
        "social": outputs.get("social", ""),
        "email": outputs.get("email", ""),
        "generations_used": user["generations_used"] + 1,
        "plan": user["plan"],
    }


@listingai_routes.post("/create-checkout-session")
def create_checkout_session(body: CreateCheckoutRequest):
    user = get_or_create_user(body.email)

    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        customer = stripe.Customer.create(email=body.email)
        customer_id = customer.id
        supabase.table("listingai_users").update({
            "stripe_customer_id": customer_id
        }).eq("id", user["id"]).execute()

    session = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        mode="subscription",
        success_url=body.success_url,
        cancel_url=body.cancel_url,
        metadata={"email": body.email},
    )
    return {"url": session.url}


@listingai_routes.post("/webhook")
async def listingai_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        email = session.get("metadata", {}).get("email")
        subscription_id = session.get("subscription")
        if email:
            supabase.table("listingai_users").update({
                "plan": "pro",
                "stripe_subscription_id": subscription_id,
            }).eq("email", email).execute()

    elif event["type"] == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        supabase.table("listingai_users").update({
            "plan": "free"
        }).eq("stripe_subscription_id", subscription["id"]).execute()

    return {"received": True}
