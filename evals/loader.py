"""Loads evals/cases/*.json into typed `EvalCase`s for the judge_evals
harness (tests/test_judge_evals.py). Kept separate from the test module so a
future consumer (e.g. a CLI to re-run a single case) can import it without
pulling in pytest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.engine.judge.models import AssertionSpec, TranscriptTurn
from evals.assertions import ASSERTIONS_BY_ID

_CASES_DIR = Path(__file__).parent / "cases"


@dataclass(frozen=True)
class EvalCase:
    id: str
    description: str
    assertions: list[AssertionSpec]
    transcript: list[TranscriptTurn]
    expected: dict[str, Literal["passed", "failed"]]


def load_cases() -> list[EvalCase]:
    cases = []
    for path in sorted(_CASES_DIR.glob("*.json")):
        raw = json.loads(path.read_text())
        cases.append(
            EvalCase(
                id=raw["id"],
                description=raw["description"],
                assertions=[ASSERTIONS_BY_ID[aid] for aid in raw["assertion_ids"]],
                transcript=[TranscriptTurn(**turn) for turn in raw["transcript"]],
                expected=raw["expected"],
            )
        )
    if not cases:
        raise RuntimeError(f"no eval cases found in {_CASES_DIR}")
    return cases
