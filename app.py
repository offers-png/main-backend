from fastapi import FastAPI
from routes.clipper.routes import clipper_routes
from routes.receiptvault.routes import receipt_routes
from routes.checkout.routes import checkout_routes
from routes.competitor.routes import competitor_routes
from routes.mobile.routes import mobile_routes
from fastapi.middleware.cors import CORSMiddleware

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

@app.get("/")
def root():
    return {"status": "main backend running"}
