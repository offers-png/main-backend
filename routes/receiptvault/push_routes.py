"""
Push notifications for the ReceiptKept Android app (Firebase Cloud Messaging).

  POST   /api/devices                      -> {token, platform} register / refresh this phone
  DELETE /api/devices                      -> {token} forget this phone (sign-out)
  GET    /api/notifications/preferences    -> per-type on/off switches
  PUT    /api/notifications/preferences    -> change some of them

Tables (ReceiptKept only, RLS on, see migrations/2026-10-24_rv_device_tokens.sql):
  rv_device_tokens        one row per phone (fcm_token is unique)
  rv_notification_prefs   one row per login

Sending uses the FCM HTTP v1 API directly (the same call the Firebase Admin
SDK makes) with a short-lived OAuth token signed from the service-account
key. That keeps the Render build small (no firebase-admin / grpc). The key is
the env var FIREBASE_SERVICE_ACCOUNT (the JSON file's contents, raw or
base64). With no key set, nothing is sent and nothing fails.

Notification types (all on by default; each can be turned off in the app):
  weekly_missing_receipts  Monday 9am store time: big charges that need a receipt
                           (the only "nudge" push; at most one a week)
  receipt_saved            a receipt forwarded by email was saved
  bank_relogin             the bank connection needs the owner to sign in again
  accountant_sent          the accountant papers went out

send_push() never raises: a push problem must never break the request or
job that triggered it.
"""

import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from routes.receiptvault.routes import get_current_user, get_supabase as _rv_get_supabase
from routes.receiptvault.stores import resolve_store

push_routes = APIRouter(prefix="/api", tags=["push"])

TOKENS = "rv_device_tokens"
PREFS = "rv_notification_prefs"

# API field -> column
PREF_FIELDS = {
    "weeklyMissingReceipts": "weekly_missing_receipts",
    "receiptSaved": "receipt_saved",
    "bankRelogin": "bank_relogin",
    "accountantSent": "accountant_sent",
}
KIND_TO_COLUMN = {v: v for v in PREF_FIELDS.values()}
# Android notification channels created by the app (lib/native/push.ts)
KIND_CHANNEL = {
    "weekly_missing_receipts": "receipts",
    "receipt_saved": "receipts",
    "bank_relogin": "receipts",
    "accountant_sent": "accountant",
}


def get_supabase():
    return _rv_get_supabase()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── device tokens ───────────────────────────────────────────────────────────

class DeviceBody(BaseModel):
    token: str
    platform: Optional[str] = "android"


@push_routes.post("/devices")
async def register_device(body: DeviceBody, current_user=Depends(get_current_user)):
    token = (body.token or "").strip()
    if not token or len(token) > 4096:
        raise HTTPException(status_code=400, detail="A device token is required.")
    platform = (body.platform or "android").strip().lower()[:20]
    supabase = get_supabase()
    uid = current_user.user.id
    try:
        store, _role, _mods = resolve_store(supabase, uid)
    except HTTPException:
        store = None
    store_id = store["id"] if store else None
    existing = supabase.table(TOKENS).select("id").eq("fcm_token", token).execute().data or []
    row = {"user_id": uid, "store_id": store_id, "platform": platform, "last_seen_at": _now()}
    if existing:
        # Same phone, maybe a different login now: it belongs to whoever registered last.
        supabase.table(TOKENS).update(row).eq("fcm_token", token).execute()
    else:
        supabase.table(TOKENS).insert({**row, "fcm_token": token, "created_at": _now()}).execute()
    return {"ok": True}


class DeleteDeviceBody(BaseModel):
    token: str


@push_routes.delete("/devices")
async def delete_device(body: DeleteDeviceBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    supabase.table(TOKENS).delete().eq("fcm_token", (body.token or "").strip()).eq("user_id", current_user.user.id).execute()
    return {"ok": True}


def delete_user_push_data(supabase, user_id: str) -> None:
    """Account deletion: every phone token and the notification switches."""
    for table in (TOKENS, PREFS):
        try:
            supabase.table(table).delete().eq("user_id", user_id).execute()
        except Exception as e:
            print(f"[push] delete {table} failed: {e}")


def export_user_push_data(supabase, user_id: str) -> dict:
    """Data export: notification switches and the phones (tokens left out)."""
    try:
        devices = supabase.table(TOKENS).select("*").eq("user_id", user_id).execute().data or []
    except Exception:
        devices = []
    return {
        "notificationPreferences": get_prefs(supabase, user_id),
        "devices": [{k: v for k, v in d.items() if k not in ("fcm_token", "id")} for d in devices],
    }


# ── preferences ─────────────────────────────────────────────────────────────

def _pref_row(supabase, user_id: str) -> Optional[dict]:
    try:
        rows = supabase.table(PREFS).select("*").eq("user_id", user_id).execute().data or []
    except Exception:
        return None
    return rows[0] if rows else None


def get_prefs(supabase, user_id: str) -> dict:
    row = _pref_row(supabase, user_id) or {}
    return {api: row.get(col, True) is not False for api, col in PREF_FIELDS.items()}


def kind_enabled(supabase, user_id: str, kind: str) -> bool:
    row = _pref_row(supabase, user_id) or {}
    col = KIND_TO_COLUMN.get(kind)
    return col is None or row.get(col, True) is not False


class PrefsBody(BaseModel):
    weeklyMissingReceipts: Optional[bool] = None
    receiptSaved: Optional[bool] = None
    bankRelogin: Optional[bool] = None
    accountantSent: Optional[bool] = None


@push_routes.get("/notifications/preferences")
async def read_prefs(current_user=Depends(get_current_user)):
    return get_prefs(get_supabase(), current_user.user.id)


@push_routes.put("/notifications/preferences")
async def update_prefs(body: PrefsBody, current_user=Depends(get_current_user)):
    supabase = get_supabase()
    uid = current_user.user.id
    changes = {col: getattr(body, api) for api, col in PREF_FIELDS.items() if getattr(body, api) is not None}
    if changes:
        changes["updated_at"] = _now()
        if _pref_row(supabase, uid):
            supabase.table(PREFS).update(changes).eq("user_id", uid).execute()
        else:
            supabase.table(PREFS).insert({"user_id": uid, **changes}).execute()
    return get_prefs(supabase, uid)


# ── sending (FCM HTTP v1) ───────────────────────────────────────────────────

_token_cache = {"token": None, "exp": 0.0}


def _service_account() -> Optional[dict]:
    raw = (os.getenv("FIREBASE_SERVICE_ACCOUNT") or "").strip()
    if not raw:
        return None
    try:
        if not raw.startswith("{"):
            raw = base64.b64decode(raw).decode("utf-8")
        sa = json.loads(raw)
        return sa if sa.get("private_key") and sa.get("client_email") and sa.get("project_id") else None
    except Exception as e:
        print(f"[push] FIREBASE_SERVICE_ACCOUNT unreadable: {e}")
        return None


def push_configured() -> bool:
    return _service_account() is not None


async def _access_token(sa: dict) -> Optional[str]:
    if _token_cache["token"] and _token_cache["exp"] - 60 > time.time():
        return _token_cache["token"]
    import jwt  # PyJWT[crypto]
    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": sa["client_email"],
            "scope": "https://www.googleapis.com/auth/firebase.messaging",
            "aud": sa.get("token_uri") or "https://oauth2.googleapis.com/token",
            "iat": now,
            "exp": now + 3600,
        },
        sa["private_key"],
        algorithm="RS256",
        headers={"kid": sa.get("private_key_id")} if sa.get("private_key_id") else None,
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            sa.get("token_uri") or "https://oauth2.googleapis.com/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        )
    if r.status_code != 200:
        print(f"[push] OAuth token failed: {r.status_code} {r.text[:200]}")
        return None
    data = r.json()
    _token_cache["token"] = data.get("access_token")
    _token_cache["exp"] = time.time() + int(data.get("expires_in") or 3600)
    return _token_cache["token"]


def _tokens_for(supabase, user_id: str) -> list:
    try:
        return supabase.table(TOKENS).select("*").eq("user_id", user_id).execute().data or []
    except Exception as e:
        print(f"[push] token read failed: {e}")
        return []


async def send_push(supabase, user_id: str, kind: str, title: str, body: str, url: str = "/") -> int:
    """Send one notification to every phone of `user_id`, if they have that
    kind turned on. Returns how many phones it reached. Never raises."""
    try:
        if not user_id or not kind_enabled(supabase, user_id, kind):
            return 0
        sa = _service_account()
        if not sa:
            return 0
        devices = _tokens_for(supabase, user_id)
        if not devices:
            return 0
        access = await _access_token(sa)
        if not access:
            return 0
        endpoint = f"https://fcm.googleapis.com/v1/projects/{sa['project_id']}/messages:send"
        sent = 0
        async with httpx.AsyncClient(timeout=20.0) as client:
            for d in devices:
                msg = {
                    "message": {
                        "token": d["fcm_token"],
                        "notification": {"title": title[:120], "body": body[:240]},
                        "data": {"url": url, "kind": kind},
                        "android": {
                            "priority": "high",
                            "notification": {"channel_id": KIND_CHANNEL.get(kind, "receipts"), "color": "#2563EB"},
                        },
                    }
                }
                r = await client.post(endpoint, json=msg, headers={"Authorization": f"Bearer {access}"})
                if r.status_code == 200:
                    sent += 1
                elif r.status_code in (400, 404) and ("UNREGISTERED" in r.text or "INVALID_ARGUMENT" in r.text or "NOT_FOUND" in r.text):
                    # App uninstalled / token rotated: forget it
                    supabase.table(TOKENS).delete().eq("fcm_token", d["fcm_token"]).execute()
                else:
                    print(f"[push] send failed {r.status_code}: {r.text[:200]}")
        return sent
    except Exception as e:
        print(f"[push] {kind} for {user_id} failed: {e}")
        return 0


async def notify_store_owner(supabase, business_id: str, kind: str, title: str, body: str, url: str = "/") -> int:
    try:
        rows = supabase.table("businesses").select("id, user_id").eq("id", business_id).execute().data or []
    except Exception:
        return 0
    if not rows:
        return 0
    return await send_push(supabase, rows[0].get("user_id"), kind, title, body, url)


# ── weekly missing-receipt summary (Monday 9am store time) ─────────────────

def _local_now(supabase, owner_id: str, now: datetime) -> datetime:
    tz = "America/New_York"
    try:
        acct = supabase.table("rv_accounts").select("timezone").eq("owner_user_id", owner_id).execute().data or []
        if acct and acct[0].get("timezone"):
            ZoneInfo(acct[0]["timezone"])
            tz = acct[0]["timezone"]
    except Exception:
        pass
    return now.astimezone(ZoneInfo(tz))


def _missing_receipts(supabase, owner_id: str) -> int:
    from routes.receiptvault.bank_core import receipt_required
    stores = supabase.table("businesses").select("id, archived_at").eq("user_id", owner_id).execute().data or []
    n = 0
    for s in stores:
        if s.get("archived_at"):
            continue
        txns = (
            supabase.table("rv_bank_transactions").select("*")
            .eq("business_id", s["id"]).eq("status", "needs_receipt").execute()
        ).data or []
        n += sum(1 for t in txns if float(t.get("amount") or 0) < 0 and not t.get("matched_receipt_id") and receipt_required(t))
    return n


async def run_weekly_missing_receipts(supabase=None, now: Optional[datetime] = None) -> dict:
    """Hourly job. For each login with a phone registered, on Monday between
    9 and 10am in their time zone, send one summary if any big charge still
    needs a receipt. last_weekly_push_at makes sure it's at most once a week."""
    supabase = supabase or get_supabase()
    now = now or datetime.now(timezone.utc)
    done = {"users": 0, "sent": 0}
    if not push_configured():
        return done
    try:
        owners = sorted({d["user_id"] for d in (supabase.table(TOKENS).select("user_id").execute().data or []) if d.get("user_id")})
    except Exception as e:
        print(f"[push] weekly: token list failed: {e}")
        return done
    for uid in owners:
        done["users"] += 1
        local = _local_now(supabase, uid, now)
        # 9-11am window: Render can sleep through the 9 o'clock run
        if local.weekday() != 0 or not (9 <= local.hour <= 11):
            continue
        row = _pref_row(supabase, uid) or {}
        if row.get("weekly_missing_receipts") is False:
            continue
        last = row.get("last_weekly_push_at")
        if last:
            try:
                if now - datetime.fromisoformat(str(last).replace("Z", "+00:00")) < timedelta(days=6):
                    continue
            except ValueError:
                pass
        n = _missing_receipts(supabase, uid)
        if n <= 0:
            continue
        title = f"{n} big charge{'s' if n != 1 else ''} need{'s' if n == 1 else ''} a receipt"
        sent = await send_push(supabase, uid, "weekly_missing_receipts", title, "Tap to scan them now. It takes a minute each.", "/bank")
        if sent:
            done["sent"] += 1
            stamp = {"last_weekly_push_at": now.isoformat(), "updated_at": _now()}
            if row:
                supabase.table(PREFS).update(stamp).eq("user_id", uid).execute()
            else:
                supabase.table(PREFS).insert({"user_id": uid, **stamp}).execute()
    print(f"[push] weekly {done}")
    return done
