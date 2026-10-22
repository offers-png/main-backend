"""
ReceiptKept multi-store core.

One owner login can own several stores (each store is a `businesses` row).
The front end tells us which store the person is working in with the
`X-Business-Id` header on every request; ActiveStoreMiddleware copies it
into a ContextVar so every access helper (resolve_store and the wrappers in
routes.py) scopes to that store without each endpoint having to pass it
around.

No header (older app builds, background jobs) falls back to the old
behaviour: the user's first owned store, else their first team membership.
That keeps existing single-store accounts working with no action needed.
"""

from contextvars import ContextVar
from typing import Optional

from fastapi import HTTPException

ALL_MODULES = ["receipts", "money_box", "mileage", "invoices", "inventory"]

ACTIVE_BUSINESS_ID: ContextVar[Optional[str]] = ContextVar("rk_active_business_id", default=None)
REQUEST_TIMEZONE: ContextVar[Optional[str]] = ContextVar("rk_request_timezone", default=None)
# Per-request memo of resolve_store results, so endpoints that call several
# access helpers (mileage does three) only hit the database once.
_STORE_CACHE: ContextVar[Optional[dict]] = ContextVar("rk_store_cache", default=None)


class ActiveStoreMiddleware:
    """Pure ASGI middleware (not BaseHTTPMiddleware) so the ContextVars it
    sets are visible inside the endpoint."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        business_id = (headers.get("x-business-id") or "").strip() or None
        tz = (headers.get("x-timezone") or "").strip() or None
        t1 = ACTIVE_BUSINESS_ID.set(business_id)
        t2 = REQUEST_TIMEZONE.set(tz)
        t3 = _STORE_CACHE.set({})
        try:
            await self.app(scope, receive, send)
        finally:
            ACTIVE_BUSINESS_ID.reset(t1)
            REQUEST_TIMEZONE.reset(t2)
            _STORE_CACHE.reset(t3)


def owned_stores(supabase, user_id: str) -> list:
    rows = (
        supabase.table("businesses").select("*")
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
    )
    return rows.data or []


def memberships(supabase, user_id: str) -> list:
    rows = (
        supabase.table("business_users").select("*")
        .eq("user_id", user_id)
        .eq("status", "active")
        .execute()
    )
    return rows.data or []


def _get_business(supabase, business_id: str) -> Optional[dict]:
    rows = supabase.table("businesses").select("*").eq("id", business_id).execute()
    return rows.data[0] if rows.data else None


def resolve_store(supabase, user_id: str, business_id: Optional[str] = None):
    """Returns (business, role, modules) for the store this request targets.

    role is 'owner' | 'manager' | 'bookkeeper' | 'employee'. Returns
    (None, None, []) when the user has no store at all. Raises 403 when the
    request names a store the user doesn't own and isn't a member of — we
    never silently fall back, so data can't land in the wrong store."""
    requested = business_id or ACTIVE_BUSINESS_ID.get()
    cache = _STORE_CACHE.get()
    cache_key = (user_id, requested)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    result = (None, None, [])
    if requested:
        biz = _get_business(supabase, requested)
        if biz and str(biz.get("user_id")) == str(user_id):
            result = (biz, "owner", list(ALL_MODULES))
        elif biz:
            m = (
                supabase.table("business_users").select("*")
                .eq("business_id", requested).eq("user_id", user_id).eq("status", "active")
                .execute()
            )
            if m.data:
                result = (biz, m.data[0].get("role") or "employee", m.data[0].get("modules") or [])
        if result[0] is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "store_access_denied", "message": "You don't have access to that store. Pick another store from the menu."},
            )
    else:
        owned = owned_stores(supabase, user_id)
        if owned:
            result = (owned[0], "owner", list(ALL_MODULES))
        else:
            for m in memberships(supabase, user_id):
                biz = _get_business(supabase, m["business_id"])
                if biz:
                    result = (biz, m.get("role") or "employee", m.get("modules") or [])
                    break

    if cache is not None:
        cache[cache_key] = result
    return result


def list_stores_for_user(supabase, user_id: str) -> list:
    """Every store this login can switch to: owned first (oldest first),
    then team memberships."""
    out = []
    seen = set()
    for b in owned_stores(supabase, user_id):
        seen.add(b["id"])
        out.append({"id": b["id"], "businessName": b.get("business_name"), "role": "owner", "createdAt": str(b.get("created_at") or "")})
    for m in memberships(supabase, user_id):
        if m["business_id"] in seen:
            continue
        biz = _get_business(supabase, m["business_id"])
        if not biz:
            continue
        seen.add(biz["id"])
        out.append({"id": biz["id"], "businessName": biz.get("business_name"), "role": m.get("role") or "employee", "createdAt": str(biz.get("created_at") or "")})
    return out
