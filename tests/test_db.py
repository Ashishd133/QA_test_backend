import pytest

from app.db import to_asyncpg_url


def test_to_asyncpg_url_rejects_empty_database_url() -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        to_asyncpg_url("")
