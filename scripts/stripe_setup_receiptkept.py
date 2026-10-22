"""
Creates (or finds) the ReceiptKept products and monthly prices in Stripe and
prints the env vars to paste into Render. Safe to run more than once: prices
are found by lookup_key, so re-running never makes duplicates.

    STRIPE_SECRET_KEY=sk_test_... python scripts/stripe_setup_receiptkept.py

Run it with a TEST key first. When you go live, run it again with the live
key and put the printed live price IDs into Render.
Also creates the "referral-1-month-free" coupon used by referrals.
"""

import os
import sys

import httpx

KEY = os.getenv("STRIPE_SECRET_KEY", "")
if not KEY:
    sys.exit("Set STRIPE_SECRET_KEY first (use the sk_test_ key first).")

API = "https://api.stripe.com/v1"
H = {"Authorization": f"Bearer {KEY}"}

PRICES = [
    # env var,                    lookup_key,                  product name,                     cents
    ("STRIPE_PRICE_BASIC",          "rk_basic_monthly",          "ReceiptKept Basic",              599),
    ("STRIPE_PRICE_GROWTH",         "rk_growth_monthly",         "ReceiptKept Growth",             1499),
    ("STRIPE_PRICE_BUSINESS",       "rk_business_monthly",       "ReceiptKept Business",           2999),
    ("STRIPE_PRICE_STORE_BASIC",    "rk_store_basic_monthly",    "ReceiptKept Extra Store (Basic)",    499),
    ("STRIPE_PRICE_STORE_GROWTH",   "rk_store_growth_monthly",   "ReceiptKept Extra Store (Growth)",   999),
    ("STRIPE_PRICE_STORE_BUSINESS", "rk_store_business_monthly", "ReceiptKept Extra Store (Business)", 1499),
]


def call(method, path, **data):
    r = httpx.request(method, f"{API}/{path}", headers=H, data=data or None, params=None if data else None, timeout=30)
    body = r.json()
    if r.status_code >= 400:
        sys.exit(f"Stripe error on {path}: {body.get('error', {}).get('message')}")
    return body


def main():
    mode = "TEST" if KEY.startswith(("sk_test", "rk_test")) else "LIVE"
    print(f"# Stripe {mode} mode")
    for env, lookup, name, cents in PRICES:
        found = httpx.get(f"{API}/prices", headers=H, params={"lookup_keys[]": lookup, "active": "true"}, timeout=30).json()
        if found.get("data"):
            price = found["data"][0]
        else:
            product = call("POST", "products", name=name, **{"metadata[app]": "receiptkept"})
            price = call(
                "POST", "prices",
                product=product["id"], currency="usd", unit_amount=str(cents),
                lookup_key=lookup, **{"recurring[interval]": "month", "metadata[app]": "receiptkept"},
            )
        print(f"{env}={price['id']}")

    coupon = httpx.get(f"{API}/coupons/referral-1-month-free", headers=H, timeout=30)
    if coupon.status_code == 404:
        call("POST", "coupons", id="referral-1-month-free", percent_off="100", duration="once", name="Referral: 1 month free")
        print("# created coupon referral-1-month-free")


if __name__ == "__main__":
    main()
