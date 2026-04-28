from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from routes.clipper.routes import clipper_routes
from routes.receiptvault.routes import receipt_routes
from routes.checkout.routes import checkout_routes, stripe_webhook
from routes.competitor.routes import competitor_routes
from routes.mobile.routes import mobile_routes
from routes.listingai.routes import listingai_routes
from routes.gigledger.routes import gig_router
from routes.gigledger.mileage import mileage_router
from routes.tariff.routes import router as tariff_router
from routes.scanpass.routes import scanpass_routes, scanpass_stripe_webhook
from routes.genid.routes import genid_routes

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(clipper_routes, prefix="/api/clipper")
app.include_router(receipt_routes, prefix="/api/receiptvault")
app.include_router(checkout_routes, prefix="/api/checkout")
app.include_router(competitor_routes, prefix="/api/competitor")
app.include_router(mobile_routes, prefix="/api/mobile")
app.include_router(listingai_routes, prefix="/listingai")
app.include_router(gig_router, prefix="/gig")
app.include_router(mileage_router)
app.include_router(tariff_router)
app.include_router(scanpass_routes, prefix="/api")
app.include_router(genid_routes, prefix="/api/genid")

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
