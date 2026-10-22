"""
ReceiptKept transactional email helpers.

(The old 7-day-trial reminder emails lived here; there is no trial any more
— every signup starts on the Free plan — so only the shared send/wrap
helpers remain. The module name is kept so existing imports keep working.)
"""

import os

import httpx

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
SUPABASE_KEY = (
    os.getenv("SUPABASE_ANON_KEY")
    or os.getenv("SUPABASE_KEY")
    or os.getenv("SUPABASE_SERVICE_KEY")
)
SEND_EMAIL_URL = f"{SUPABASE_URL}/functions/v1/send-invoice"
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://receiptkept.com")


async def _send_email(to: str, subject: str, html: str):
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            SEND_EMAIL_URL,
            headers={"apikey": SUPABASE_KEY or "", "Content-Type": "application/json", "X-Relay-Secret": os.getenv("EMAIL_RELAY_SECRET", "")},
            json={"to": to, "from": "ReceiptKept", "subject": subject, "html": html},
        )
        resp.raise_for_status()


def _wrap(body_html: str) -> str:
    return f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto">
  <div style="background:#1a6b3a;padding:24px 32px;border-radius:12px 12px 0 0">
    <h1 style="color:white;margin:0;font-size:20px">ReceiptKept</h1>
  </div>
  <div style="background:#f2faf5;padding:24px 32px">
    {body_html}
    <p style="color:#9c9c96;font-size:12px;margin-top:24px">ReceiptKept &middot; receiptkept.com</p>
  </div>
</div>"""
