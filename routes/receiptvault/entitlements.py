"""
ReceiptKept plans and limits — the ONE place they live.

The front end reads all of this from GET /api/billing/plans and never
hard-codes a limit. Billing is per owner account (rv_accounts), not per
store: every store an owner has, and every team member on those stores,
rides on the owner's plan.

`None` in a limit means unlimited.
"""

import os
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException

from routes.receiptvault.stores import REQUEST_TIMEZONE, owned_stores

PLAN_ORDER = ["free", "basic", "growth", "business"]

# Feature keys used across the backend and front end.
#   money_box, mileage, manual_expenses, schedule_c, pnl, csv_export  -> every plan
#   invoices, inventory, tax, accountant, auto_categorize             -> Basic and up
#   email_forwarding, bank_connection, team                           -> Growth and up
_EVERY_PLAN = ["money_box", "mileage", "manual_expenses", "schedule_c", "pnl", "csv_export"]
_BASIC_UP = _EVERY_PLAN + ["invoices", "inventory", "tax", "accountant", "auto_categorize", "statement_upload"]
_GROWTH_UP = _BASIC_UP + ["email_forwarding", "bank_connection", "team"]

PLAN_LIMITS = {
    "free": {
        "name": "Free",
        "price": 0.0,
        "scans_per_month": 10,
        "statement_uploads_per_month": 0,
        "bank_accounts": 0,
        "team_members": 0,
        "included_stores": 1,
        "extra_store_price": None,  # can't add stores on Free
        "features": _EVERY_PLAN,
    },
    "basic": {
        "name": "Basic",
        "price": 5.99,
        "scans_per_month": 50,
        "statement_uploads_per_month": 2,
        "bank_accounts": 0,
        "team_members": 0,
        "included_stores": 1,
        "extra_store_price": 4.99,
        "features": _BASIC_UP,
    },
    "growth": {
        "name": "Growth",
        "price": 14.99,
        "scans_per_month": None,
        "statement_uploads_per_month": None,
        "bank_accounts": 2,
        "team_members": 3,
        "included_stores": 1,
        "extra_store_price": 9.99,
        "features": _GROWTH_UP,
    },
    "business": {
        "name": "Business",
        "price": 29.99,
        "scans_per_month": None,
        "statement_uploads_per_month": None,
        "bank_accounts": None,
        "team_members": None,
        "included_stores": 1,
        "extra_store_price": 14.99,
        "features": _GROWTH_UP,
    },
}

# Plain-English names for upgrade prompts.
FEATURE_LABELS = {
    "money_box": "Money Box",
    "mileage": "the mileage tracker",
    "manual_expenses": "manual expenses",
    "schedule_c": "tax categories",
    "pnl": "Where Your Money Stands",
    "csv_export": "spreadsheet export",
    "invoices": "invoices",
    "inventory": "inventory",
    "tax": "quarterly tax estimates and tax payments",
    "accountant": "sending papers to your accountant",
    "auto_categorize": "auto-sorting, receipt matching and the Personal button",
    "statement_upload": "bank statement upload",
    "email_forwarding": "email receipt forwarding",
    "bank_connection": "bank connection",
    "team": "team members",
}

# Marketing bullets for the plans page / landing page.
PLAN_HIGHLIGHTS = {
    "free": [
        "10 receipt scans a month",
        "Money Box: cash in, card in, money out, checks",
        "Mileage tracker",
        "Tax categories (Schedule C)",
        "See your profit",
        "Spreadsheet (CSV) export",
    ],
    "basic": [
        "50 receipt scans a month",
        "Everything in Free",
        "Invoices and inventory",
        "Quarterly tax estimate and tax payments",
        "Accountant PDF, accountant dropbox and \"Get My Papers\"",
        "Upload 2 bank statements a month",
        "Auto-sort charges, match receipts, Personal button",
        "Extra stores $4.99/mo each",
    ],
    "growth": [
        "Unlimited receipt scans",
        "Everything in Basic",
        "Unlimited bank statement uploads",
        "Forward receipts by email",
        "Connect 2 bank accounts",
        "Team: 3 people with approvals",
        "Extra stores $9.99/mo each",
    ],
    "business": [
        "Unlimited receipt scans",
        "Everything in Growth",
        "Connect unlimited bank accounts",
        "Unlimited team members",
        "Extra stores $14.99/mo each",
    ],
}

# Stripe price IDs come from env vars (test-mode IDs first, live IDs later).
PRICE_ENV = {
    "basic": "STRIPE_PRICE_BASIC",
    "growth": "STRIPE_PRICE_GROWTH",
    "business": "STRIPE_PRICE_BUSINESS",
}
ADDON_PRICE_ENV = {
    "basic": "STRIPE_PRICE_STORE_BASIC",
    "growth": "STRIPE_PRICE_STORE_GROWTH",
    "business": "STRIPE_PRICE_STORE_BUSINESS",
}
DEFAULT_TIMEZONE = "America/New_York"


def plan_price_id(plan: str) -> Optional[str]:
    env = PRICE_ENV.get(plan)
    return os.getenv(env) if env else None


def addon_price_id(plan: str) -> Optional[str]:
    env = ADDON_PRICE_ENV.get(plan)
    return os.getenv(env) if env else None


def plan_for_price(price_id: Optional[str]) -> Optional[str]:
    for plan in PRICE_ENV:
        if price_id and plan_price_id(plan) == price_id:
            return plan
    return None


def is_addon_price(price_id: Optional[str]) -> bool:
    return bool(price_id) and any(addon_price_id(p) == price_id for p in ADDON_PRICE_ENV)


def cheapest_plan_with(feature: str) -> str:
    for plan in PLAN_ORDER:
        if feature in PLAN_LIMITS[plan]["features"]:
            return plan
    return "business"


def next_plan_for_limit(current: str, limit_key: str, needed: int) -> Optional[str]:
    """Cheapest plan above `current` whose `limit_key` allows `needed`."""
    start = PLAN_ORDER.index(current) + 1 if current in PLAN_ORDER else 0
    for plan in PLAN_ORDER[start:]:
        lim = PLAN_LIMITS[plan][limit_key]
        if lim is None or lim >= needed:
            return plan
    return None


def public_plans() -> list:
    out = []
    for plan in PLAN_ORDER:
        p = PLAN_LIMITS[plan]
        out.append({
            "plan": plan,
            "name": p["name"],
            "price": p["price"],
            "limits": {
                "scansPerMonth": p["scans_per_month"],
                "statementUploadsPerMonth": p["statement_uploads_per_month"],
                "bankAccounts": p["bank_accounts"],
                "teamMembers": p["team_members"],
                "includedStores": p["included_stores"],
            },
            "extraStorePrice": p["extra_store_price"],
            "features": list(p["features"]),
            "highlights": PLAN_HIGHLIGHTS[plan],
            "available": plan == "free" or bool(plan_price_id(plan)),
        })
    return out


# ── Accounts ────────────────────────────────────────────────────────────────

def get_account(supabase, owner_user_id: str) -> dict:
    """The owner's billing account; created on Free the first time we see them."""
    rows = supabase.table("rv_accounts").select("*").eq("owner_user_id", owner_user_id).execute()
    if rows.data:
        acct = rows.data[0]
    else:
        acct = {"owner_user_id": owner_user_id, "plan": "free", "extra_stores": 0}
        try:
            ins = supabase.table("rv_accounts").insert(acct).execute()
            if ins.data:
                acct = ins.data[0]
        except Exception:
            # created concurrently by another request
            again = supabase.table("rv_accounts").select("*").eq("owner_user_id", owner_user_id).execute()
            if again.data:
                acct = again.data[0]
    if acct.get("plan") not in PLAN_LIMITS:
        acct["plan"] = "free"
    return acct


def account_for_business(supabase, business: dict) -> dict:
    return get_account(supabase, str(business["user_id"]))


def update_account(supabase, owner_user_id: str, data: dict):
    get_account(supabase, owner_user_id)  # make sure the row exists
    data = dict(data)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    supabase.table("rv_accounts").update(data).eq("owner_user_id", owner_user_id).execute()


def limits_for(account: dict) -> dict:
    return PLAN_LIMITS[account.get("plan") or "free"]


def has_feature(account: dict, feature: str) -> bool:
    return feature in limits_for(account)["features"]


def upgrade_error(code: str, message: str, feature: Optional[str] = None, required_plan: Optional[str] = None, is_owner: bool = True) -> HTTPException:
    detail = {"code": code, "message": message, "isOwner": is_owner}
    if feature:
        detail["feature"] = feature
    if required_plan:
        detail["requiredPlan"] = required_plan
        detail["requiredPlanName"] = PLAN_LIMITS[required_plan]["name"]
        detail["requiredPlanPrice"] = PLAN_LIMITS[required_plan]["price"]
    return HTTPException(status_code=402, detail=detail)


def require_feature(supabase, business: dict, feature: str, role: str = "owner"):
    account = account_for_business(supabase, business)
    if has_feature(account, feature):
        return account
    plan = cheapest_plan_with(feature)
    p = PLAN_LIMITS[plan]
    label = FEATURE_LABELS.get(feature, feature)
    if role == "owner":
        msg = f"Upgrade to {p['name']} for ${p['price']:.2f}/month to use {label}."
    else:
        msg = f"This store's plan doesn't include {label}. Ask the owner to upgrade to {p['name']}."
    raise upgrade_error("upgrade_required", msg, feature=feature, required_plan=plan, is_owner=role == "owner")


# ── Monthly usage (counted from existing rows) ──────────────────────────────

def account_timezone(account: Optional[dict]) -> str:
    for tz in (REQUEST_TIMEZONE.get(), (account or {}).get("timezone"), DEFAULT_TIMEZONE):
        if tz:
            try:
                ZoneInfo(tz)
                return tz
            except Exception:
                continue
    return DEFAULT_TIMEZONE


def month_start_utc(tz_name: str, now: Optional[datetime] = None) -> str:
    """The 1st of this month at midnight in the owner's time zone, as a
    naive UTC ISO string (receipts.uploaded_at is stored as naive UTC)."""
    tz = ZoneInfo(tz_name)
    local_now = (now or datetime.now(timezone.utc)).astimezone(tz)
    local_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def owner_store_ids(supabase, owner_user_id: str) -> list:
    return [b["id"] for b in owned_stores(supabase, owner_user_id)]


def count_scans_this_month(supabase, owner_user_id: str, account: Optional[dict] = None) -> int:
    ids = owner_store_ids(supabase, owner_user_id)
    if not ids:
        return 0
    since = month_start_utc(account_timezone(account))
    rows = (
        supabase.table("receipts").select("id", count="exact")
        .in_("business_id", ids).gte("uploaded_at", since)
        .execute()
    )
    return rows.count if rows.count is not None else len(rows.data or [])


def count_statement_uploads_this_month(supabase, owner_user_id: str, account: Optional[dict] = None) -> int:
    ids = owner_store_ids(supabase, owner_user_id)
    if not ids:
        return 0
    since = month_start_utc(account_timezone(account))
    try:
        rows = (
            supabase.table("rv_statement_uploads").select("id", count="exact")
            .in_("business_id", ids).eq("status", "imported").gte("imported_at", since)
            .execute()
        )
    except Exception:
        return 0
    return rows.count if rows.count is not None else len(rows.data or [])


def count_bank_accounts(supabase, owner_user_id: str) -> int:
    ids = owner_store_ids(supabase, owner_user_id)
    if not ids:
        return 0
    try:
        rows = (
            supabase.table("rv_bank_accounts").select("id", count="exact")
            .in_("business_id", ids).eq("status", "active").eq("source", "stripe")
            .execute()
        )
    except Exception:
        return 0
    return rows.count if rows.count is not None else len(rows.data or [])


def count_team_members(supabase, owner_user_id: str) -> int:
    ids = owner_store_ids(supabase, owner_user_id)
    if not ids:
        return 0
    rows = (
        supabase.table("business_users").select("id", count="exact")
        .in_("business_id", ids).neq("status", "removed")
        .execute()
    )
    return rows.count if rows.count is not None else len(rows.data or [])


def _limit_check(account: dict, limit_key: str, used: int, what: str, upsell_fmt: str, role: str):
    lim = limits_for(account)[limit_key]
    if lim is None or used < lim:
        return
    current = account.get("plan") or "free"
    nxt = next_plan_for_limit(current, limit_key, used + 1)
    if role != "owner":
        raise upgrade_error("limit_reached", f"This store has used all its {what} for this plan. Ask the owner to upgrade.", required_plan=nxt, is_owner=False)
    if nxt:
        p = PLAN_LIMITS[nxt]
        msg = upsell_fmt.format(lim=lim, plan=p["name"], price=f"${p['price']:.2f}")
    else:
        msg = f"You've reached the limit of {lim} {what} on your plan."
    raise upgrade_error("limit_reached", msg, required_plan=nxt)


def check_scan_allowed(supabase, business: dict, role: str = "owner"):
    account = account_for_business(supabase, business)
    used = count_scans_this_month(supabase, str(business["user_id"]), account)
    plan = account.get("plan") or "free"
    free_word = "free " if plan == "free" else ""
    _limit_check(
        account, "scans_per_month", used, "receipt scans",
        "You've used your {lim} " + free_word + "scans this month. Upgrade to {plan} for {price} to keep scanning.",
        role,
    )


def check_statement_upload_allowed(supabase, business: dict, role: str = "owner"):
    account = require_feature(supabase, business, "statement_upload", role)
    used = count_statement_uploads_this_month(supabase, str(business["user_id"]), account)
    _limit_check(
        account, "statement_uploads_per_month", used, "statement uploads",
        "You've uploaded your {lim} statements for this month. Upgrade to {plan} for {price} for unlimited uploads.",
        role,
    )


def check_bank_account_allowed(supabase, business: dict, role: str = "owner"):
    account = require_feature(supabase, business, "bank_connection", role)
    used = count_bank_accounts(supabase, str(business["user_id"]))
    _limit_check(
        account, "bank_accounts", used, "connected bank accounts",
        "Your plan connects {lim} bank accounts. Upgrade to {plan} for {price} to connect more.",
        role,
    )


def check_team_member_allowed(supabase, business: dict, role: str = "owner"):
    account = require_feature(supabase, business, "team", role)
    used = count_team_members(supabase, str(business["user_id"]))
    _limit_check(
        account, "team_members", used, "team members",
        "Your plan includes {lim} team members. Upgrade to {plan} for {price} to add more.",
        role,
    )


# ── Stores: add-ons and read-only extras ────────────────────────────────────

def allowed_store_count(account: dict) -> int:
    return limits_for(account)["included_stores"] + int(account.get("extra_stores") or 0)


def active_owned_stores(supabase, owner_user_id: str) -> list:
    return [b for b in owned_stores(supabase, owner_user_id) if not b.get("archived_at")]


def read_only_store_ids(supabase, owner_user_id: str, account: Optional[dict] = None) -> list:
    """Stores past what the plan pays for (e.g. 3 stores after dropping to
    Free). Oldest stores stay editable; the newest extras go view-only.
    Nothing is ever deleted. Archived stores are always view-only."""
    account = account or get_account(supabase, owner_user_id)
    stores = owned_stores(supabase, owner_user_id)
    allowed = allowed_store_count(account)
    out, kept = [], 0
    for b in stores:
        if b.get("archived_at"):
            out.append(b["id"])
        elif kept < allowed:
            kept += 1
        else:
            out.append(b["id"])
    return out


def check_store_writable(supabase, business: dict, role: str = "owner"):
    owner = str(business["user_id"])
    account = get_account(supabase, owner)
    if business["id"] not in read_only_store_ids(supabase, owner, account):
        return
    if business.get("archived_at"):
        msg = "This store is archived, so it's view-only. Restore it in Settings to make changes."
    elif role == "owner":
        nxt = account.get("plan") if account.get("plan") != "free" else "basic"
        p = PLAN_LIMITS[nxt]
        n = allowed_store_count(account)
        msg = (
            f"Your plan pays for {n} store{'' if n == 1 else 's'}, so this one is view-only. "
            f"Add it to your plan (${p['extra_store_price']:.2f}/month on {p['name']}) or archive an extra store in Settings."
        )
    else:
        msg = "This store is view-only right now. Ask the owner to update their plan."
    raise HTTPException(status_code=402, detail={"code": "store_read_only", "message": msg, "isOwner": role == "owner", "requiredPlan": "basic", "requiredPlanName": "Basic"})


def check_can_add_store(supabase, owner_user_id: str, owned_count: int):
    account = get_account(supabase, owner_user_id)
    plan = account.get("plan") or "free"
    p = PLAN_LIMITS[plan]
    if p["extra_store_price"] is None:
        b = PLAN_LIMITS["basic"]
        raise upgrade_error(
            "upgrade_required",
            f"The Free plan includes 1 store. Upgrade to Basic for ${b['price']:.2f}/month, then add more stores for ${b['extra_store_price']:.2f}/month each.",
            feature="extra_stores", required_plan="basic",
        )
    if not account.get("stripe_subscription_id"):
        raise upgrade_error("upgrade_required", "Finish setting up your plan before adding another store.", required_plan=plan)


# ── Stripe ──────────────────────────────────────────────────────────────────

def stripe_key() -> str:
    return os.getenv("STRIPE_SECRET_KEY", "")


async def stripe_request(method: str, path: str, data: Optional[dict] = None) -> dict:
    if not stripe_key():
        raise HTTPException(status_code=503, detail="Billing isn't set up yet. Please try again later.")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.request(
            method,
            f"https://api.stripe.com/v1/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {stripe_key()}", "Content-Type": "application/x-www-form-urlencoded"},
            data=data,
        )
    try:
        body = r.json()
    except Exception:
        body = {}
    if r.status_code >= 400 or (isinstance(body, dict) and body.get("error")):
        msg = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
        print(f"[stripe] {method} {path} failed {r.status_code}: {str(body)[:300]}")
        raise HTTPException(status_code=502, detail=msg or "Billing request failed. Please try again.")
    return body


def _addon_item(subscription: dict) -> Optional[dict]:
    for item in (subscription.get("items") or {}).get("data", []):
        if is_addon_price((item.get("price") or {}).get("id")):
            return item
    return None


async def sync_store_addon_quantity(supabase, owner_user_id: str):
    """Bill extra stores as a second subscription item: quantity = active
    stores beyond the included one, priced at the owner's plan's add-on."""
    account = get_account(supabase, owner_user_id)
    plan = account.get("plan") or "free"
    sub_id = account.get("stripe_subscription_id")
    extra = max(0, len(active_owned_stores(supabase, owner_user_id)) - PLAN_LIMITS[plan]["included_stores"])
    if plan == "free" or not sub_id:
        return
    price = addon_price_id(plan)
    if not price:
        raise HTTPException(status_code=503, detail="Extra-store billing isn't set up yet. Please try again later.")
    sub = await stripe_request("GET", f"subscriptions/{sub_id}")
    item = _addon_item(sub)
    if extra > 0 and item:
        await stripe_request("POST", f"subscription_items/{item['id']}", {"quantity": str(extra), "price": price, "proration_behavior": "create_prorations"})
    elif extra > 0:
        await stripe_request("POST", "subscription_items", {"subscription": sub_id, "price": price, "quantity": str(extra), "proration_behavior": "create_prorations"})
    elif item:
        await stripe_request("DELETE", f"subscription_items/{item['id']}?proration_behavior=create_prorations")
    # Webhook confirms this later; set it now so the new store is usable at once.
    update_account(supabase, owner_user_id, {"extra_stores": extra})


def apply_subscription(supabase, subscription: dict, owner_user_id: Optional[str] = None):
    """Mirror a Stripe subscription onto rv_accounts (webhook handler)."""
    customer = subscription.get("customer")
    owner = owner_user_id or (subscription.get("metadata") or {}).get("owner_user_id")
    if not owner and customer:
        rows = supabase.table("rv_accounts").select("owner_user_id").eq("stripe_customer_id", customer).execute()
        if rows.data:
            owner = rows.data[0]["owner_user_id"]
    if not owner:
        print(f"[billing] subscription {subscription.get('id')} has no matching account")
        return None
    # A deleted account (or another app's customer): don't recreate anything
    known = supabase.table("rv_accounts").select("owner_user_id").eq("owner_user_id", owner).execute().data
    if not known and not owned_stores(supabase, owner):
        print(f"[billing] subscription {subscription.get('id')} is for an account that no longer exists — ignored")
        return None

    status = subscription.get("status")
    plan, extra = None, 0
    for item in (subscription.get("items") or {}).get("data", []):
        pid = (item.get("price") or {}).get("id")
        if plan_for_price(pid):
            plan = plan_for_price(pid)
        elif is_addon_price(pid):
            extra = int(item.get("quantity") or 0)

    live = status in ("active", "trialing", "past_due")
    data = {
        "stripe_customer_id": customer,
        "subscription_status": status,
    }
    if live and plan:
        data.update({"plan": plan, "extra_stores": extra, "stripe_subscription_id": subscription.get("id")})
    elif not live:
        # Ended — back to Free. Data is never deleted; extra stores go view-only.
        data.update({"plan": "free", "extra_stores": 0, "stripe_subscription_id": None})
    if subscription.get("current_period_end"):
        data["current_period_end"] = datetime.fromtimestamp(int(subscription["current_period_end"]), timezone.utc).isoformat()
    update_account(supabase, owner, data)
    return owner
