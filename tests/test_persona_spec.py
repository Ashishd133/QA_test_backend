"""B2.7-02 unit tests for `_build_persona_spec`'s merge logic -- pure
function, no DB/network, so this runs even where TEST_DATABASE_URL isn't
configured (CI). Exercises the actual persona -> engine wiring nothing
else in the suite covers: a live call is the only end-to-end check, and
that's explicitly deferred (see this session's own scope decision).
"""

from typing import cast

from sqlalchemy.engine import RowMapping

from app.engine.executor.simulation import _build_persona_spec

_NO_PERSONA_ROW = cast(
    RowMapping,
    {
        "persona_voice": None,
        "persona_language": None,
        "persona_accent": None,
        "persona_emotion": None,
        "persona_speaking_rate": None,
        "persona_traits": None,
    },
)


def test_no_linked_persona_falls_back_to_script_only() -> None:
    spec = _build_persona_spec("Asha Rao", {"traits": {"tone": "curt"}}, _NO_PERSONA_ROW)
    assert spec.name == "Asha Rao"
    assert spec.traits == {"tone": "curt"}
    assert spec.voice is None
    assert spec.language is None
    assert spec.speaking_rate is None


def test_linked_persona_traits_are_the_base_and_script_traits_override() -> None:
    persona_row = cast(
        RowMapping,
        {
            "persona_voice": "en-IN-Chirp3-HD-Achernar",
            "persona_language": "en-IN",
            "persona_accent": None,
            "persona_emotion": None,
            "persona_speaking_rate": 1.1,
            "persona_traits": {"tone": "warm", "patience": "high"},
        },
    )
    spec = _build_persona_spec("Priya Sharma", {"traits": {"tone": "curt"}}, persona_row)
    # Script-level trait wins on a shared key ("tone"); persona traits not
    # mentioned by the script survive untouched ("patience").
    assert spec.traits == {"tone": "curt", "patience": "high"}
    assert spec.voice == "en-IN-Chirp3-HD-Achernar"
    assert spec.language == "en-IN"
    assert spec.speaking_rate == 1.1


def test_persona_accent_and_emotion_fold_into_traits_without_overriding_explicit_ones() -> None:
    persona_row = cast(
        RowMapping,
        {
            "persona_voice": None,
            "persona_language": None,
            "persona_accent": "Indian English",
            "persona_emotion": "frustrated",
            "persona_speaking_rate": None,
            "persona_traits": {"emotion": "already set by traits"},
        },
    )
    spec = _build_persona_spec("Frustrated Frank", {}, persona_row)
    assert spec.traits["accent"] == "Indian English"
    # setdefault: an explicit traits["emotion"] is never clobbered by the
    # column-level persona_emotion.
    assert spec.traits["emotion"] == "already set by traits"


def test_no_script_uses_generic_fallback_goal_and_opening_line() -> None:
    spec = _build_persona_spec("Asha Rao", None, _NO_PERSONA_ROW)
    assert "Asha Rao" in spec.goal
    assert spec.opening_line == "Hi, I need some help with my account."


def test_script_goal_and_opening_line_take_precedence() -> None:
    spec = _build_persona_spec(
        "Asha Rao",
        {"goal": "Get a replacement card fast.", "openingLine": "My card was stolen."},
        _NO_PERSONA_ROW,
    )
    assert spec.goal == "Get a replacement card fast."
    assert spec.opening_line == "My card was stolen."
