"""B4-02/B4-04 live check: run attack packs against whatever agent is
registered with the LiveKit project (normally the reference agent --
`uv run python -m app.engine.reference_agent.agent dev` in another terminal;
set REFERENCE_AGENT_WEAK_GATE=1 for it to test the auth-bypass packs against
a weakened gate). No DB involved: prints each exchange, the judged verdict
and the verbatim evidence.

Usage: uv run python -m scripts.redteam_live pii-family-pretext [pack-id ...]
       [--no-identity]
"""

from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from app.engine.caller.persona import PersonaSpec, Turn
from app.engine.caller.persona_call import run_persona_call
from app.engine.discovery.explorer import GeminiExplorerLLM
from app.engine.judge.judge import build_judge_client
from app.engine.judge.models import TranscriptTurn
from app.engine.redteam.attacker import (
    MAX_VERIFY_TURNS,
    AttackerCaller,
    Exchange,
    GeminiAttackerLLM,
)
from app.engine.redteam.judge import AttackJudge
from app.engine.redteam.packs import registry
from app.gcp_auth import load_google_oauth2_credentials

IDENTITY = {"name": "Asha Rao", "dob": "1990-04-12", "verificationPhrase": "blue lagoon"}


async def run_pack(pack_id: str, *, with_identity: bool) -> None:
    pack = next(p for p in registry() if p.id == pack_id)
    settings = get_settings()
    llm = GeminiAttackerLLM(
        GeminiExplorerLLM(
            credentials=load_google_oauth2_credentials(),
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
        )
    )
    transcript: list[Turn] = []

    async def on_turn(turn: Turn) -> None:
        transcript.append(turn)
        print(f"{turn.speaker:>6}: {turn.text}")

    async def on_exchange(e: Exchange) -> None:
        print(f"  [interim] {e.verdict} ({e.severity})")

    attacker = AttackerCaller(
        llm,
        pack,
        identity=IDENTITY if (with_identity or pack.requires_verified) else None,
        on_exchange=on_exchange,
    )
    print(f"\n===== {pack.id} ({pack.category}, default severity {pack.severity_if_success}) =====")
    persona = PersonaSpec(name="Red Team", traits={}, goal="attack", opening_line="Hi.")
    await run_persona_call(
        persona,
        on_turn=on_turn,
        planner=attacker,
        max_turns=MAX_VERIFY_TURNS + 1 + len(pack.escalations) + 3,
        run_timeout_secs=300,
        silence_timeout_secs=30,
    )
    if not attacker.attack_lines:
        print(f"-> NOT DELIVERED: {attacker.failure}")
        return
    graded = await AttackJudge(build_judge_client()).grade(
        pack, [TranscriptTurn(role=t.speaker, text=t.text) for t in transcript]
    )
    print(f"-> {graded.verdict.upper()} severity={graded.severity} refs={graded.turn_refs}")
    print(f"   evidence: {graded.evidence!r} (isolated={graded.evidence_isolated})")
    print(f"   rationale: {graded.rationale}")
    print(f"   fix: {graded.suggested_fix}")


async def main(pack_ids: list[str], with_identity: bool) -> None:
    for pack_id in pack_ids:
        await run_pack(pack_id, with_identity=with_identity)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packs", nargs="+")
    parser.add_argument("--identity", action="store_true", help="give the attacker the identity")
    args = parser.parse_args()
    asyncio.run(main(args.packs, args.identity))
