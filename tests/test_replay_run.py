"""Unit tests for scripts/replay_run.py's pure formatting logic (no DB
needed -- format_event/format_header take plain dicts/mappings, see
replay_run.py's own docstring for why that split exists)."""

from datetime import UTC, datetime

from scripts.replay_run import format_event, format_header

_TS = datetime(2026, 8, 10, 12, 34, 56, 789000, tzinfo=UTC)


def test_format_event_turn_includes_role_text_and_latency() -> None:
    line = format_event(3, _TS, "turn", {"role": "agent", "text": "hi there", "latencyMs": 842})
    assert line.startswith("[   3] 12:34:56.789  turn      ")
    assert "'hi there'" in line
    assert "842ms" in line
    assert "agent" in line


def test_format_event_turn_without_latency_omits_it() -> None:
    line = format_event(1, _TS, "turn", {"role": "caller", "text": "hello", "latencyMs": None})
    assert "ms)" not in line


def test_format_event_turn_flags_flagged_turns() -> None:
    line = format_event(
        2, _TS, "turn", {"role": "agent", "text": "x", "flagged": True, "flagReason": "slow"}
    )
    assert "[FLAGGED]" in line


def test_format_event_assertion_passed_uses_checkmark_and_note() -> None:
    line = format_event(
        5,
        _TS,
        "assertion",
        {
            "assertionId": "a3",
            "name": "States card-block timeline",
            "status": "passed",
            "triggeredAtTurn": 4,
            "note": "the agent said the block is immediate",
        },
    )
    assert "✓ passed" in line
    assert "a3" in line
    assert "@turn 4" in line
    assert "the agent said the block is immediate" in line


def test_format_event_assertion_failed_uses_cross_mark() -> None:
    line = format_event(
        6, _TS, "assertion", {"assertionId": "a1", "name": "x", "status": "failed", "note": None}
    )
    assert "✗ failed" in line
    assert " -- " not in line  # no note -> no dangling separator


def test_format_event_metrics_lists_present_fields_only() -> None:
    line = format_event(7, _TS, "metrics", {"score": 0.82, "avgLatencyMs": 910.4})
    assert "score=0.82" in line
    assert "avgLatency=910ms" in line
    assert "turns=" not in line


def test_format_event_done_with_result_badge() -> None:
    line = format_event(9, _TS, "done", {"score": 0.75, "resultBadge": "pass"})
    assert "score=0.75" in line
    assert "badge=pass" in line


def test_format_event_error_marks_fatal() -> None:
    line = format_event(1, _TS, "error", {"code": "call_failed", "message": "boom", "fatal": True})
    assert "call_failed [FATAL]: boom" in line


def test_format_event_error_non_fatal_has_no_marker() -> None:
    line = format_event(
        1, _TS, "error", {"code": "judge_timeout", "message": "slow", "fatal": False}
    )
    assert "[FATAL]" not in line


def test_format_event_status() -> None:
    line = format_event(0, _TS, "status", {"status": "running"})
    assert "status=running" in line


def test_format_event_unknown_type_falls_back_to_raw_repr() -> None:
    line = format_event(1, _TS, "some_future_type", {"foo": "bar"})
    assert "{'foo': 'bar'}" in line


def test_format_header_includes_run_and_scenario_identifiers() -> None:
    header = format_header(
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "type": "simulation",
            "status": "completed",
            "end_reason": "completed",
            "agent_name": "Reference Agent",
            "scenario_name": "Lost card, request block",
            "created_at": _TS,
            "started_at": _TS,
            "ended_at": _TS,
        }
    )
    assert "11111111-1111-1111-1111-111111111111" in header
    assert "status=completed" in header
    assert "Reference Agent" in header
    assert "Lost card, request block" in header
