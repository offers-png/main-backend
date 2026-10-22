"""Phase 5: Stripe Financial Connections (fake Stripe) — link, initial
import, refresh webhook adds only new ones, disconnect stops syncing,
plan limits, reconnect without duplicates."""

import pytest

from conftest import as_user
from test_multi_store import OWNER, make_first_store, finish_setup
from test_billing import send_event, stripe_env  # noqa: F401  (autouse env)


class FakeFC:
    def __init__(self):
        self.accounts = {}
        self.txns = {}       # account -> list of txns
        self.refresh = {}    # account -> refresh id
        self.sub_calls = []
        self.session_accounts = []
        self.last_session = None

    def add_txn(self, acct, tid, amount_cents, desc, ts, refresh, status="posted"):
        self.txns.setdefault(acct, []).append({"id": tid, "amount": amount_cents, "description": desc, "status": status,
                                               "transacted_at": ts, "status_transitions": {"posted_at": ts}, "transaction_refresh": refresh})
        self.refresh[acct] = refresh

    def respond(self, method, url, kw, Response):
        path = url.split("/v1/", 1)[1]
        data = kw.get("data") or {}
        if path == "customers":
            return Response(200, {"id": "cus_fc"})
        if path == "financial_connections/sessions":
            self.last_session = data
            return Response(200, {"id": "fcsess_1", "client_secret": "fcsess_1_secret"})
        if path.startswith("financial_connections/sessions/"):
            return Response(200, {"id": "fcsess_1", "accounts": {"data": self.session_accounts}})
        if path.startswith("financial_connections/transactions"):
            q = dict(p.split("=", 1) for p in path.split("?", 1)[1].split("&"))
            acct = q["account"]
            after = q.get("transaction_refresh[after]")
            rows = self.txns.get(acct, [])
            if after:
                order = [t["transaction_refresh"] for t in rows]
                idx = max(i for i, r in enumerate(order) if r == after) if after in order else -1
                rows = rows[idx + 1:]
            return Response(200, {"data": rows, "has_more": False})
        if path.startswith("financial_connections/accounts/"):
            parts = path.split("/")
            acct = parts[2]
            if len(parts) == 3:
                return Response(200, {"id": acct, "transaction_refresh": {"id": self.refresh.get(acct), "status": "succeeded"}})
            self.sub_calls.append((parts[3], acct))
            return Response(200, {"id": acct})
        return Response(200, {})


@pytest.fixture
def fc(http_calls):
    f = FakeFC()
    http_calls.state["responder"] = lambda m, u, kw: f.respond(m, u, kw, http_calls.Response)
    return f


@pytest.fixture
def growth_store(client, db):
    db.unique["rv_bank_transactions"] = [("business_id", "dedupe_key")]
    s = make_first_store(client)
    finish_setup(client, OWNER, s)
    next(a for a in db.tables["rv_accounts"] if a["owner_user_id"] == OWNER)["plan"] = "growth"
    return s


def fc_account(i, inst="Test Bank", last4="6789", sub="checking"):
    return {"id": f"fca_{i}", "display_name": f"{inst} {sub}", "institution_name": inst, "last4": last4, "subcategory": sub, "category": "cash"}


T0 = 1790000000  # 2026-09-21


def test_free_and_basic_cannot_connect(client, db, fc):
    s = make_first_store(client)
    r = client.post("/api/bank/connect/session", headers=as_user(OWNER, s))
    assert r.status_code == 402 and r.json()["detail"]["requiredPlan"] == "growth"


def test_session_is_transactions_only(client, growth_store, fc):
    r = client.post("/api/bank/connect/session", headers=as_user(OWNER, growth_store))
    assert r.status_code == 200 and r.json()["clientSecret"] == "fcsess_1_secret"
    assert fc.last_session["permissions[]"] == "transactions"
    subs = [v for k, v in fc.last_session.items() if k.startswith("filters[account_subcategories]")]
    assert subs == ["checking", "savings", "credit_card"]


def test_link_import_refresh_disconnect(client, db, growth_store, fc):
    fc.session_accounts = [fc_account(1)]
    fc.add_txn("fca_1", "fctxn_a", -4512, "SHELL OIL", T0, "fctxnref_1")
    fc.add_txn("fca_1", "fctxn_b", 125000, "SQUARE DEPOSIT", T0, "fctxnref_1")
    fc.add_txn("fca_1", "fctxn_p", -999, "PENDING THING", T0, "fctxnref_1", status="pending")

    r = client.post("/api/bank/connect/complete", json={"sessionId": "fcsess_1"}, headers=as_user(OWNER, growth_store)).json()
    assert len(r["connected"]) == 1 and r["imported"] == 2
    assert ("subscribe", "fca_1") in fc.sub_calls
    rows = db.tables["rv_bank_transactions"]
    assert {t["amount"] for t in rows} == {-45.12, 1250.0} and all(t["source"] == "stripe" for t in rows)

    # daily refresh: one new txn + the pending one now posted
    fc.add_txn("fca_1", "fctxn_c", -2000, "STAPLES", T0 + 86400, "fctxnref_2")
    fc.add_txn("fca_1", "fctxn_p", -999, "PENDING THING", T0, "fctxnref_2")
    send_event(client, "financial_connections.account.refreshed_transactions",
               {"id": "fca_1", "object": "financial_connections.account", "transaction_refresh": {"id": "fctxnref_2", "status": "succeeded"}})
    assert len(db.tables["rv_bank_transactions"]) == 4
    # replaying the same webhook adds nothing
    send_event(client, "financial_connections.account.refreshed_transactions",
               {"id": "fca_1", "transaction_refresh": {"id": "fctxnref_2", "status": "succeeded"}})
    assert len(db.tables["rv_bank_transactions"]) == 4

    acct_id = db.tables["rv_bank_accounts"][0]["id"]
    accts = client.get("/api/bank/accounts", headers=as_user(OWNER, growth_store)).json()
    assert accts[0]["status"] == "active" and accts[0]["lastSyncedAt"]

    # disconnect: unsubscribe + disconnect at Stripe; later webhooks do nothing
    r = client.post(f"/api/bank/accounts/{acct_id}/disconnect", headers=as_user(OWNER, growth_store)).json()
    assert r["status"] == "disconnected"
    assert ("unsubscribe", "fca_1") in fc.sub_calls and ("disconnect", "fca_1") in fc.sub_calls
    fc.add_txn("fca_1", "fctxn_d", -100, "AFTER DISCONNECT", T0 + 2 * 86400, "fctxnref_3")
    send_event(client, "financial_connections.account.refreshed_transactions",
               {"id": "fca_1", "transaction_refresh": {"id": "fctxnref_3", "status": "succeeded"}})
    assert len(db.tables["rv_bank_transactions"]) == 4  # nothing new, nothing deleted

    # reconnect the same bank: new Stripe ids, no duplicate rows
    fc.session_accounts = [fc_account(9)]
    for t in fc.txns["fca_1"][:2]:
        fc.add_txn("fca_9", t["id"] + "_new", t["amount"], t["description"], t["transacted_at"], "fctxnref_9")
    r = client.post("/api/bank/connect/complete", json={"sessionId": "fcsess_1"}, headers=as_user(OWNER, growth_store)).json()
    assert r["imported"] == 0
    assert len(db.tables["rv_bank_accounts"]) == 1 and db.tables["rv_bank_accounts"][0]["status"] == "active"


def test_growth_connects_two_accounts_max(client, db, growth_store, fc):
    fc.session_accounts = [fc_account(1), fc_account(2, last4="1111"), fc_account(3, last4="2222", sub="credit_card")]
    r = client.post("/api/bank/connect/complete", json={"sessionId": "fcsess_1"}, headers=as_user(OWNER, growth_store)).json()
    assert len(r["connected"]) == 2 and r["skipped"] == 1
    assert "connects 2 bank accounts" in r["message"]
    assert ("disconnect", "fca_3") in fc.sub_calls
    r = client.post("/api/bank/connect/session", headers=as_user(OWNER, growth_store))
    assert r.status_code == 402
