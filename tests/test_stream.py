"""B0-07 tests.

The replay/live/reconnect acceptance test drives event_stream() directly
(not through an HTTP/ASGI transport): whether httpx's ASGITransport streams
incrementally or buffers-to-completion is version-dependent, and this
generator never terminates on its own in live mode — betting that test on
transport behavior risks a hang instead of a clear failure. `anext()` on the
generator directly tests the exact ticket criteria with no chunk-arrival race.

That leaves a real gap: the generator being correct doesn't prove the HTTP
route (middleware -> StreamingResponse -> generator) actually delivers it —
which is exactly where this endpoint's real bugs lived during development
(BaseHTTPMiddleware buffering, a since-removed is_disconnected() poll that
stalled the loop). test_stream_via_real_http_path_completes_on_done covers
that path, sidestepping the transport-buffering question by using a run
that reaches `done` so a plain non-streaming request completes naturally.
"""

import asyncio
import uuid

from sqlalchemy import text

from app.api.runs import event_stream
from app.config import get_settings
from app.db import get_engine
from app.events import done_event, emit, turn_event
from app.main import app
from tests.conftest import _test_engine, requires_test_db

pytestmark = requires_test_db


def _parse_frame(frame: str) -> tuple[int, str]:
    lines = frame.strip("\n").splitlines()
    seq = next(int(line.removeprefix("id:").strip()) for line in lines if line.startswith("id:"))
    event_type = next(
        line.removeprefix("event:").strip() for line in lines if line.startswith("event:")
    )
    return seq, event_type


async def _anext_with_timeout(gen: object, timeout: float = 15.0) -> str:
    return await asyncio.wait_for(gen.__anext__(), timeout=timeout)  # type: ignore[attr-defined]


async def test_stream_replay_then_live_then_reconnect() -> None:
    engine = _test_engine()
    agent_id = uuid.uuid4()
    run_id = uuid.uuid4()

    try:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO agents (id, name, transport, created_by_user_id) "
                    "VALUES (:id, 'Stream Test Agent', 'web', 'user-1')"
                ),
                {"id": agent_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO runs (id, type, agent_id, created_by_user_id) "
                    "VALUES (:id, 'simulation', :agent_id, 'user-1')"
                ),
                {"id": run_id, "agent_id": agent_id},
            )

        # Seed 3 events up front.
        async with engine.connect() as conn, conn.begin():
            for i in range(3):
                await emit(conn, run_id, turn_event(index=i, role="agent", text=f"turn {i}"))

        # --- Connect fresh (after_seq=0): expect exactly the 3 replayed events ---
        gen = event_stream(engine, run_id, after_seq=0)
        seqs = []
        for _ in range(3):
            seq, event_type = _parse_frame(await _anext_with_timeout(gen))
            assert event_type == "turn"
            seqs.append(seq)
        assert seqs == [1, 2, 3]

        # --- Insert 2 more while the generator is still live; expect them live ---
        async with engine.connect() as conn, conn.begin():
            await emit(conn, run_id, turn_event(index=3, role="agent", text="turn 3"))
            await emit(conn, run_id, turn_event(index=4, role="agent", text="turn 4"))

        live_seqs = []
        for _ in range(2):
            seq, _ = _parse_frame(await _anext_with_timeout(gen))
            live_seqs.append(seq)
        assert live_seqs == [4, 5]
        await gen.aclose()

        # --- Reconnect with after_seq=3 (Last-Event-ID: 3): expect exactly 4, 5 ---
        reconnect_gen = event_stream(engine, run_id, after_seq=3)
        reconnect_seqs = []
        for _ in range(2):
            seq, _ = _parse_frame(await _anext_with_timeout(reconnect_gen))
            reconnect_seqs.append(seq)
        assert reconnect_seqs == [4, 5]
        await reconnect_gen.aclose()
    finally:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM run_events WHERE run_id = :run_id"), {"run_id": run_id}
            )
            await conn.execute(text("DELETE FROM runs WHERE id = :run_id"), {"run_id": run_id})
            await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
        await engine.dispose()


async def test_stream_unknown_run_is_404() -> None:
    from httpx import ASGITransport, AsyncClient

    test_engine = _test_engine()
    app.dependency_overrides[get_engine] = lambda: test_engine
    try:
        transport = ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {get_settings().python_service_token}"}
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/v1/runs/{uuid.uuid4()}/stream", headers=headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    finally:
        app.dependency_overrides.pop(get_engine, None)
        await test_engine.dispose()


async def test_stream_via_real_http_path_completes_on_done() -> None:
    """Drives the full stack (ServiceTokenMiddleware -> StreamingResponse ->
    event_stream) via ASGITransport, not just the bare generator. This is
    the test that would have caught B0-07's actual bug: an earlier version
    of ServiceTokenMiddleware subclassed BaseHTTPMiddleware, which buffers
    streaming bodies, and a since-removed request.is_disconnected() poll —
    both invisible to a test that drives the generator directly. Using a
    run that reaches `done` lets a plain (non-streaming) request complete
    naturally instead of betting on ASGITransport's streaming behavior.
    """
    from httpx import ASGITransport, AsyncClient

    engine = _test_engine()
    app.dependency_overrides[get_engine] = lambda: engine
    agent_id = uuid.uuid4()
    run_id = uuid.uuid4()

    try:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO agents (id, name, transport, created_by_user_id) "
                    "VALUES (:id, 'HTTP Stream Test Agent', 'web', 'user-1')"
                ),
                {"id": agent_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO runs (id, type, agent_id, created_by_user_id) "
                    "VALUES (:id, 'simulation', :agent_id, 'user-1')"
                ),
                {"id": run_id, "agent_id": agent_id},
            )
            await emit(conn, run_id, turn_event(index=0, role="agent", text="hi"))
            await emit(conn, run_id, done_event(score=0.9, result_badge="pass"))

        transport = ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {get_settings().python_service_token}"}
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/v1/runs/{run_id}/stream", headers=headers, timeout=15.0)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = response.text
        assert "event: turn" in body
        assert "event: done" in body
        assert body.index("event: turn") < body.index("event: done")
    finally:
        app.dependency_overrides.pop(get_engine, None)
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM run_events WHERE run_id = :run_id"), {"run_id": run_id}
            )
            await conn.execute(text("DELETE FROM runs WHERE id = :run_id"), {"run_id": run_id})
            await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
        await engine.dispose()
