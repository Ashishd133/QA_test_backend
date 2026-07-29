from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    python_service_token: str = "dev-only-insecure-token"
    database_url: str = ""
    # Isolated Neon branch used exclusively by the test suite (B1-01/B1-08):
    # the shared production DB has a live worker continuously polling for
    # queued runs, which will claim and execute test-inserted rows out from
    # under a test before it can assert against them.
    test_database_url: str = ""
    environment: str = "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
