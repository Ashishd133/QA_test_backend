"""B2.7-05 tests: the nine built-in metrics are real, visible rows -- not
a code-only registry. Relies on `app/seed.py` having been run against
TEST_DATABASE_URL (the established dual-DB discipline; see this ticket's
commit and every migration since 010), not on seeding them itself --
that's the whole point of the "without a seed step" done-when: a fresh
*project* sees them because they're globally seeded once, not per-project.
"""

from httpx import ASGITransport, AsyncClient

from app.engine.metrics.builtins import BUILTIN_METRICS
from app.main import app
from tests.conftest import auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def test_all_nine_builtins_appear_for_a_fresh_project() -> None:
    async with await _client() as client:
        response = await client.get("/v1/metrics")
    assert response.status_code == 200
    items = response.json()
    builtin_names = {m["name"] for m in items if m["builtin"]}
    assert builtin_names == {m.name for m in BUILTIN_METRICS}


async def test_every_builtin_is_active_with_a_docs_string() -> None:
    async with await _client() as client:
        response = await client.get("/v1/metrics")
    items = {m["name"]: m for m in response.json() if m["builtin"]}
    for defn in BUILTIN_METRICS:
        row = items[defn.name]
        assert row["status"] == "active"
        assert row["description"] == defn.docs
        assert row["kind"] == defn.kind
        assert row["outputType"] == defn.output_type
        assert row["projectId"] is None
        assert row["agentId"] is None


async def test_builtins_are_read_only() -> None:
    async with await _client() as client:
        list_response = await client.get("/v1/metrics")
        builtin_id = next(m["id"] for m in list_response.json() if m["name"] == "response_latency")

        patch_response = await client.patch(f"/v1/metrics/{builtin_id}", json={"description": "x"})
        assert patch_response.status_code == 409
        assert patch_response.json()["error"]["code"] == "builtin_read_only"

        delete_response = await client.delete(f"/v1/metrics/{builtin_id}")
        assert delete_response.status_code == 409
        assert delete_response.json()["error"]["code"] == "builtin_read_only"
