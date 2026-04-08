from fastapi import APIRouter

competitor_routes = APIRouter()

@competitor_routes.get("/")
def competitor_root():
    return {"service": "competitor running"}
