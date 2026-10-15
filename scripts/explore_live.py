"""B5-02/03 live check: run the discovery explorer against whatever agent is
registered with the LiveKit project (normally the reference agent --
`uv run python -m app.engine.reference_agent.agent dev` in another terminal)
and print the map it built. No DB involved.

Usage: uv run python -m scripts.explore_live [--wrong-identity]
"""

from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from app.engine.caller.persona import PersonaSpec, Turn
from app.engine.caller.persona_call import run_persona_call
from app.engine.discovery.explorer import (
    DEFAULT_TURN_BUDGET,
    OPENING_LINE,
    ExplorerCaller,
    GeminiExplorerLLM,
    propose_candidates,
)
from app.engine.discovery.graph import FlowGraph
from app.gcp_auth import load_google_oauth2_credentials

RIGHT = {"name": "Asha Rao", "dob": "1990-04-12", "verificationPhrase": "blue lagoon"}
WRONG = {"name": "Rahul Mehta", "dob": "1978-03-03", "verificationPhrase": "red balloon"}


async def main(identity: dict[str, str]) -> None:
    settings = get_settings()
    llm = GeminiExplorerLLM(
        credentials=load_google_oauth2_credentials(),
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
    )
    graph = FlowGraph()
    candidates = await propose_candidates(
        llm, name="Reference agent", description=None, prompt=None
    )

    async def on_observation(index, obs, result) -> None:  # type: ignore[no-untyped-def]
        gate = f" gate={obs.gate_result}" if obs.gate_result else ""
        print(f"  [observe @{index}] {obs.node_key} ({obs.kind}){gate} {obs.reason or ''}")

    explorer = ExplorerCaller(
        llm,
        identity=identity,
        candidates=candidates,
        graph=graph,
        turn_budget=DEFAULT_TURN_BUDGET,
        on_observation=on_observation,
    )

    async def on_turn(turn: Turn) -> None:
        print(f"{turn.speaker:>6}: {turn.text}")

    persona = PersonaSpec(name="Cadence Explorer", traits={}, goal="map", opening_line=OPENING_LINE)
    result = await run_persona_call(
        persona,
        on_turn=on_turn,
        planner=explorer,
        max_turns=DEFAULT_TURN_BUDGET,
        run_timeout_secs=600,
    )
    print(f"\ncall ended: {result.end_reason}; caller turns={explorer.caller_turns}")
    print("nodes:")
    graph.finalize()
    for n in graph.nodes:
        print(f"  {n.key:<22} {n.kind:<9} {n.state:<8} {n.blocked_reason or ''}")
    print("edges:", graph.edges)
    print("intents:")
    for i in graph.intents(explorer.candidates):
        print(f"  {i.name:<34} {i.state:<8} {i.path}  {i.reason or ''}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrong-identity", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(WRONG if args.wrong_identity else RIGHT))
