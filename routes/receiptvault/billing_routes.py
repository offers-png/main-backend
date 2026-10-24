"""
ReceiptKept billing — per owner account, four plans (Free / Basic / Growth /
Business) plus a per-extra-store add-on. Limits live in entitlements.py.

  GET  /api/billing/plans     public plan list (the front end's only source of limits)
  GET  /api/billing/status    the active store's account: plan, usage, view-only stores
  POST /api/billing/checkout  {plan} — new subscription (Checkout) or plan change
  POST /api/billing/portal    Stripe customer portal (card, invoices, cancel)
  POST /api/billing/webhook   Stripe events
"""

import hashlib
import hmac
import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from routes.receiptvault.routes import get_current_user, get_supabase as _rv_get_supabase
from routes.receiptvault.stores import resolve_store, owned_stores
from routes.receiptvault import entitlements as ent

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://receiptkept.com")
WEBHOOK_TOLERANCE_SECONDS = 300

billing_routes = APIRouter(prefix="/api", tags=["billing"])


def get_supabase():
    return _rv_get_supabase()


def webhook_secrets() -> list:
    """Signing secrets to accept. Whitespace is trimmed (a pasted trailing
    space or newline would otherwise reject every event) and both env var
    names are tried."""
    out = []
    for name in ("STRIPE_WEBHOOK_SECRET_RECEIPTKEPT", "STRIPE_WEBHOOK_SECRET_ReceiptVault"):
        v = (os.getenv(name) or "").strip().strip('"').strip("'")
        if v and v not in out:
            out.append(v)
    return out


def _owner_account_or_403(supabase, current_user):
    business, role, _m = resolve_store(supabase, current_user.user.id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only the store owner can change the plan.")
    return business, ent.get_account(supabase, current_user.user.id)


@billing_routes.get("/billing/plans")
async def get_plans():
    return ent.public_plans()


@billing_routes.get("/billing/status")
async def billing_status(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    business, role, _m = resolve_store(supabase, current_user.user.id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    owner = str(business["user_id"])
    account = ent.get_account(supabase, owner)

    # Remember the owner's time zone for monthly counters
    tz = ent.REQUEST_TIMEZONE.get()
    if role == "owner" and tz and tz != account.get("timezone"):
        try:
            ent.update_account(supabase, owner, {"timezone": ent.account_timezone(None)})
        except Exception as e:
            print(f"[billing] couldn't save timezone: {e}")

    plan = account.get("plan") or "free"
    limits = ent.limits_for(account)
    comp = bool(account.get("is_comp"))
    interval = account.get("billing_interval") or "month"
    read_only = ent.read_only_store_ids(supabase, owner, account)
    stores = owned_stores(supabase, owner)
    return {
        "plan": plan,
        "planName": limits["name"],
        "isComp": comp,  # lifetime plan: free forever, no limits, no upgrade prompts
        "price": 0.0 if comp else ent.plan_price(plan, interval),
        "interval": interval,
        "features": list(limits["features"]),
        "limits": {
            "scansPerMonth": limits["scans_per_month"],
            "statementUploadsPerMonth": limits["statement_uploads_per_month"],
            "bankAccounts": limits["bank_accounts"],
            "teamMembers": limits["team_members"],
            "includedStores": None if comp else limits["included_stores"],
        },
        "usage": {
            "scansThisMonth": ent.count_scans_this_month(supabase, owner, account),
            "statementUploadsThisMonth": ent.count_statement_uploads_this_month(supabase, owner, account),
            "bankAccounts": ent.count_bank_accounts(supabase, owner),
            "teamMembers": ent.count_team_members(supabase, owner),
            "stores": len([s for s in stores if not s.get("archived_at")]),
        },
        "extraStorePrice": None if comp else ent.extra_store_price(plan, interval),
        "extraStores": int(account.get("extra_stores") or 0),
        "readOnlyStoreIds": read_only,
        "activeStoreReadOnly": business["id"] in read_only,
        "isOwner": role == "owner",
        "hasSubscription": bool(account.get("stripe_subscription_id")),
        "subscriptionStatus": account.get("subscription_status"),
        "paymentPastDue": account.get("subscription_status") == "past_due",
        "currentPeriodEnd": account.get("current_period_end"),
        # kept for older app builds that still read these
        "is_active": True,
        "in_trial": False,
        "trial_ends_at": None,
    }


class CheckoutBody(BaseModel):
    plan: str
    interval: str = "month"  # "month" | "year"


async def _ensure_customer(supabase, account: dict, current_user) -> str:
    customer_id = account.get("stripe_customer_id")
    if customer_id:
        return customer_id
    customer = await ent.stripe_request("POST", "customers", {
        "email": current_user.user.email or "",
        "metadata[owner_user_id]": current_user.user.id,
        "metadata[app]": "receiptkept",
    })
    ent.update_account(supabase, current_user.user.id, {"stripe_customer_id": customer["id"]})
    return customer["id"]


def _pending_referral_reward(supabase, owner_user_id: str):
    ids = [b["id"] for b in owned_stores(supabase, owner_user_id)]
    if not ids:
        return None
    rows = (
        supabase.table("referrals").select("*")
        .in_("referrer_business_id", ids).eq("reward_type", "pending_coupon")
        .execute()
    )
    pending = [r for r in (rows.data or []) if not r.get("reward_applied")]
    return pending[0] if pending else None


@billing_routes.post("/billing/checkout")
async def create_checkout(body: CheckoutBody, current_user=Depends(get_current_user)):
    """Free -> paid opens Stripe Checkout. Paid -> another paid plan changes
    the existing subscription in place (with proration), including moving
    the extra-store add-on to the new plan's add-on price."""
    if body.plan not in ("basic", "growth", "business"):
        raise HTTPException(status_code=400, detail="Pick Basic, Growth or Business.")
    if body.interval not in ent.INTERVALS:
        raise HTTPException(status_code=400, detail="Pick monthly or yearly.")
    interval = body.interval
    price = ent.plan_price_id(body.plan, interval)
    addon_price = ent.addon_price_id(body.plan, interval)
    if not price or not addon_price:
        what = "Yearly billing" if interval == "year" else "That plan"
        raise HTTPException(status_code=503, detail=f"{what} isn't available yet. Please try again later.")

    supabase = get_supabase()
    _business, account = _owner_account_or_403(supabase, current_user)
    if account.get("is_comp"):
        raise HTTPException(status_code=400, detail=f"You have {account['plan'].title()} for life, so there's nothing to buy.")
    owner = current_user.user.id
    extra = max(0, len(ent.active_owned_stores(supabase, owner)) - 1)

    sub_id = account.get("stripe_subscription_id")
    if sub_id and account.get("plan") != "free":
        sub = await ent.stripe_request("GET", f"subscriptions/{sub_id}")
        # Every item moves to the same billing period (Stripe requires it).
        # The difference is billed NOW (always_invoice) — otherwise an upgrade on a
        # yearly plan wouldn't be paid for until next year. Downgrades become a
        # credit on the next bill. pending_if_incomplete: if the card is declined,
        # the subscription stays as it was instead of unlocking the bigger plan.
        data = {"proration_behavior": "always_invoice", "payment_behavior": "pending_if_incomplete"}
        i = 0
        addon_done = False
        for item in (sub.get("items") or {}).get("data", []):
            pid = (item.get("price") or {}).get("id")
            if ent.plan_for_price(pid):
                data[f"items[{i}][id]"] = item["id"]
                data[f"items[{i}][price]"] = price
                i += 1
            elif ent.is_addon_price(pid):
                data[f"items[{i}][id]"] = item["id"]
                if extra > 0:
                    data[f"items[{i}][price]"] = addon_price
                    data[f"items[{i}][quantity]"] = str(extra)
                else:
                    data[f"items[{i}][deleted]"] = "true"
                addon_done = True
                i += 1
        if extra > 0 and not addon_done:
            data[f"items[{i}][price]"] = addon_price
            data[f"items[{i}][quantity]"] = str(extra)
        updated = await ent.stripe_request("POST", f"subscriptions/{sub_id}", data)
        ent.apply_subscription(supabase, updated, owner)
        return {"updated": True, "plan": body.plan, "interval": interval}

    customer_id = await _ensure_customer(supabase, account, current_user)
    data = {
        "customer": customer_id,
        "mode": "subscription",
        "line_items[0][price]": price,
        "line_items[0][quantity]": "1",
        "success_url": f"{FRONTEND_URL}/plans?checkout=success",
        "cancel_url": f"{FRONTEND_URL}/plans?checkout=canceled",
        "client_reference_id": owner,
        "metadata[owner_user_id]": owner,
        "metadata[plan]": body.plan,
        "metadata[interval]": interval,
        # Lets other apps' webhooks on this Stripe account skip ReceiptKept sessions
        "metadata[app]": "receiptkept",
        "subscription_data[metadata][app]": "receiptkept",
        "subscription_data[metadata][owner_user_id]": owner,
        "subscription_data[metadata][plan]": body.plan,
        "allow_promotion_codes": "true",
    }
    if extra > 0:
        data["line_items[1][price]"] = addon_price
        data["line_items[1][quantity]"] = str(extra)
    reward = _pending_referral_reward(supabase, owner)
    if reward:
        # Stripe allows promotion codes OR discounts, not both
        data.pop("allow_promotion_codes")
        data["discounts[0][coupon]"] = ent.referral_coupon(body.plan, interval)
        data["metadata[referral_id]"] = reward["id"]
    session = await ent.stripe_request("POST", "checkout/sessions", data)
    return {"url": session["url"]}


@billing_routes.post("/billing/portal")
async def billing_portal(current_user=Depends(get_current_user)):
    supabase = get_supabase()
    _business, account = _owner_account_or_403(supabase, current_user)
    if not account.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="You're on the Free plan, so there's no bill to manage yet.")
    portal = await ent.stripe_request("POST", "billing_portal/sessions", {
        "customer": account["stripe_customer_id"],
        "return_url": f"{FRONTEND_URL}/plans",
    })
    return {"url": portal["url"]}


def verify_stripe_signature(payload: bytes, sig_header: str, secret: str, now: float = None) -> bool:
    try:
        parts = {}
        for part in (sig_header or "").split(","):
            k, v = part.split("=", 1)
            parts.setdefault(k.strip(), []).append(v.strip())
        ts = int(parts.get("t", ["0"])[0])
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > WEBHOOK_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v1) for v1 in parts.get("v1", []))


@billing_routes.post("/billing/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    secrets_ = webhook_secrets()
    if not secrets_:
        print("[billing] webhook secret not configured — rejecting event")
        raise HTTPException(status_code=500, detail="Webhook not configured")
    sig = request.headers.get("stripe-signature", "")
    if not any(verify_stripe_signature(payload, sig, s) for s in secrets_):
        print(f"[billing] invalid webhook signature (checked {len(secrets_)} configured secret(s))")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event = json.loads(payload)
    supabase = get_supabase()
    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed" and obj.get("mode") == "subscription":
        owner = (obj.get("metadata") or {}).get("owner_user_id") or obj.get("client_reference_id")
        if owner:
            ent.update_account(supabase, owner, {
                "stripe_customer_id": obj.get("customer"),
                "stripe_subscription_id": obj.get("subscription"),
            })
            referral_id = (obj.get("metadata") or {}).get("referral_id")
            if referral_id:
                supabase.table("referrals").update({"reward_applied": True, "reward_type": "stripe_coupon"}).eq("id", referral_id).execute()
            if obj.get("subscription"):
                try:
                    sub = await ent.stripe_request("GET", f"subscriptions/{obj['subscription']}")
                    ent.apply_subscription(supabase, sub, owner)
                except HTTPException as e:
                    print(f"[billing] couldn't load subscription after checkout: {e.detail}")

    elif etype in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        if etype == "customer.subscription.deleted":
            obj = dict(obj, status="canceled")
        ent.apply_subscription(supabase, obj)

    elif etype in ("invoice.paid", "invoice.payment_succeeded"):
        customer = obj.get("customer")
        if customer:
            supabase.table("rv_accounts").update({"subscription_status": "active"}).eq("stripe_customer_id", customer).execute()

    elif etype == "invoice.payment_failed":
        customer = obj.get("customer")
        if customer:
            # Stripe retries per its settings; just flag it so the app can warn.
            supabase.table("rv_accounts").update({"subscription_status": "past_due"}).eq("stripe_customer_id", customer).execute()

    elif etype == "financial_connections.account.refreshed_transactions":
        try:
            from routes.receiptvault.connect_routes import handle_refreshed_transactions
            await handle_refreshed_transactions(supabase, obj)
        except ModuleNotFoundError:
            pass

    return {"ok": True}
