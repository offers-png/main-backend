from fastapi import APIRouter

mobile_routes = APIRouter()

@mobile_routes.get("/")
def mobile_root():
    return {"service": "mobile running"}
