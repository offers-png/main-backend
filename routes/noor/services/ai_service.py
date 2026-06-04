import httpx
from fastapi import HTTPException
from ..utils.config import settings

class AIService:
    @staticmethod
    async def call_claude(messages: list, system_prompt: str, max_tokens: int = 120, model: str = None) -> str:
        if not model:
            model = settings.claude_fast_model
            
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system_prompt,
                    "messages": messages,
                }
            )
            if resp.status_code != 200:
                raise HTTPException(500, f"Claude error: {resp.text}")
            return resp.json()["content"][0]["text"]

    @staticmethod
    async def transcribe_audio(audio_bytes: bytes, language: str = None) -> str:
        if not settings.openai_api_key:
            raise HTTPException(400, "OPENAI_API_KEY not set — Whisper unavailable")

        import io
        async with httpx.AsyncClient(timeout=30) as client:
            data = {"model": settings.whisper_model}
            if language:
                data["language"] = language

            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                files={"file": ("audio.webm", io.BytesIO(audio_bytes), "audio/webm")},
                data=data
            )
            if resp.status_code != 200:
                raise HTTPException(500, f"Whisper error: {resp.text}")
            return resp.json().get("text", "")

ai_service = AIService()
