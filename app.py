from fastapi import FastAPI, Request
from routes.clipper.routes import clipper_routes
from routes.receiptvault.routes import receipt_routes
from routes.checkout.routes import checkout_routes, stripe_webhook
from routes.competitor.routes import competitor_routes
from routes.mobile.routes import mobile_routes
from routes.listingai.routes import listingai_routes
from fastapi.middleware.cors import CORSMiddleware
from routes.gigledger.routes import gig_router
from routes.gigledger.mileage import mileage_router
from routes.tariff.routes import router as tariff_router

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

# Stripe is configured to POST to /api/webhook — alias it here
@app.post("/api/webhook")
async def webhook_alias(request: Request):
    return await stripe_webhook(request)

@app.get("/")
def root():
    return {"status": "main backend running"}
