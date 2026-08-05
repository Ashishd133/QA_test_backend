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
    # Reference agent (B2-01): LiveKit itself is configured via the LIVEKIT_URL/
    # LIVEKIT_API_KEY/LIVEKIT_API_SECRET env vars that AgentServer reads directly.
    # Gemini Live goes through Vertex AI (the AI-Studio-style GEMINI_API_KEY was
    # rejected with API_KEY_SERVICE_BLOCKED) — auth is via GOOGLE_APPLICATION_CREDENTIALS
    # (a service account key), which google-auth picks up automatically.
    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
