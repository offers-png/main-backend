from fastapi import APIRouter
import requests
from bs4 import BeautifulSoup
import tldextract

router = APIRouter()

def extract_text(url: str):
    try:
        res = requests.get(url, timeout=5)
        soup = BeautifulSoup(res.text, "lxml")

        title = soup.title.string if soup.title else ""
        description = ""
        desc_tag = soup.find("meta", attrs={"name": "description"})
        if desc_tag:
            description = desc_tag.get("content", "")

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

@router.get("/analyze")
def analyze(url: str):
    return extract_text(url)

@router.get("/health")
def health():
    return {"status": "competitor engine running"}
