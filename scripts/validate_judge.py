"""B2-05 validation: FinalJudge verdicts vs. hand labels on 3 recorded calls.

Not a pytest test -- a real model call is non-deterministic and costs
money/latency, so this is a one-off proof the judge is calibrated
correctly, run by hand. B2-06's golden eval harness (`evals/`,
`pytest -m judge_evals`) is where this kind of check becomes a persisted,
CI-enforced gate with ~30 labeled transcripts instead of 3.

The 3 transcripts are real recordings from this session's B2-02/B2-04
caller runs against the reference agent (see /tmp/persona_call2.log,
/tmp/persona_call.log, /tmp/scripted_call_transcript.log):
  A: identity verifies cleanly, card blocked, replacement confirmed.
  B: agent asks for verification repeatedly but the call is cut off
     mid-loop -- never resolves, no next-steps confirmation.
  C: agent rejects the (correct) identity as non-matching and the caller
     leaves without retrying -- verification never succeeds, no next steps.

Run with `uv run python -m scripts.validate_judge`.
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from google.oauth2 import service_account
from loguru import logger

from app.engine.judge.judge import FinalJudge, build_vertex_client
from app.engine.judge.models import AssertionSpec, TranscriptTurn

load_dotenv()

ASSERTIONS = [
    AssertionSpec(
        id="a1",
        name="Requests identity verification before action",
        description=(
            "Before performing any account action (checking balance, blocking a "
            "card, etc.), the agent asks the caller for identifying details "
            "(e.g. name, date of birth, security phrase) and does not proceed "
            "on the strength of a bare request alone."
        ),
        distinguish_from=(
            "Not satisfied by a generic greeting ('Thank you for calling') -- "
            "the agent must actually ask for specific identifying details. Also "
            "not the same as successful verification: asking counts even if the "
            "provided details are then rejected."
        ),
    ),
    AssertionSpec(
        id="a2",
        name="Confirms next steps before ending the call",
        description=(
            "Before the call ends, the agent confirms to the caller what "
            "happens next / what was actually done (e.g. 'your card is "
            "blocked, a replacement will arrive in N days') in relation to "
            "what the caller originally asked for."
        ),
        distinguish_from=(
            "A generic closing ('anything else I can help with?', 'have a "
            "great day') does NOT satisfy this -- it must confirm the outcome "
            "of the caller's actual request, not just be a pleasant sign-off."
        ),
    ),
]

TRANSCRIPT_A = [
    TranscriptTurn(
        role="caller",
        text="Hi, I think I've lost my debit card and I need it blocked right away.",
    ),
    TranscriptTurn(
        role="agent",
        text="Hello. Thank you for calling Cadence Bank before I can help you with your",
    ),
    TranscriptTurn(role="caller", text="Yes, I understand. My name is Asha Rao."),
    TranscriptTurn(
        role="agent",
        text=(
            "I can certainly help with that to confirm your identity. First, "
            "can you please provide?"
        ),
    ),
    TranscriptTurn(
        role="caller",
        text="My date of birth is April 12th, 1990, and my security phrase is 'blue lagoon'.",
    ),
    TranscriptTurn(role="agent", text="Thank you. And what is your date of birth?"),
    TranscriptTurn(role="caller", text="It's April 12th, 1990."),
    TranscriptTurn(
        role="agent",
        text=(
            "Your debit card has been blocked and a replacement will be mailed "
            "within five business days. Is there anything else? I can help you with"
        ),
    ),
    TranscriptTurn(role="caller", text="No, that's everything. Thank you so much for your help!"),
    TranscriptTurn(role="agent", text="You're welcome. Have a great day."),
]
EXPECTED_A = {"a1": "passed", "a2": "passed"}

TRANSCRIPT_B = [
    TranscriptTurn(
        role="caller",
        text="Hi, I think I've lost my debit card and I need it blocked right away.",
    ),
    TranscriptTurn(
        role="agent",
        text="Hello. Thank you for calling Cadence Bank before I can assist you.",
    ),
    TranscriptTurn(role="caller", text="Yes, my name is Priya Sharma."),
    TranscriptTurn(
        role="agent",
        text=(
            "I understand and I can help with that first. Could you please "
            "State your full name for verification?"
        ),
    ),
    TranscriptTurn(role="caller", text="Yes, it's Priya Sharma."),
    TranscriptTurn(role="agent", text="Thank you Priya. And what is your date of birth?"),
    TranscriptTurn(role="caller", text="It's October 12th, 1985."),
    TranscriptTurn(role="agent", text="Got it. And what is your date of birth?"),
    TranscriptTurn(role="caller", text="It's October 12th, 1985."),
    TranscriptTurn(role="agent", text="Thank you."),
    TranscriptTurn(role="caller", text="So, about my card, can you block it for me please?"),
    TranscriptTurn(role="agent", text="I'm sorry those details didn't match our records."),
    TranscriptTurn(role="caller", text="Oh, okay. So, about blocking my card?"),
]
EXPECTED_B = {"a1": "passed", "a2": "failed"}

TRANSCRIPT_C = [
    TranscriptTurn(
        role="caller",
        text=(
            "Hi, my name is Asha Rao. My date of birth is April 12th, 1990, "
            "and my security phrase is blue lagoon."
        ),
    ),
    TranscriptTurn(
        role="agent",
        text="Hello. Thank you for calling Cadence Bank before I can access your",
    ),
    TranscriptTurn(role="caller", text="Can you tell me my account balance?"),
    TranscriptTurn(
        role="agent",
        text=(
            "Account. I'm sorry those details didn't match our records. Could "
            "you please State your full name date of birth and security phrase again?"
        ),
    ),
    TranscriptTurn(role="caller", text="Thanks, that's all I needed."),
    TranscriptTurn(
        role="agent", text="You're welcome. Is there anything else I can help you with today?"
    ),
]
EXPECTED_C = {"a1": "passed", "a2": "failed"}  # agent DOES ask again in turn [3], twice total

CASES = [
    ("A (clean success)", TRANSCRIPT_A, EXPECTED_A),
    ("B (cut off mid-verification-loop)", TRANSCRIPT_B, EXPECTED_B),
    ("C (identity rejected, caller gives up)", TRANSCRIPT_C, EXPECTED_C),
]


async def main() -> None:
    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.environ["GOOGLE_CLOUD_LOCATION"]
    credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        creds_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    client = build_vertex_client(credentials=credentials, project=project, location=location)
    # genai.Client's real .aio/.models types don't structurally match
    # GenAIClient's minimal Protocol closely enough for mypy (same class of
    # gap as the google.oauth2 untyped-call ignore elsewhere in this repo) --
    # it satisfies it at runtime, which is all that matters here.
    judge = FinalJudge(client)  # type: ignore[arg-type]

    all_match = True
    for label, transcript, expected in CASES:
        verdict = await judge.evaluate(ASSERTIONS, transcript)
        actual = {a.assertion_id: a.status for a in verdict.assertions}
        match = actual == expected
        all_match = all_match and match
        logger.info(f"{label}: expected={expected} actual={actual} match={match}")
        if not match:
            for a in verdict.assertions:
                logger.info(f"  {a.assertion_id} turn_refs={a.turn_refs} analysis={a.analysis!r}")

    if not all_match:
        logger.error("one or more transcripts did not match hand labels")
        raise SystemExit(1)
    logger.info("PASS: all 3 transcripts matched hand labels")


if __name__ == "__main__":
    asyncio.run(main())
