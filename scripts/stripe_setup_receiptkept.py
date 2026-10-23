"""
Creates (or finds) the ReceiptKept products, monthly + yearly prices and
referral coupons in Stripe, and prints the env vars to put on Render.

It NEVER makes duplicates:
  * An existing price is found by its lookup key (rk_basic_monthly, ...), or
    by the price ID you already use (pass your current Render values as env
    vars, e.g. STRIPE_PRICE_BASIC=price_...). A price found by ID gets the
    lookup key attached so later runs find it on their own.
  * Yearly prices are added to the SAME product as the monthly price.
  * In live mode it refuses to create a monthly price or product that should
    already exist; it stops and tells you instead.
  * Amounts and billing periods are checked; a mismatch stops the run.
  * Nothing is written until the whole plan has been checked.

Always do a dry run first:

    STRIPE_SECRET_KEY=sk_live_...  \
    STRIPE_PRICE_BASIC=price_... STRIPE_PRICE_GROWTH=price_... STRIPE_PRICE_BUSINESS=price_... \
    STRIPE_PRICE_STORE_BASIC=price_... STRIPE_PRICE_STORE_GROWTH=price_... STRIPE_PRICE_STORE_BUSINESS=price_... \
    python scripts/stripe_setup_receiptkept.py --dry-run

then the same command without --dry-run. Safe to run again any time.
"""

import argparse
import os
import sys

import httpx

API = "https://api.stripe.com/v1"

# (env var, lookup_key, product key, product name, cents, interval)
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

# One free month. Monthly plans: 100% off one bill. Yearly plans: a month's
# worth off (100% off a yearly bill would be a free year).
COUPONS = [
    ("referral-1-month-free",     {"percent_off": "100", "duration": "once", "name": "Referral: 1 month free"}),
    ("referral-1-month-basic",    {"amount_off": "599",  "currency": "usd", "duration": "once", "name": "Referral: 1 month free (Basic yearly)"}),
    ("referral-1-month-growth",   {"amount_off": "1499", "currency": "usd", "duration": "once", "name": "Referral: 1 month free (Growth yearly)"}),
    ("referral-1-month-business", {"amount_off": "2999", "currency": "usd", "duration": "once", "name": "Referral: 1 month free (Business yearly)"}),
]


class Stop(Exception):
    """A problem found while checking: nothing has been written."""


class Stripe:
    def __init__(self, key: str, request=None):
        self.key = key
        self._request = request or self._http

    def _http(self, method, path, params=None, data=None):
        r = httpx.request(method, f"{API}/{path}", headers={"Authorization": f"Bearer {self.key}"},
                          params=params, data=data, timeout=30)
        try:
            body = r.json()
        except ValueError:
            body = {}
        return r.status_code, body

    def get(self, path, **params):
        return self._request("GET", path, params, None)

    def post(self, path, **data):
        status, body = self._request("POST", path, None, data)
        if status >= 400:
            raise Stop(f"Stripe refused {path}: {(body.get('error') or {}).get('message')}")
        return body


def _product_id(price: dict) -> str:
    p = price.get("product")
    return p if isinstance(p, str) else p["id"]


def _check(price: dict, cents: int, interval: str, label: str):
    rec = price.get("recurring") or {}
    if price.get("unit_amount") != cents or rec.get("interval") != interval or price.get("currency") != "usd":
        raise Stop(f"{label} is {price.get('unit_amount')} {price.get('currency')} per {rec.get('interval')}, "
                   f"expected {cents} usd per {interval}. Prices can't be edited in Stripe - fix or archive it in "
                   f"the dashboard, then run again.")
    if not price.get("active", True):
        raise Stop(f"{label} ({price['id']}) is archived in Stripe. Unarchive it or remove it from your env vars.")


def build_plan(stripe: Stripe, env: dict, live: bool) -> dict:
    """Work out every action first. Raises Stop on any problem, before writing."""
    found, actions, products = {}, [], {}
    for env_name, lookup, pkey, name, cents, interval in PRICES:
        status, by_lookup = stripe.get("prices", **{"lookup_keys[]": lookup, "active": "true"})
        price = (by_lookup.get("data") or [None])[0] if status < 400 else None
        how = "found by lookup key"
        current_id = (env.get(env_name) or "").strip()
        if price and current_id and price["id"] != current_id:
            raise Stop(f"{env_name}: Render uses {current_id} but lookup key {lookup} belongs to {price['id']}. "
                       f"Check which one is right before going on.")
        if not price and current_id:
            status, by_id = stripe.get(f"prices/{current_id}")
            if status >= 400:
                raise Stop(f"{env_name}={current_id} isn't a price in this Stripe account ({'live' if live else 'test'} mode).")
            price = by_id
            how = "found by the ID you passed in; will get lookup key " + lookup
            actions.append(("label", price["id"], lookup))
        if price:
            _check(price, cents, interval, f"{env_name} ({price['id']})")
            pid = _product_id(price)
            if products.setdefault(pkey, pid) != pid:
                raise Stop(f"{env_name} ({price['id']}) is on a different product than the other {pkey} price.")
            found[env_name] = (price["id"], how)
            continue
        if interval == "month" and live:
            raise Stop(f"{env_name}: no existing live monthly price found. Pass your current Render value "
                       f"(e.g. {env_name}=price_...) so it's reused instead of duplicated.")
        if pkey not in products:
            if live:
                raise Stop(f"No live product for {name}: create the monthly price first (or pass its ID).")
            actions.append(("product", pkey, name))
            products[pkey] = f"<new {pkey} product>"
        actions.append(("price", env_name, lookup, pkey, cents, interval))
        found[env_name] = (None, f"will create on product {products[pkey]}")

    for cid, params in COUPONS:
        status, coupon = stripe.get(f"coupons/{cid}")
        if status == 404:
            actions.append(("coupon", cid, params))
        elif status < 400:
            if cid != "referral-1-month-free" and (coupon.get("amount_off") != int(params["amount_off"]) or coupon.get("currency") != "usd"):
                raise Stop(f"Coupon {cid} exists with a different amount. Check it in the dashboard.")
    return {"found": found, "actions": actions, "products": products}


def apply_plan(stripe: Stripe, plan: dict) -> dict:
    ids = {k: v[0] for k, v in plan["found"].items()}
    products = dict(plan["products"])
    for act in plan["actions"]:
        if act[0] == "label":
            _, price_id, lookup = act
            stripe.post(f"prices/{price_id}", lookup_key=lookup)
        elif act[0] == "product":
            _, pkey, name = act
            products[pkey] = stripe.post("products", name=name, **{"metadata[app]": "receiptkept"})["id"]
        elif act[0] == "price":
            _, env_name, lookup, pkey, cents, interval = act
            price = stripe.post("prices", product=products[pkey], currency="usd", unit_amount=str(cents),
                                lookup_key=lookup, **{"recurring[interval]": interval, "metadata[app]": "receiptkept"})
            ids[env_name] = price["id"]
        elif act[0] == "coupon":
            _, cid, params = act
            stripe.post("coupons", id=cid, **params)
    return ids


def main(argv=None, env=None, stripe=None, out=print) -> int:
    ap = argparse.ArgumentParser(description="Set up ReceiptKept prices in Stripe")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    key = env.get("STRIPE_SECRET_KEY", "")
    if not key:
        out("Set STRIPE_SECRET_KEY first.")
        return 2
    live = key.startswith(("sk_live", "rk_live"))
    stripe = stripe or Stripe(key)
    out(f"# Stripe {'LIVE' if live else 'TEST'} mode{' - DRY RUN, nothing will change' if args.dry_run else ''}")
    try:
        plan = build_plan(stripe, env, live)
    except Stop as e:
        out(f"STOPPED, nothing was changed: {e}")
        return 1
    for env_name, (pid, how) in plan["found"].items():
        out(f"#   {env_name}: {pid or '(new)'} - {how}")
    creates = [a for a in plan["actions"] if a[0] in ("product", "price", "coupon")]
    labels = [a for a in plan["actions"] if a[0] == "label"]
    out(f"# Plan: create {sum(a[0] == 'price' for a in creates)} price(s), {sum(a[0] == 'product' for a in creates)} product(s), "
        f"{sum(a[0] == 'coupon' for a in creates)} coupon(s); add lookup keys to {len(labels)} existing price(s).")
    if args.dry_run:
        out("# Dry run finished. Run again without --dry-run to do it.")
        return 0
    try:
        ids = apply_plan(stripe, plan)
    except Stop as e:
        out(f"STOPPED partway: {e}\nRun the script again: it picks up what already exists and won't duplicate.")
        return 1
    out("# Put these on Render (the monthly ones should match what's there now):")
    for env_name, *_ in PRICES:
        out(f"{env_name}={ids[env_name]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
