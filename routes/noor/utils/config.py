import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    supabase_url: str = os.environ.get("SUPABASE_URL", "")
    supabase_key: str = os.environ.get("SUPABASE_SERVICE_KEY", "")
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")
    
    # AI Model Settings
    claude_fast_model: str = "claude-haiku-4-5-20251001"
    claude_vision_model: str = "claude-sonnet-4-20250514"
    whisper_model: str = "whisper-1"

    class Config:
        env_file = ".env"

settings = Settings()
