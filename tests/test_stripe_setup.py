"""The Stripe setup script must reuse existing live monthly prices and never duplicate."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("rk_setup", Path(__file__).parent.parent / "scripts" / "stripe_setup_receiptkept.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

MONTHLY = {  # env -> (cents, product) as made by hand in the live dashboard: no lookup keys
    "STRIPE_PRICE_BASIC": (599, "prod_basic"),
    "STRIPE_PRICE_GROWTH": (1499, "prod_growth"),
    "STRIPE_PRICE_BUSINESS": (2999, "prod_business"),
    "STRIPE_PRICE_STORE_BASIC": (499, "prod_sb"),
    "STRIPE_PRICE_STORE_GROWTH": (999, "prod_sg"),
    "STRIPE_PRICE_STORE_BUSINESS": (1499, "prod_sbiz"),
}


class FakeStripe:
    def __init__(self):
        self.prices, self.products, self.coupons, self.writes = {}, {"prod_basic", "prod_growth", "prod_business", "prod_sb", "prod_sg", "prod_sbiz"}, {}, []
        for env, (cents, prod) in MONTHLY.items():
            pid = "price_live_" + env.lower()
            self.prices[pid] = {"id": pid, "unit_amount": cents, "currency": "usd", "recurring": {"interval": "month"},
                                "product": prod, "active": True, "lookup_key": None}

    def request(self, method, path, params, data):
        if method == "GET" and path == "prices":
            key = params["lookup_keys[]"]
            return 200, {"data": [p for p in self.prices.values() if p["lookup_key"] == key and p["active"]]}
        if method == "GET" and path.startswith("prices/"):
            p = self.prices.get(path.split("/")[1])
            return (200, p) if p else (404, {"error": {"message": "No such price"}})
        if method == "GET" and path.startswith("coupons/"):
            c = self.coupons.get(path.split("/")[1])
            return (200, c) if c else (404, {"error": {"message": "No such coupon"}})
        self.writes.append((path, dict(data)))
        if path.startswith("prices/"):
            self.prices[path.split("/")[1]]["lookup_key"] = data["lookup_key"]
            return 200, self.prices[path.split("/")[1]]
        if path == "prices":
            pid = f"price_new_{len(self.prices)}"
            self.prices[pid] = {"id": pid, "unit_amount": int(data["unit_amount"]), "currency": "usd", "active": True,
                                "recurring": {"interval": data["recurring[interval]"]}, "product": data["product"], "lookup_key": data["lookup_key"]}
            return 200, self.prices[pid]
        if path == "products":
            pid = f"prod_new_{len(self.products)}"
            self.products.add(pid)
            return 200, {"id": pid}
        if path == "coupons":
            amount = data.get("amount_off")
            self.coupons[data["id"]] = {"id": data["id"], "amount_off": int(amount) if amount else None, "currency": data.get("currency")}
            return 200, self.coupons[data["id"]]
        raise AssertionError(path)


def live_env(**extra):
    env = {"STRIPE_SECRET_KEY": "sk_live_x", **{k: "price_live_" + k.lower() for k in MONTHLY}}
    env.update(extra)
    return env


def run(fake, env, *argv):
    lines = []
    code = setup.main(list(argv), env=env, stripe=setup.Stripe("k", fake.request), out=lines.append)
    return code, "\n".join(lines)


def test_dry_run_writes_nothing():
    fake = FakeStripe()
    code, out = run(fake, live_env(), "--dry-run")
    assert code == 0 and fake.writes == []
    assert "create 6 price(s), 0 product(s), 4 coupon(s); add lookup keys to 6 existing price(s)" in out


def test_live_reuses_monthly_and_adds_yearly_to_same_products():
    fake = FakeStripe()
    code, out = run(fake, live_env())
    assert code == 0
    assert not any(w[0] == "products" for w in fake.writes)  # no new products
    new_prices = [w[1] for w in fake.writes if w[0] == "prices"]
    assert len(new_prices) == 6 and all(p["recurring[interval]"] == "year" for p in new_prices)
    assert {p["product"]: int(p["unit_amount"]) for p in new_prices} == {
        "prod_basic": 4999, "prod_growth": 12599, "prod_business": 25199, "prod_sb": 4199, "prod_sg": 8399, "prod_sbiz": 12599}
    for env_name in MONTHLY:  # monthly IDs printed unchanged, now labelled
        assert f"{env_name}=price_live_{env_name.lower()}" in out
        assert fake.prices["price_live_" + env_name.lower()]["lookup_key"]
    assert len(fake.prices) == 12

    # second run (even without passing the IDs) finds everything: no writes at all
    fake.writes.clear()
    code, out = run(fake, {"STRIPE_SECRET_KEY": "sk_live_x"})
    assert code == 0 and fake.writes == [] and "create 0 price(s), 0 product(s), 0 coupon(s)" in out


def test_live_without_current_ids_refuses_to_create_monthly():
    fake = FakeStripe()
    code, out = run(fake, {"STRIPE_SECRET_KEY": "sk_live_x"})
    assert code == 1 and fake.writes == [] and "no existing live monthly price found" in out


def test_wrong_amount_stops_before_writing():
    fake = FakeStripe()
    fake.prices["price_live_stripe_price_growth"]["unit_amount"] = 1999
    code, out = run(fake, live_env())
    assert code == 1 and fake.writes == [] and "expected 1499 usd per month" in out


def test_unknown_or_test_mode_id_stops():
    fake = FakeStripe()
    code, out = run(fake, live_env(STRIPE_PRICE_BASIC="price_1UTVjCHDiwSWl43optVD0YtQ"))
    assert code == 1 and fake.writes == [] and "isn't a price in this Stripe account" in out


def test_lookup_key_on_a_different_price_stops():
    fake = FakeStripe()
    fake.prices["price_other"] = {"id": "price_other", "unit_amount": 599, "currency": "usd", "recurring": {"interval": "month"},
                                  "product": "prod_basic", "active": True, "lookup_key": "rk_basic_monthly"}
    code, out = run(fake, live_env())
    assert code == 1 and fake.writes == [] and "belongs to price_other" in out


def test_test_mode_from_scratch_still_creates_everything():
    fake = FakeStripe()
    fake.prices.clear()
    fake.products.clear()
    code, out = run(fake, {"STRIPE_SECRET_KEY": "sk_test_x"})
    assert code == 0 and sum(w[0] == "products" for w in fake.writes) == 6 and len(fake.prices) == 12
