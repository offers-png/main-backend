from fastapi import APIRouter

checkout_routes = APIRouter()

@checkout_routes.get("/")
def checkout_root():
    return {"service": "checkout running"}
