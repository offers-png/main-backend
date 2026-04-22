from fastapi import APIRouter
import requests
from bs4 import BeautifulSoup

competitor_routes = APIRouter()

def extract_text(url: str):
    try:
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"

        res = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0"
        })
        soup = BeautifulSoup(res.text, "lxml")

        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        description = ""

        desc_tag = soup.find("meta", attrs={"name": "description"})
        if desc_tag and desc_tag.get("content"):
            description = desc_tag.get("content", "").strip()

        return {
            "url": url,
            "title": title,
            "description": description
        }
    except Exception as e:
        return {
            "url": url,
            "error": str(e)
        }

@competitor_routes.get("/analyze")
def analyze(url: str):
    return extract_text(url)

@competitor_routes.get("/health")
def health():
    return {"status": "competitor engine running"}
