"""The descriptor script only changes app products' statement_descriptor."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("rk_desc", Path(__file__).parent.parent / "scripts" / "stripe_set_descriptors.py")
desc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desc)


def fake_account():
    products = [
        {"id": "p1", "name": "ReceiptKept Basic", "active": True, "metadata": {"app": "receiptkept"}, "statement_descriptor": None},
        {"id": "p2", "name": "ReceiptKept Extra Store (Growth)", "active": True, "metadata": {}, "statement_descriptor": None},
        {"id": "p3", "name": "CallsKept Pro", "active": True, "metadata": {}, "statement_descriptor": None},
        {"id": "p4", "name": "Recall — Missed Call Recovery", "active": False, "metadata": {}, "statement_descriptor": None},
        {"id": "p5", "name": "GenID Verification", "active": True, "metadata": {}, "statement_descriptor": None},
        {"id": "p6", "name": "API Access (pro)", "active": True, "metadata": {}, "statement_descriptor": "OTHER"},
        {"id": "p7", "name": "CallsKept Basic", "active": True, "metadata": {}, "statement_descriptor": "DEALDILY* CALLSKEPT"},
    ]
    writes = []

    def request(method, path, params, data):
        if method == "GET":
            return 200, {"data": products, "has_more": False}
        writes.append((path, dict(data)))
        p = next(x for x in products if x["id"] == path.split("/")[1])
        p.update(data)
        return 200, p
    return products, writes, request


def run(request, *argv):
    lines = []
    code = desc.main(list(argv), env={"STRIPE_SECRET_KEY": "sk_live_x"}, request=request, out=lines.append)
    return code, "\n".join(lines)


def test_dry_run_changes_nothing():
    _, writes, request = fake_account()
    code, out = run(request, "--dry-run")
    assert code == 0 and writes == [] and "Would change 5 product(s)" in out


def test_sets_each_app_and_touches_nothing_else():
    products, writes, request = fake_account()
    code, out = run(request)
    assert code == 0
    by_id = {p["id"]: p["statement_descriptor"] for p in products}
    assert by_id == {"p1": "DEALDILY* RECEIPTKEPT", "p2": "DEALDILY* RECEIPTKEPT", "p3": "DEALDILY* CALLSKEPT",
                     "p4": "DEALDILY* CALLSKEPT", "p5": "DEALDILY* GENID", "p6": "OTHER", "p7": "DEALDILY* CALLSKEPT"}
    assert all(set(data) == {"statement_descriptor"} for _, data in writes)  # nothing but the descriptor
    assert {path for path, _ in writes} == {"products/p1", "products/p2", "products/p3", "products/p4", "products/p5"}
    writes.clear()
    code, out = run(request)
    assert code == 0 and writes == []  # second run: nothing to do


def test_descriptors_fit_stripe_limit():
    assert all(len(d) <= 22 for d in desc.DESCRIPTORS.values())
