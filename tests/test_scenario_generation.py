"""B2.7-12 tests: POST /v1/suites/{id}/scenarios:generate + the extended
`fromDraftId` accept path. Generation itself is exercised against a fake
GenAIClient (same fake-client pattern as tests/test_judge.py) -- no real
Gemini call, deterministic, no cost. The accept path is real DB + real
HTTP throughout, since that's where B2.7-11's persona_id/goal/script/
metrics plumbing actually has to work end to end.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.suites import get_generation_client_factory
from app.engine.generation.models import GeneratedAssertion, GeneratedDraft, GenerationResponse
from app.engine.judge.judge import (
    GenAIClient,
    _Aio,
    _AioModels,
    _GenerateContentResponse,
    _UsageMetadata,
)
from app.main import app
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


@dataclass
class _FakeUsageMetadata(_UsageMetadata):
    prompt_token_count: int | None = 10
    candidates_token_count: int | None = 10


@dataclass
class _FakeResponse(_GenerateContentResponse):
    parsed: object
    text: str | None = None
    usage_metadata: _FakeUsageMetadata | None = field(default_factory=_FakeUsageMetadata)


@dataclass
class _FakeModels(_AioModels):
    responses: list[_FakeResponse]
    calls: list[str] = field(default_factory=list)

    async def generate_content(self, *, model: str, contents: str, config: object) -> _FakeResponse:
        self.calls.append(contents)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


@dataclass
class _FakeAio(_Aio):
    models: _FakeModels


@dataclass
class _FakeClient(GenAIClient):
    aio: _FakeAio


def _draft(
    name: str, *, kind: str = "positive", persona_name: str = "Priya Sharma"
) -> GeneratedDraft:
    return GeneratedDraft(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        persona_name=persona_name,
        scenario_goal=f"{name} is resolved to the caller's satisfaction",
        caller_goal=f"You need help with: {name}",
        opening_line="Hi, I need some help.",
        assertions=[
            GeneratedAssertion(
                id="a1",
                name="Verifies identity",
                description="Agent asks for identity verification before acting",
                distinguish_from="Not about whether it acts on the request afterward",
            )
        ],
        metric_names=["response_latency"],
    )


def _client_returning(*responses: _FakeResponse) -> _FakeClient:
    return _FakeClient(aio=_FakeAio(models=_FakeModels(responses=list(responses))))


async def _make_agent_and_suite(
    client: AsyncClient,
    *,
    prompt: str | None = "Help callers with their bank account.",
    description: str | None = None,
) -> dict[str, str]:
    agent_response = await client.post(
        "/v1/agents",
        json={
            "name": "Generation Test Agent",
            "config": {
                "transport": "web",
                "roomUrl": "https://example.livekit.cloud/room",
                "token": "tok",
            },
            "prompt": prompt,
            "description": description,
        },
    )
    agent_id = agent_response.json()["id"]
    suite_response = await client.post(
        "/v1/suites", json={"name": "Generation Test Suite", "agentId": agent_id}
    )
    suite_id = suite_response.json()["id"]
    return {"agent_id": agent_id, "suite_id": suite_id}


async def _cleanup(engine: AsyncEngine, agent_id: str) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "DELETE FROM discovery_drafts WHERE suite_id IN "
                "(SELECT id FROM suites WHERE agent_id = :id)"
            ),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "DELETE FROM scenario_metrics WHERE scenario_id IN "
                "(SELECT sc.id FROM scenarios sc JOIN suites s ON s.id = sc.suite_id "
                " WHERE s.agent_id = :id)"
            ),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "DELETE FROM scenarios WHERE suite_id IN "
                "(SELECT id FROM suites WHERE agent_id = :id)"
            ),
            {"id": agent_id},
        )
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})


async def test_generate_writes_n_drafts_including_a_negative_case() -> None:
    engine = _test_engine()
    fake = _client_returning(
        _FakeResponse(
            parsed=GenerationResponse(
                drafts=[_draft("Balance inquiry"), _draft("Impersonation attempt", kind="negative")]
            )
        )
    )
    app.dependency_overrides[get_generation_client_factory] = lambda: lambda: fake
    try:
        async with await _client() as client:
            info = await _make_agent_and_suite(client)
            response = await client.post(
                f"/v1/suites/{info['suite_id']}/scenarios:generate",
                json={"source": "agent_prompt", "count": 2},
            )
            assert response.status_code == 201
            body = response.json()
            assert len(body["drafts"]) == 2
            kinds = {d["kind"] for d in body["drafts"]}
            assert "negative" in kinds
            assert "cost" in body
            for d in body["drafts"]:
                assert d["personaId"] is not None
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT count(*) FROM discovery_drafts WHERE suite_id = :id"),
                    {"id": info["suite_id"]},
                )
            ).scalar_one()
            assert rows == 2
    finally:
        app.dependency_overrides.pop(get_generation_client_factory, None)
        try:
            await _cleanup(engine, info["agent_id"])
        finally:
            await engine.dispose()


async def test_generate_missing_agent_prompt_is_422() -> None:
    engine = _test_engine()
    fake = _client_returning(_FakeResponse(parsed=GenerationResponse(drafts=[])))
    app.dependency_overrides[get_generation_client_factory] = lambda: lambda: fake
    try:
        async with await _client() as client:
            info = await _make_agent_and_suite(client, prompt=None, description=None)
            response = await client.post(
                f"/v1/suites/{info['suite_id']}/scenarios:generate",
                json={"source": "agent_prompt", "count": 2},
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "validation_error"
    finally:
        app.dependency_overrides.pop(get_generation_client_factory, None)
        try:
            await _cleanup(engine, info["agent_id"])
        finally:
            await engine.dispose()


async def test_generate_count_out_of_range_is_422() -> None:
    """No fake client override here on purpose. `count` is validated by the
    request schema, but FastAPI's solve_dependencies calls every Depends()
    for a request -- including get_generation_client_factory -- in one
    pass regardless of whether a sibling (the body) ends up failing
    validation; an earlier version of this endpoint called
    build_judge_client() eagerly inside that dependency and crashed this
    exact case with a raw KeyError on missing GCP credentials instead of
    a clean 422 (the same bug class B2.7-10 shipped once already). This
    test is what caught it: get_generation_client_factory now returns the
    *uncalled* factory (cheap, can't fail), and real client construction
    happens inside generate_scenarios's own body, which never runs once
    body validation has failed."""
    engine = _test_engine()
    async with await _client() as client:
        info = await _make_agent_and_suite(client)
        response = await client.post(
            f"/v1/suites/{info['suite_id']}/scenarios:generate",
            json={"source": "agent_prompt", "count": 21},
        )
        assert response.status_code == 422
    await _cleanup(engine, info["agent_id"])
    await engine.dispose()


async def test_generate_unresolved_persona_falls_back_rather_than_erroring() -> None:
    engine = _test_engine()
    fake = _client_returning(
        _FakeResponse(
            parsed=GenerationResponse(
                drafts=[
                    _draft("Made up persona case", persona_name="Nonexistent Persona Xyz"),
                    _draft("Refusal case", kind="negative"),
                ]
            )
        )
    )
    app.dependency_overrides[get_generation_client_factory] = lambda: lambda: fake
    try:
        async with await _client() as client:
            info = await _make_agent_and_suite(client)
            response = await client.post(
                f"/v1/suites/{info['suite_id']}/scenarios:generate",
                json={"source": "agent_prompt", "count": 2},
            )
            assert response.status_code == 201
            body = response.json()
            made_up = next(d for d in body["drafts"] if d["name"] == "Made up persona case")
            assert made_up["personaId"] is not None
            assert made_up["persona"] != "Nonexistent Persona Xyz"
    finally:
        app.dependency_overrides.pop(get_generation_client_factory, None)
        try:
            await _cleanup(engine, info["agent_id"])
        finally:
            await engine.dispose()


async def test_accept_draft_twice_is_201_then_200_with_runnable_scenario() -> None:
    engine = _test_engine()
    fake = _client_returning(
        _FakeResponse(
            parsed=GenerationResponse(
                drafts=[_draft("Card block"), _draft("Refusal case", kind="negative")]
            )
        )
    )
    app.dependency_overrides[get_generation_client_factory] = lambda: lambda: fake
    try:
        async with await _client() as client:
            info = await _make_agent_and_suite(client)
            generate_response = await client.post(
                f"/v1/suites/{info['suite_id']}/scenarios:generate",
                json={"source": "agent_prompt", "count": 2},
            )
            draft_id = generate_response.json()["drafts"][0]["draftId"]

            first = await client.post(
                f"/v1/suites/{info['suite_id']}/scenarios", json={"fromDraftId": draft_id}
            )
            assert first.status_code == 201
            scenario_id = first.json()["id"]

            second = await client.post(
                f"/v1/suites/{info['suite_id']}/scenarios", json={"fromDraftId": draft_id}
            )
            assert second.status_code == 200
            assert second.json()["id"] == scenario_id

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT persona_id, goal, script, "
                            "(SELECT count(*) FROM scenario_metrics WHERE scenario_id = sc.id) "
                            " AS metric_count "
                            "FROM scenarios sc WHERE sc.id = :id"
                        ),
                        {"id": scenario_id},
                    )
                )
                .mappings()
                .one()
            )
            assert row["persona_id"] is not None
            assert row["goal"]
            assert row["script"] is not None and row["script"].get("openingLine")
            assert row["metric_count"] == 1

            draft_row = (
                (
                    await conn.execute(
                        text("SELECT added_scenario_id FROM discovery_drafts WHERE draft_id = :id"),
                        {"id": draft_id},
                    )
                )
                .mappings()
                .one()
            )
            assert str(draft_row["added_scenario_id"]) == scenario_id
    finally:
        app.dependency_overrides.pop(get_generation_client_factory, None)
        try:
            await _cleanup(engine, info["agent_id"])
        finally:
            await engine.dispose()


async def test_accept_cross_project_draft_is_404() -> None:
    engine = _test_engine()
    fake = _client_returning(
        _FakeResponse(
            parsed=GenerationResponse(
                drafts=[_draft("Some case"), _draft("Refusal case", kind="negative")]
            )
        )
    )
    app.dependency_overrides[get_generation_client_factory] = lambda: lambda: fake
    other_project_id = uuid.uuid4()
    other_project_headers = auth_headers(project_id=other_project_id)
    try:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("INSERT INTO projects (id, name) VALUES (:id, 'Other Generation Project')"),
                {"id": other_project_id},
            )
        async with await _client() as client:
            info = await _make_agent_and_suite(client)
            generate_response = await client.post(
                f"/v1/suites/{info['suite_id']}/scenarios:generate",
                json={"source": "agent_prompt", "count": 2},
            )
            draft_id = generate_response.json()["drafts"][0]["draftId"]

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=other_project_headers
        ) as other_client:
            response = await other_client.post(
                f"/v1/suites/{info['suite_id']}/scenarios", json={"fromDraftId": draft_id}
            )
            # A foreign project can't even see the suite to attach into.
            assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_generation_client_factory, None)
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM projects WHERE id = :id"), {"id": other_project_id}
            )
        try:
            await _cleanup(engine, info["agent_id"])
        finally:
            await engine.dispose()


async def test_delete_accepted_scenario_and_suite_still_succeeds() -> None:
    engine = _test_engine()
    fake = _client_returning(
        _FakeResponse(
            parsed=GenerationResponse(
                drafts=[_draft("Card block"), _draft("Refusal case", kind="negative")]
            )
        )
    )
    app.dependency_overrides[get_generation_client_factory] = lambda: lambda: fake
    try:
        async with await _client() as client:
            info = await _make_agent_and_suite(client)
            generate_response = await client.post(
                f"/v1/suites/{info['suite_id']}/scenarios:generate",
                json={"source": "agent_prompt", "count": 2},
            )
            draft_id = generate_response.json()["drafts"][0]["draftId"]
            accept = await client.post(
                f"/v1/suites/{info['suite_id']}/scenarios", json={"fromDraftId": draft_id}
            )
            scenario_id = accept.json()["id"]

            delete_response = await client.delete(f"/v1/scenarios/{scenario_id}")
            assert delete_response.status_code == 204

            delete_suite_response = await client.delete(f"/v1/suites/{info['suite_id']}")
            assert delete_suite_response.status_code == 204
    finally:
        app.dependency_overrides.pop(get_generation_client_factory, None)
        try:
            await _cleanup(engine, info["agent_id"])
        finally:
            await engine.dispose()


async def test_generate_from_discovery_run_uses_seeded_intents() -> None:
    engine = _test_engine()
    fake = _client_returning(
        _FakeResponse(
            parsed=GenerationResponse(
                drafts=[_draft("From discovery"), _draft("Refusal case", kind="negative")]
            )
        )
    )
    app.dependency_overrides[get_generation_client_factory] = lambda: lambda: fake
    discovery_run_id = uuid.uuid4()
    try:
        async with await _client() as client:
            info = await _make_agent_and_suite(client)
            async with engine.connect() as conn, conn.begin():
                await conn.execute(
                    text(
                        "INSERT INTO runs (id, type, status, agent_id, created_by_user_id) "
                        "VALUES (:id, 'discovery', 'completed', :agent_id, 'user-1')"
                    ),
                    {"id": discovery_run_id, "agent_id": uuid.UUID(info["agent_id"])},
                )
                await conn.execute(
                    text(
                        "INSERT INTO discovery_intents (run_id, name, state, path) "
                        "VALUES (:run_id, 'check_balance', 'explored', '/balance')"
                    ),
                    {"run_id": discovery_run_id},
                )

            response = await client.post(
                f"/v1/suites/{info['suite_id']}/scenarios:generate",
                json={
                    "source": "discovery_run",
                    "discoveryRunId": str(discovery_run_id),
                    "count": 2,
                },
            )
            assert response.status_code == 201
            assert len(response.json()["drafts"]) == 2
    finally:
        app.dependency_overrides.pop(get_generation_client_factory, None)
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM discovery_intents WHERE run_id = :id"), {"id": discovery_run_id}
            )
        try:
            await _cleanup(engine, info["agent_id"])
        finally:
            await engine.dispose()
