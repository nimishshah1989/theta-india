from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_key: str = ""
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    claude_daily_budget_usd: float = 0.30
    screener_session_cookie: str = ""
    environment: str = "development"
    port: int = 8001
    cors_origins: str = ""
    app_version: str = "0.1.0"

    class Config:
        env_file = ".env"

@lru_cache()
def get_settings() -> Settings:
    return Settings()
