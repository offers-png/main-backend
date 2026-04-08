from fastapi import APIRouter

clipper_routes = APIRouter()

@clipper_routes.get("/")
def clipper_root():
    return {"service": "clipper running"}
