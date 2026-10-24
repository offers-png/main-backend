from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timedelta, timezone
from scheduler import run_scheduled_sends
from routes.receiptvault.connect_routes import sync_stale_accounts
from routes.receiptvault.entitlements import cancel_comp_subscriptions
from routes.receiptvault.routes import get_supabase as rk_supabase
from routes.clipper.routes import clipper_routes
from routes.receiptvault.stores import ActiveStoreMiddleware
from routes.receiptvault.app_setup import register_receiptkept
from routes.checkout.routes import checkout_routes, stripe_webhook
from routes.competitor.routes import competitor_routes
from routes.mobile.routes import mobile_routes
from routes.listingai.routes import listingai_routes
from routes.gigledger.routes import gig_router
from routes.gigledger.mileage import mileage_router
from routes.tariff.routes import router as tariff_router
from routes.scanpass.routes import scanpass_routes, scanpass_stripe_webhook
from routes.genid.routes import genid_routes
from routes.noor.routes import noor_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler()
    # Run every hour — checks send_day/send_frequency for each business
    scheduler.add_job(run_scheduled_sends, IntervalTrigger(hours=1), id="rv_scheduler", replace_existing=True)
    # Connected banks: safety net for the daily sync (Stripe's webhook can be
    # missed). Every 6 hours, and a minute after each start: Render's free
    # plan sleeps when idle, so a wake-up is often the first chance to run.
    scheduler.add_job(sync_stale_accounts, IntervalTrigger(hours=6), id="rk_bank_sync", replace_existing=True,
                      next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1))
    # Lifetime accounts: stop any Stripe subscription still attached (no charge)
    async def _cancel_comp():
        await cancel_comp_subscriptions(rk_supabase())
    scheduler.add_job(_cancel_comp, IntervalTrigger(hours=6), id="rk_comp_cancel", replace_existing=True,
                      next_run_time=datetime.now(timezone.utc) + timedelta(seconds=45))
    scheduler.start()
    print("[startup] ReceiptVault scheduler started — runs every hour")
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

# Reads X-Business-Id (the store picked in the store switcher) for every
# ReceiptKept request. Added before CORS so CORS stays the outermost layer.
app.add_middleware(ActiveStoreMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://receipts.dealdily.com",
        "https://receiptkept.com",
        "https://www.receiptkept.com",
        "https://receiptvaultdealdily.netlify.app",
        "https://sparkling-hotteok-81fc0b.netlify.app",
        "https://issa-uzair.netlify.app",
        "https://dealdily.com",
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:8080",
    ],
    allow_origin_regex=r"https://.*\.dealdily\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(clipper_routes, prefix="/api/clipper")
register_receiptkept(app)
app.include_router(checkout_routes, prefix="/api/checkout")
app.include_router(competitor_routes, prefix="/api/competitor")
app.include_router(mobile_routes, prefix="/api/mobile")
app.include_router(listingai_routes, prefix="/listingai")
app.include_router(gig_router, prefix="/gig")
app.include_router(mileage_router)
app.include_router(tariff_router)
app.include_router(scanpass_routes, prefix="/api")
app.include_router(genid_routes, prefix="/api/genid") 
app.include_router(noor_router)

# TEMP in-memory auth storage just to get signup/login working
USERS = {}

class AuthBody(BaseModel):
    email: str
    password: str

@app.post("/signup")
async def signup(body: AuthBody):
    email = body.email.strip().lower()
    password = body.password.strip()

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    if email in USERS:
        raise HTTPException(status_code=400, detail="Account already exists")

    USERS[email] = {
        "email": email,
        "password": password,
        "active": True,
    }

    return {
        "message": "Signup successful",
        "checkout_url": "https://competitor-intel-engine.onrender.com/dashboard"
    }

@app.post("/login")
async def login(body: AuthBody):
    email = body.email.strip().lower()
    password = body.password.strip()

    user = USERS.get(email)
    if not user or user["password"] != password:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return {
        "message": "Login successful",
        "redirect": "https://competitor-intel-engine.onrender.com/dashboard"
    }

@app.get("/me")
async def me():
    # TEMP dummy response so dashboard page doesn't instantly fail
    return {
        "email": "saleh852@gmail.com",
        "active": True
    }

@app.post("/logout")
async def logout():
    return {"message": "Logged out"}

# Stripe is configured to POST to /api/webhook — alias it here
@app.post("/api/webhook")
async def webhook_alias(request: Request):
    return await stripe_webhook(request)

@app.post("/api/scanpass/stripe/webhook")
async def scanpass_webhook_alias(request: Request):
    return await scanpass_stripe_webhook(request)

@app.get("/")
def root():
    return {"status": "main backend running"}
