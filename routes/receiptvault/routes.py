from fastapi import APIRouter
import httpx
import asyncio
import os

receipt_routes = APIRouter()

RECEIPTVAULT_URL = "https://receipts.dealdily.com"

@receipt_routes.get("/")
def receipt_root():
    return {"service": "receiptvault running"}

@receipt_routes.get("/ping")
async def ping_receiptvault():
    """Keep receiptvault warm by pinging it"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{RECEIPTVAULT_URL}/api/user")
            return {"status": "pinged", "response_code": r.status_code}
    except Exception as e:
        return {"status": "ping_failed", "error": str(e)}
