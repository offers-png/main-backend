from fastapi import APIRouter

receipt_routes = APIRouter()

@receipt_routes.get("/")
def receipt_root():
    return {"service": "receiptvault running"}
