import importlib
import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")

from fake_supabase import FakeSupabase  # noqa: E402

RV_MODULES = [
    "routes.receiptvault.routes",
    "routes.receiptvault.billing_routes",
    "routes.receiptvault.money_routes",
    "routes.receiptvault.pnl_routes",
    "routes.receiptvault.mileage_routes",
    "routes.receiptvault.inventory_routes",
    "routes.receiptvault.invoice_routes",
    "routes.receiptvault.team_routes",
    "routes.receiptvault.dropbox_routes",
    "routes.receiptvault.store_routes",
]
OPTIONAL_MODULES = [
    "routes.receiptvault.bank_routes",
    "routes.receiptvault.statement_routes",
    "routes.receiptvault.connect_routes",
    "routes.receiptvault.inbound_email_routes",
    "routes.receiptvault.export_routes",
]


def _load(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name == name:
            return None
        raise


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or str(self._payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


@pytest.fixture(autouse=True)
def http_calls(monkeypatch):
    """No test ever reaches the network: every httpx call is recorded and
    answered by `responder` (tests can swap it to fake Stripe/Anthropic)."""
    import httpx
    calls = []
    state = {"responder": lambda method, url, kw: _FakeResponse(200, {})}

    async def fake_request(self, method, url, *a, **kw):
        calls.append((method, str(url), kw))
        return state["responder"](method, str(url), kw)

    async def fake_post(self, url, *a, **kw):
        return await fake_request(self, "POST", url, *a, **kw)

    async def fake_get(self, url, *a, **kw):
        return await fake_request(self, "GET", url, *a, **kw)

    async def fake_delete(self, url, *a, **kw):
        return await fake_request(self, "DELETE", url, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(httpx.AsyncClient, "delete", fake_delete)
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    return SimpleNamespace(calls=calls, state=state, Response=_FakeResponse)


@pytest.fixture
def db():
    return FakeSupabase()


@pytest.fixture
def app(db, monkeypatch):
    mods = [m for m in (_load(n) for n in RV_MODULES + OPTIONAL_MODULES) if m]
    for m in mods:
        if hasattr(m, "get_supabase"):
            monkeypatch.setattr(m, "get_supabase", lambda: db)
    ent = _load("routes.receiptvault.entitlements")
    if ent and hasattr(ent, "get_supabase"):
        monkeypatch.setattr(ent, "get_supabase", lambda: db)

    from routes.receiptvault.routes import get_current_user
    from routes.receiptvault.stores import ActiveStoreMiddleware

    async def fake_user(x_test_user: str = Header(None), x_test_email: str = Header(None)):
        if not x_test_user:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="no user")
        return SimpleNamespace(user=SimpleNamespace(id=x_test_user, email=x_test_email or f"{x_test_user}@test.com", user_metadata={}, created_at="2026-01-01"))

    from routes.receiptvault.app_setup import register_receiptkept
    a = FastAPI()
    a.add_middleware(ActiveStoreMiddleware)
    register_receiptkept(a)
    a.dependency_overrides[get_current_user] = fake_user
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def as_user(user_id, store_id=None, email=None):
    h = {"X-Test-User": user_id}
    if store_id:
        h["X-Business-Id"] = store_id
    if email:
        h["X-Test-Email"] = email
    return h
