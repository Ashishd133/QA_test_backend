"""B2.6-05 tests: parent_run_id on every span the real simulation executor
creates.

No DB, no network -- `_span_attributes` is a pure function, and it's the
one piece of this ticket actually unit-testable in this environment (the
rest is "does Phoenix show it", not code this suite can assert on; B2-08's
live pipeline itself needs the reference agent/LiveKit, per memory
b2_08_e2e_blocked_network.md). tests/test_claim_concurrency.py covers
ClaimedRun.parent_run_id itself, the other half of this ticket's plumbing.
"""

import uuid

from app.engine.executor.simulation import _span_attributes


def test_span_attributes_includes_parent_run_id_for_a_batch_child() -> None:
    run_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    attrs = _span_attributes(run_id, parent_id)
    assert attrs == {"run_id": str(run_id), "parent_run_id": str(parent_id)}


def test_span_attributes_omits_parent_run_id_for_a_standalone_run() -> None:
    """Not emitted as a null/empty attribute -- absent entirely, so a
    Phoenix filter on `parent_run_id` never matches a standalone Test
    Run's spans."""
    run_id = uuid.uuid4()
    attrs = _span_attributes(run_id, None)
    assert attrs == {"run_id": str(run_id)}
    assert "parent_run_id" not in attrs


def test_span_attributes_passes_through_extra_kwargs() -> None:
    """The root `run.simulation` span also carries scenario_id -- confirms
    that still works alongside parent_run_id, not just run_id alone."""
    run_id = uuid.uuid4()
    scenario_id = uuid.uuid4()
    attrs = _span_attributes(run_id, None, scenario_id=str(scenario_id))
    assert attrs == {"run_id": str(run_id), "scenario_id": str(scenario_id)}
