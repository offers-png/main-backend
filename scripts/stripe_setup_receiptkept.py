"""
Creates (or finds) the ReceiptKept products and monthly + yearly prices in Stripe and
prints the env vars to paste into Render. Safe to run more than once: prices
are found by lookup_key, so re-running never makes duplicates.

    STRIPE_SECRET_KEY=sk_test_... python scripts/stripe_setup_receiptkept.py

Run it with a TEST key first. When you go live, run it again with the live
key and put the printed live price IDs into Render.
Also creates the referral coupons (one free month on monthly and yearly plans).
"""

import os
import sys

import httpx

KEY = os.getenv("STRIPE_SECRET_KEY", "")
if not KEY:
    sys.exit("Set STRIPE_SECRET_KEY first (use the sk_test_ key first).")

API = "https://api.stripe.com/v1"
H = {"Authorization": f"Bearer {KEY}"}

# (env var, lookup_key, product key, product name, cents, interval)
# Yearly prices go on the same product as the monthly one.
PRICES = [
    ("STRIPE_PRICE_BASIC",                 "rk_basic_monthly",          "basic",          "ReceiptKept Basic",                  599,   "month"),
    ("STRIPE_PRICE_GROWTH",                "rk_growth_monthly",         "growth",         "ReceiptKept Growth",                 1499,  "month"),
    ("STRIPE_PRICE_BUSINESS",              "rk_business_monthly",       "business",       "ReceiptKept Business",               2999,  "month"),
    ("STRIPE_PRICE_STORE_BASIC",           "rk_store_basic_monthly",    "store_basic",    "ReceiptKept Extra Store (Basic)",    499,   "month"),
    ("STRIPE_PRICE_STORE_GROWTH",          "rk_store_growth_monthly",   "store_growth",   "ReceiptKept Extra Store (Growth)",   999,   "month"),
    ("STRIPE_PRICE_STORE_BUSINESS",        "rk_store_business_monthly", "store_business", "ReceiptKept Extra Store (Business)", 1499,  "month"),
    ("STRIPE_PRICE_BASIC_YEARLY",          "rk_basic_yearly",           "basic",          "ReceiptKept Basic",                  4999,  "year"),
    ("STRIPE_PRICE_GROWTH_YEARLY",         "rk_growth_yearly",          "growth",         "ReceiptKept Growth",                 12599, "year"),
    ("STRIPE_PRICE_BUSINESS_YEARLY",       "rk_business_yearly",        "business",       "ReceiptKept Business",               25199, "year"),
    ("STRIPE_PRICE_STORE_BASIC_YEARLY",    "rk_store_basic_yearly",     "store_basic",    "ReceiptKept Extra Store (Basic)",    4199,  "year"),
    ("STRIPE_PRICE_STORE_GROWTH_YEARLY",   "rk_store_growth_yearly",    "store_growth",   "ReceiptKept Extra Store (Growth)",   8399,  "year"),
    ("STRIPE_PRICE_STORE_BUSINESS_YEARLY", "rk_store_business_yearly",  "store_business", "ReceiptKept Extra Store (Business)", 12599, "year"),
]

# Referral reward = one free month. Monthly plans: 100% off the first bill.
# Yearly plans: a month's worth off (100% off a yearly bill would be a free year).
COUPONS = [
    ("referral-1-month-free",     {"percent_off": "100", "duration": "once", "name": "Referral: 1 month free"}),
    ("referral-1-month-basic",    {"amount_off": "599",  "currency": "usd", "duration": "once", "name": "Referral: 1 month free (Basic yearly)"}),
    ("referral-1-month-growth",   {"amount_off": "1499", "currency": "usd", "duration": "once", "name": "Referral: 1 month free (Growth yearly)"}),
    ("referral-1-month-business", {"amount_off": "2999", "currency": "usd", "duration": "once", "name": "Referral: 1 month free (Business yearly)"}),
]


def call(method, path, **data):
    r = httpx.request(method, f"{API}/{path}", headers=H, data=data or None, params=None if data else None, timeout=30)
    body = r.json()
    if r.status_code >= 400:
        sys.exit(f"Stripe error on {path}: {body.get('error', {}).get('message')}")
    return body


def find_price(lookup):
    found = httpx.get(f"{API}/prices", headers=H, params={"lookup_keys[]": lookup, "active": "true"}, timeout=30).json()
    return (found.get("data") or [None])[0]


def main():
    mode = "TEST" if KEY.startswith(("sk_test", "rk_test")) else "LIVE"
    print(f"# Stripe {mode} mode")
    products = {}  # product key -> product id (shared by monthly and yearly)
    for env, lookup, pkey, name, cents, interval in PRICES:
        price = find_price(lookup)
        if price:
            products.setdefault(pkey, price["product"] if isinstance(price["product"], str) else price["product"]["id"])
            if price["unit_amount"] != cents or price["recurring"]["interval"] != interval:
                sys.exit(f"{lookup} exists with a different amount/interval ({price['unit_amount']} / "
                         f"{price['recurring']['interval']}). Prices can't be edited in Stripe: archive it in the "
                         f"dashboard, then run this again.")
        else:
            if pkey not in products:
                monthly = find_price(f"rk_{pkey}_monthly") if interval == "year" else None
                products[pkey] = monthly["product"] if monthly else call(
                    "POST", "products", name=name, **{"metadata[app]": "receiptkept"})["id"]
            price = call(
                "POST", "prices",
                product=products[pkey], currency="usd", unit_amount=str(cents),
                lookup_key=lookup, **{"recurring[interval]": interval, "metadata[app]": "receiptkept"},
            )
        print(f"{env}={price['id']}")

    for cid, params in COUPONS:
        if httpx.get(f"{API}/coupons/{cid}", headers=H, timeout=30).status_code == 404:
            call("POST", "coupons", id=cid, **params)
            print(f"# created coupon {cid}")


if __name__ == "__main__":
    main()
