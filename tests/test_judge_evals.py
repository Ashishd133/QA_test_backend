"""B2-07: golden eval harness. Runs FinalJudge for real (Vertex, temperature
0) against every case in evals/cases/*.json and checks it against the hand
label.

Two separate bars, per the ticket:
  - >=90% agreement across ALL (transcript, assertion) pairs -- the
    aggregate accuracy gate.
  - ZERO false "passed" on any pair hand-labeled "failed" -- unconditional,
    not folded into the 90%. A judge that rubber-stamps everything as
    passing can still clear 90% agreement if most cases are genuinely
    clean; this is the check that catches that failure mode specifically.

Real API calls: costs money, has network latency, and (temperature 0
notwithstanding) isn't perfectly deterministic run to run -- excluded from
the default `pytest -q` (see pyproject.toml's `addopts`), run explicitly via
`pytest -m judge_evals`, and gated in CI by its own workflow
(.github/workflows/judge-evals.yml) scoped to changes under app/engine/judge/
and evals/, not the main ci.yml.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from google.oauth2 import service_account

from app.engine.judge.judge import FinalJudge, build_vertex_client
from evals.loader import load_cases

# Settings (app.config) doesn't model GOOGLE_APPLICATION_CREDENTIALS -- it's
# read directly by google-auth, same as scripts/validate_judge.py -- so this
# test needs its own .env load rather than going through get_settings().
load_dotenv()

pytestmark = pytest.mark.judge_evals

_AGREEMENT_THRESHOLD = 0.90

requires_judge_creds = pytest.mark.skipif(
    not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
    reason="GOOGLE_APPLICATION_CREDENTIALS not configured",
)


def _build_judge() -> FinalJudge:
    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        creds_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    client = build_vertex_client(credentials=credentials, project=project, location=location)
    return FinalJudge(client)  # type: ignore[arg-type]


@requires_judge_creds
async def test_judge_agreement_and_zero_false_passed_on_failures() -> None:
    judge = _build_judge()
    cases = load_cases()

    total_pairs = 0
    matched_pairs = 0
    false_passed: list[str] = []
    mismatches: list[str] = []

    for case in cases:
        verdict = await judge.evaluate(case.assertions, case.transcript)
        actual = {a.assertion_id: a.status for a in verdict.assertions}

        for assertion_id, expected_status in case.expected.items():
            total_pairs += 1
            actual_status = actual.get(assertion_id)
            if actual_status == expected_status:
                matched_pairs += 1
            else:
                mismatches.append(
                    f"{case.id}/{assertion_id}: expected={expected_status} actual={actual_status}"
                )
            if expected_status == "failed" and actual_status == "passed":
                false_passed.append(f"{case.id}/{assertion_id}")

    agreement = matched_pairs / total_pairs
    assert not false_passed, f"false 'passed' on hand-labeled failures: {false_passed}"
    assert agreement >= _AGREEMENT_THRESHOLD, (
        f"agreement {agreement:.0%} ({matched_pairs}/{total_pairs}) below "
        f"{_AGREEMENT_THRESHOLD:.0%} threshold. Mismatches: {mismatches}"
    )
