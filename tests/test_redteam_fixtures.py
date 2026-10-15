"""B4-02/B4-03: recorded red-team exchanges (evals/redteam/*.json), captured
from real runs against the reference agent. Offline checks: every fixture
names a shipped pack, and each expected evidence span really is a verbatim
span of the recorded agent turns -- the property the live judge is held to.
(The live judge-accuracy eval over these is a follow-up; see the PR.)"""

import json
from pathlib import Path

import pytest

from app.engine.judge.models import TranscriptTurn
from app.engine.redteam.judge import locate_evidence
from app.engine.redteam.packs import registry

_FIXTURES = sorted((Path(__file__).parent.parent / "evals" / "redteam").glob("*.json"))


def test_fixtures_exist() -> None:
    assert _FIXTURES


@pytest.mark.parametrize("path", _FIXTURES, ids=lambda p: p.stem)
def test_fixture_is_well_formed_and_its_evidence_is_verbatim(path: Path) -> None:
    case = json.loads(path.read_text())
    assert case["pack_id"] in {p.id for p in registry()}
    transcript = [TranscriptTurn(role=t["role"], text=t["text"]) for t in case["transcript"]]
    assert any(t.role == "agent" for t in transcript)
    expected = case["expected"]
    if expected["verdict"] == "blocked":
        assert expected["evidence_contains"] is None
    else:
        assert locate_evidence(expected["evidence_contains"], transcript) is not None
