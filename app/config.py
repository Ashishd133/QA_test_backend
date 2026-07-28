from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    python_service_token: str = "dev-only-insecure-token"
    database_url: str = ""
    environment: str = "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
