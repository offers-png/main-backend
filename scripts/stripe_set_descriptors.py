"""
Sets the card-statement text on every ReceiptKept, CallsKept and GenID product
in Stripe, so charges read "DEALDILY* RECEIPTKEPT" and so on.

Why on the product: all three apps bill by subscription, and Stripe doesn't
let a subscription checkout pass statement_descriptor_suffix. For subscription
charges the product's statement_descriptor is the WHOLE statement line, so it
includes the DEALDILY* prefix itself.

Only products' statement_descriptor changes. Prices, names and everything else
are left alone. Products that don't belong to these apps are listed, not touched.

    STRIPE_SECRET_KEY=sk_live_... python scripts/stripe_set_descriptors.py --dry-run
    STRIPE_SECRET_KEY=sk_live_... python scripts/stripe_set_descriptors.py

Safe to run again: products already set are skipped.
"""

import argparse
import os
import sys

import httpx

API = "https://api.stripe.com/v1"

DESCRIPTORS = {
    "receiptkept": "DEALDILY* RECEIPTKEPT",
    "callskept": "DEALDILY* CALLSKEPT",
    "genid": "DEALDILY* GENID",
}


def app_for(product: dict):
    """Which app a product belongs to, or None."""
    app = ((product.get("metadata") or {}).get("app") or "").lower()
    if app in DESCRIPTORS:
        return app
    name = (product.get("name") or "").lower()
    if name.startswith("receiptkept"):
        return "receiptkept"
    # "Recall — Missed Call Recovery" is CallsKept's earlier name
    if name.startswith("callskept") or name.startswith("recall") or "missed call" in name:
        return "callskept"
    if name.startswith("genid"):
        return "genid"
    return None


def all_products(request):
    out, after = [], None
    while True:
        params = {"limit": "100"}  # active and archived
        if after:
            params["starting_after"] = after
        status, body = request("GET", "products", params, None)
        if status >= 400:
            raise SystemExit(f"Stripe refused the product list: {(body.get('error') or {}).get('message')}")
        out += body["data"]
        if not body.get("has_more"):
            return out
        after = body["data"][-1]["id"]


def http(key):
    def request(method, path, params, data):
        r = httpx.request(method, f"{API}/{path}", headers={"Authorization": f"Bearer {key}"},
                          params=params, data=data, timeout=30)
        return r.status_code, r.json()
    return request


def main(argv=None, env=None, request=None, out=print) -> int:
    ap = argparse.ArgumentParser(description="Set statement descriptors on app products")
    ap.add_argument("--dry-run", action="store_true", help="show what would change; change nothing")
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    key = env.get("STRIPE_SECRET_KEY", "")
    if not key:
        out("Set STRIPE_SECRET_KEY first.")
        return 2
    request = request or http(key)
    live = key.startswith(("sk_live", "rk_live"))
    out(f"# Stripe {'LIVE' if live else 'TEST'} mode{' - DRY RUN, nothing will change' if args.dry_run else ''}")

    changed = failed = 0
    for p in all_products(request):
        app = app_for(p)
        label = f"{p['id']} {p['name']!r}{'' if p.get('active') else ' (archived)'}"
        if not app:
            out(f"  not touched (not ReceiptKept/CallsKept/GenID): {label}")
            continue
        want = DESCRIPTORS[app]
        if p.get("statement_descriptor") == want:
            out(f"  already set: {label} -> {want}")
            continue
        out(f"  {'would set' if args.dry_run else 'set'}: {label}: {p.get('statement_descriptor')!r} -> {want!r}")
        if args.dry_run:
            changed += 1
            continue
        status, body = request("POST", f"products/{p['id']}", None, {"statement_descriptor": want})
        if status >= 400 or body.get("statement_descriptor") != want:
            out(f"    FAILED: {(body.get('error') or {}).get('message')}")
            failed += 1
        else:
            changed += 1
    out(f"# {'Would change' if args.dry_run else 'Changed'} {changed} product(s); {failed} failed. Prices untouched.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
