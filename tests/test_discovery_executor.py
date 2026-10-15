"""B5-03: the discovery executor end to end against the test DB, with the
LiveKit call and the explorer model replaced by scripts. Everything else is
real: run creation (encrypted dummy identity), event log, materialization
into discovery_nodes/edges/intents, and the lifecycle/terminal status."""

import asyncio
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.engine.caller.latency_clock import LatencyClock
from app.engine.caller.persona import PersonaSpec, Turn
from app.engine.caller.persona_call import OnTurn, PersonaCallResult, TurnPlanner
from app.engine.discovery.explorer import ExplorerLLMOutput, ProposedCandidates
from app.engine.discovery.models import GateResult, NodeKind, TurnObservation
from app.engine.executor.discovery import run_discovery
from app.engine.livekit_creds import LiveKitCreds
from app.main import app
from app.workers.claim import ClaimedRun
from tests.conftest import _test_engine, auth_headers, requires_test_db

# Each emit is a real round trip to the (remote) test DB; a 14-turn run is
# well past the 120s default.
pytestmark = [requires_test_db, pytest.mark.timeout(600)]

_IDENTITY = {
    "name": "Asha Rao",
    "dob": "1990-04-12",
    "account": "0091488823671",
    "verificationPhrase": "blue lagoon",
}
_REJECTED = "The agent said the details did not match its records."


def _out(
    key: str,
    kind: NodeKind,
    gate: GateResult | None = None,
    reason: str | None = None,
    reached: str | None = None,
) -> ExplorerLLMOutput:
    obs = TurnObservation(
        node_key=key,
        label=key.replace("_", " ").title(),
        kind=kind,
        gate_result=gate,
        reason=reason,
    )
    return ExplorerLLMOutput(observation=obs, utterance="ok", reached_candidate=reached)


class ScriptedLLM:
    def __init__(self, outputs: list[ExplorerLLMOutput]) -> None:
        self._outputs = list(outputs)

    async def plan(self, system: str, prompt: str) -> ExplorerLLMOutput:
        return self._outputs.pop(0)

    async def propose(self, system: str, prompt: str) -> ProposedCandidates:
        raise AssertionError("agent has no description; defaults must be used")


def _scripted_call(agent_lines: list[str]):  # type: ignore[no-untyped-def]
    """A CallRunner that plays `agent_lines` against the real explorer."""

    async def run_call(
        persona: PersonaSpec,
        *,
        on_turn: OnTurn | None,
        latency_clock: LatencyClock | None,
        cancel_event: asyncio.Event | None,
        run_id: uuid.UUID | None,
        livekit: LiveKitCreds | None,
        planner: TurnPlanner | None,
        max_turns: int,
        run_timeout_secs: float,
        silence_timeout_secs: float | None,
    ) -> PersonaCallResult:
        assert planner is not None and on_turn is not None
        transcript: list[Turn] = []
        for line in agent_lines:
            agent = Turn("agent", line)
            transcript.append(agent)
            await on_turn(agent)
            planned = await planner.next_turn(transcript)
            caller = Turn("caller", planned.utterance)
            transcript.append(caller)
            await on_turn(caller)
            if planned.call_complete:
                break
        return PersonaCallResult(transcript=transcript, end_reason="caller_ended")

    return run_call


async def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=auth_headers()
    )


async def _make_agent(engine: AsyncEngine) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, transport, max_concurrency, created_by_user_id) "
                "VALUES (:id, 'Discovery Agent', 'web', 5, 'user-1')"
            ),
            {"id": agent_id},
        )
    return agent_id


async def _start_run(engine: AsyncEngine, agent_id: uuid.UUID) -> ClaimedRun:
    async with await _client() as client:
        response = await client.post(
            "/v1/discovery/runs", json={"agentId": str(agent_id), "dummyIdentity": _IDENTITY}
        )
    assert response.status_code == 202
    run_id = uuid.UUID(response.json()["runId"])
    async with engine.connect() as conn, conn.begin():
        config = (
            await conn.execute(text("SELECT config FROM runs WHERE id = :id"), {"id": run_id})
        ).scalar_one()
        await conn.execute(
            text("UPDATE runs SET status = 'claimed' WHERE id = :id"), {"id": run_id}
        )
    return ClaimedRun(
        id=run_id, type="discovery", agent_id=agent_id, scenario_id=None, config=config
    )


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        run_ids = (
            (await conn.execute(text("SELECT id FROM runs WHERE agent_id = :a"), {"a": agent_id}))
            .scalars()
            .all()
        )
        profile_ids = (
            (
                await conn.execute(
                    text("SELECT config->>'testProfileId' FROM runs WHERE agent_id = :a"),
                    {"a": agent_id},
                )
            )
            .scalars()
            .all()
        )
        for table in (
            "discovery_edges",
            "discovery_intents",
            "discovery_nodes",
            "run_events",
            "turns",
        ):
            await conn.execute(
                text(f"DELETE FROM {table} WHERE run_id = ANY(:ids)"), {"ids": run_ids}
            )
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :a"), {"a": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :a"), {"a": agent_id})
        for pid in profile_ids:
            if pid:
                await conn.execute(
                    text("DELETE FROM sensitive_access_log WHERE resource_id = :p"),
                    {"p": uuid.UUID(pid)},
                )
                await conn.execute(
                    text("DELETE FROM test_profiles WHERE id = :p"), {"p": uuid.UUID(pid)}
                )
    await engine.dispose()


async def _events(engine: AsyncEngine, run_id: uuid.UUID, type_: str) -> list[dict[str, object]]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT data FROM run_events WHERE run_id = :r AND type = :t ORDER BY seq"
                    ),
                    {"r": run_id, "t": type_},
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def test_correct_identity_maps_the_whole_flow_progressively() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    try:
        claimed = await _start_run(engine, agent_id)
        llm = ScriptedLLM(
            [
                _out("greeting", "greeting"),
                _out("verification", "gate", "pending"),
                _out("main_menu", "menu", "passed"),
                _out("balance", "intent", reached="Check account balance"),
                _out("card_block", "intent", reached="Report a lost or stolen card"),
                _out("branch_info", "intent", reached="Find branch hours and location"),
                _out("human_handoff", "handoff", reached="Talk to a human agent"),
            ]
        )
        await run_discovery(
            engine,
            claimed,
            run_call=_scripted_call([f"agent line {i}" for i in range(7)]),
            make_llm=lambda: llm,
        )

        async with engine.connect() as conn:
            run = (
                (
                    await conn.execute(
                        text("SELECT status, end_reason, metrics FROM runs WHERE id = :r"),
                        {"r": claimed.id},
                    )
                )
                .mappings()
                .one()
            )
            nodes = (
                (
                    await conn.execute(
                        text(
                            "SELECT node_id, state, blocked_reason "
                            "FROM discovery_nodes WHERE run_id = :r"
                        ),
                        {"r": claimed.id},
                    )
                )
                .mappings()
                .all()
            )
            edges = (
                await conn.execute(
                    text("SELECT from_node, to_node FROM discovery_edges WHERE run_id = :r"),
                    {"r": claimed.id},
                )
            ).all()
            intents = (
                (
                    await conn.execute(
                        text("SELECT name, state, path FROM discovery_intents WHERE run_id = :r"),
                        {"r": claimed.id},
                    )
                )
                .mappings()
                .all()
            )
            turns = (
                await conn.execute(
                    text("SELECT count(*) FROM turns WHERE run_id = :r"), {"r": claimed.id}
                )
            ).scalar_one()

        assert run["status"] == "completed" and run["end_reason"] == "completed"
        # Reached topics are named after the planned candidates, not whatever
        # the model called them (balance -> "Check account balance").
        assert {n["node_id"] for n in nodes} == {
            "greeting",
            "verification",
            "main_menu",
            "check_account_balance",
            "report_a_lost_or_stolen_card",
            "find_branch_hours_and_location",
            "talk_to_a_human_agent",
        }
        assert all(n["state"] == "mapped" for n in nodes)
        edge_set = {tuple(e) for e in edges}
        assert ("greeting", "verification") in edge_set
        # Hub-and-spoke: topics are reached from the menu, not chained.
        assert ("main_menu", "report_a_lost_or_stolen_card") in edge_set
        assert {i["name"]: i["state"] for i in intents} == {
            "Check account balance": "mapped",
            "Report a lost or stolen card": "mapped",
            "Find branch hours and location": "mapped",
            "Talk to a human agent": "mapped",
        }
        paths = {i["name"]: i["path"] for i in intents}
        assert paths["Report a lost or stolen card"] == (
            "Greeting → Verification → Main Menu → Report a lost or stolen card"
        )
        assert turns == 14

        # Progressive: the first node event precedes the second, and a node
        # carries its incoming edges.
        node_events = await _events(engine, claimed.id, "node")
        assert [e["nodeId"] for e in node_events[:2]] == ["greeting", "verification"]
        assert node_events[1]["fromNodes"] == ["greeting"]

        done = (await _events(engine, claimed.id, "done"))[0]
        result = done["discoveryResult"]
        assert isinstance(result, dict)
        assert result["nodesMapped"] == 7 and result["nodesBlocked"] == 0
        assert result["intentsMapped"] == 4 and result["unexplored"] == []
        assert run["metrics"]["discoveryResult"] == result
    finally:
        await _cleanup(engine, agent_id)


async def test_wrong_identity_blocks_the_gate_with_a_reason_and_still_completes() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    try:
        claimed = await _start_run(engine, agent_id)
        llm = ScriptedLLM(
            [
                _out("greeting", "greeting"),
                _out("verification", "gate", "pending"),
                _out("verification", "gate", "rejected", _REJECTED),
                _out("verification", "gate", "rejected", _REJECTED),
                _out("verification", "gate", "rejected", _REJECTED),
            ]
        )
        await run_discovery(
            engine,
            claimed,
            run_call=_scripted_call([f"line {i}" for i in range(6)]),
            make_llm=lambda: llm,
        )

        async with engine.connect() as conn:
            status = (
                await conn.execute(text("SELECT status FROM runs WHERE id = :r"), {"r": claimed.id})
            ).scalar_one()
            gate = (
                (
                    await conn.execute(
                        text(
                            "SELECT state, blocked_reason FROM discovery_nodes "
                            "WHERE run_id = :r AND node_id = 'verification'"
                        ),
                        {"r": claimed.id},
                    )
                )
                .mappings()
                .one()
            )
            intents = (
                (
                    await conn.execute(
                        text("SELECT name, state, reason FROM discovery_intents WHERE run_id = :r"),
                        {"r": claimed.id},
                    )
                )
                .mappings()
                .all()
            )

        assert status == "completed"  # a wrong identity is not an error
        assert gate["state"] == "blocked" and gate["blocked_reason"] == _REJECTED
        # Every default topic sits behind the gate, blocked, with the why.
        assert len(intents) == 4
        assert all(i["state"] == "blocked" for i in intents)
        assert all(_REJECTED in i["reason"] for i in intents)
        done = (await _events(engine, claimed.id, "done"))[0]
        assert done["discoveryResult"]["intentsBlocked"] == 4  # type: ignore[index]
    finally:
        await _cleanup(engine, agent_id)


async def test_an_agent_that_never_answers_fails_the_run_instead_of_completing_empty() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    try:
        claimed = await _start_run(engine, agent_id)
        await run_discovery(
            engine, claimed, run_call=_scripted_call([]), make_llm=lambda: ScriptedLLM([])
        )
        async with engine.connect() as conn:
            status = (
                await conn.execute(text("SELECT status FROM runs WHERE id = :r"), {"r": claimed.id})
            ).scalar_one()
        assert status == "failed"
        assert (await _events(engine, claimed.id, "error"))[0]["code"] == "no_observations"
    finally:
        await _cleanup(engine, agent_id)


async def _insert_flagged_source(engine: AsyncEngine, agent_id: uuid.UUID) -> uuid.UUID:
    """A completed discovery run with a recorded map, flagged as the demo
    replay -- inserted directly (a real one costs a live call)."""
    source_id = uuid.uuid4()
    node = {
        "nodeId": "greeting",
        "label": "Greeting",
        "x": 0,
        "y": 0,
        "state": "mapped",
        "fromNodes": [],
    }
    gate = {
        "nodeId": "verification",
        "label": "Identity verification",
        "x": 240,
        "y": 0,
        "state": "blocked",
        "blockedReason": _REJECTED,
        "fromNodes": ["greeting"],
    }
    intent = {
        "name": "Balance",
        "state": "blocked",
        "path": "Greeting → Identity verification → Balance",
        "reason": _REJECTED,
    }
    result = {
        "nodesMapped": 1,
        "nodesBlocked": 1,
        "intentsMapped": 0,
        "intentsBlocked": 1,
        "unexplored": [],
        "turnsUsed": 1,
    }
    events = [
        ("status", {"status": "running"}),
        ("turn", {"index": 0, "role": "agent", "text": "Hello, welcome.", "flagged": False}),
        ("node", node),
        ("turn", {"index": 1, "role": "caller", "text": "Hi.", "flagged": False}),
        ("node", gate),
        ("intent", intent),
        ("done", {"discoveryResult": result}),
    ]
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, type, status, agent_id, created_by_user_id, config, "
                "metrics, end_reason) VALUES (:id, 'discovery', 'completed', :a, 'user-1', "
                "CAST(:config AS jsonb), CAST(:metrics AS jsonb), 'completed')"
            ),
            {
                "id": source_id,
                "a": agent_id,
                "config": json.dumps({"demoReplay": True}),
                "metrics": json.dumps({"discoveryResult": result, "turnsCompleted": 2}),
            },
        )
        for seq, (type_, data) in enumerate(events, start=1):
            await conn.execute(
                text(
                    "INSERT INTO run_events (run_id, seq, type, data, ts) VALUES "
                    "(:r, :s, :t, CAST(:d AS jsonb), now() - make_interval(secs => :ago))"
                ),
                {"r": source_id, "s": seq, "t": type_, "d": json.dumps(data), "ago": 60 - seq * 5},
            )
    return source_id


async def test_demo_environment_replays_the_flagged_run_into_a_normal_new_run() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    try:
        source_id = await _insert_flagged_source(engine, agent_id)
        claimed = await _start_run(engine, agent_id)

        async def must_not_call(*args: object, **kwargs: object) -> PersonaCallResult:
            raise AssertionError("a demo replay must not place a live call")

        await run_discovery(
            engine,
            claimed,
            run_call=must_not_call,
            make_llm=lambda: ScriptedLLM([]),
            demo=True,
            replay_max_gap_secs=0,
        )

        async with engine.connect() as conn:
            run = (
                (
                    await conn.execute(
                        text("SELECT status, end_reason, config, metrics FROM runs WHERE id = :r"),
                        {"r": claimed.id},
                    )
                )
                .mappings()
                .one()
            )
            gate = (
                (
                    await conn.execute(
                        text(
                            "SELECT state, blocked_reason FROM discovery_nodes "
                            "WHERE run_id = :r AND node_id = 'verification'"
                        ),
                        {"r": claimed.id},
                    )
                )
                .mappings()
                .one()
            )
            intents = (
                await conn.execute(
                    text("SELECT name, state FROM discovery_intents WHERE run_id = :r"),
                    {"r": claimed.id},
                )
            ).all()
            edge_count = (
                await conn.execute(
                    text("SELECT count(*) FROM discovery_edges WHERE run_id = :r"),
                    {"r": claimed.id},
                )
            ).scalar_one()
            turn_count = (
                await conn.execute(
                    text("SELECT count(*) FROM turns WHERE run_id = :r"), {"r": claimed.id}
                )
            ).scalar_one()

        assert run["status"] == "completed" and run["end_reason"] == "completed"
        assert run["config"]["replayOf"] == str(source_id)
        assert run["metrics"]["discoveryResult"]["nodesBlocked"] == 1
        assert gate["state"] == "blocked" and gate["blocked_reason"] == _REJECTED
        assert [tuple(i) for i in intents] == [("Balance", "blocked")]
        assert edge_count == 1 and turn_count == 2
        # The new run's own log is the full vocabulary, in the recorded order.
        assert [e["nodeId"] for e in await _events(engine, claimed.id, "node")] == [
            "greeting",
            "verification",
        ]
    finally:
        await _cleanup(engine, agent_id)


async def test_demo_environment_without_a_flagged_run_explores_live() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    try:
        claimed = await _start_run(engine, agent_id)
        llm = ScriptedLLM([_out("human_handoff", "handoff", reached="Talk to a human agent")])
        await run_discovery(
            engine,
            claimed,
            run_call=_scripted_call(["connecting you to a human"]),
            make_llm=lambda: llm,
            demo=True,
        )
        async with engine.connect() as conn:
            config = (
                await conn.execute(text("SELECT config FROM runs WHERE id = :r"), {"r": claimed.id})
            ).scalar_one()
        assert "replayOf" not in config  # fell through to the live path
        assert (await _events(engine, claimed.id, "node"))[0]["nodeId"] == "talk_to_a_human_agent"
    finally:
        await _cleanup(engine, agent_id)


async def test_a_model_failure_mid_exploration_is_reported_not_passed_off_as_clean() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    try:
        claimed = await _start_run(engine, agent_id)

        class DiesAfterOne(ScriptedLLM):
            async def plan(self, system: str, prompt: str) -> ExplorerLLMOutput:
                if not self._outputs:
                    raise RuntimeError("vertex unavailable")
                return await super().plan(system, prompt)

        llm = DiesAfterOne([_out("greeting", "greeting")])
        await run_discovery(
            engine, claimed, run_call=_scripted_call(["hello", "who are you"]), make_llm=lambda: llm
        )
        async with engine.connect() as conn:
            run = (
                (
                    await conn.execute(
                        text("SELECT status, end_reason FROM runs WHERE id = :r"), {"r": claimed.id}
                    )
                )
                .mappings()
                .one()
            )
        assert run["status"] == "completed" and run["end_reason"] == "error"
        error = (await _events(engine, claimed.id, "error"))[0]
        assert error["code"] == "explorer_failed" and error["fatal"] is False
        done = (await _events(engine, claimed.id, "done"))[0]
        assert done["discoveryResult"]["unexplored"]  # type: ignore[index]
    finally:
        await _cleanup(engine, agent_id)


async def test_flag_demo_replay_only_accepts_a_completed_discovery_run() -> None:
    from scripts.flag_demo_replay import flag

    engine = _test_engine()
    agent_id = await _make_agent(engine)
    try:
        queued = await _start_run(engine, agent_id)  # claimed, not completed
        assert await flag(engine, queued.id, on=True) == 1
        assert await flag(engine, uuid.uuid4(), on=True) == 1  # no such run

        source_id = await _insert_flagged_source(engine, agent_id)
        assert await flag(engine, source_id, on=False) == 0
        async with engine.connect() as conn:
            config = (
                await conn.execute(text("SELECT config FROM runs WHERE id = :r"), {"r": source_id})
            ).scalar_one()
        assert config["demoReplay"] is False
        assert await flag(engine, source_id, on=True) == 0
    finally:
        await _cleanup(engine, agent_id)
