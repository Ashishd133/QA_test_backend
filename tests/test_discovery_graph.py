"""B5-01: flow-graph + gate detector, against hand-labeled observation
fixtures (evals/discovery/*.json) keyed to the recorded reference-agent
transcripts in evals/cases/. Pure -- no DB, Vertex or LiveKit; the live
classifier is checked separately against the same fixtures."""

import json
from pathlib import Path

import pytest

from app.engine.discovery.graph import FlowGraph
from app.engine.discovery.models import TurnObservation

_FIXTURES = sorted((Path(__file__).parent.parent / "evals" / "discovery").glob("*.json"))


def _build(fixture: dict) -> FlowGraph:  # type: ignore[type-arg]
    graph = FlowGraph()
    for raw in fixture["observations"]:
        index = raw.pop("turn_index")
        graph.observe(index, TurnObservation.model_validate(raw))
    if fixture.get("finalize"):
        graph.finalize()
    return graph


@pytest.mark.parametrize("path", _FIXTURES, ids=lambda p: p.stem)
def test_graph_reconstructs_known_flow(path: Path) -> None:
    fixture = json.loads(path.read_text())
    expect = fixture["expect"]
    graph = _build(fixture)

    assert [n.key for n in graph.nodes] == expect["nodes"]
    assert sorted(map(list, graph.edges)) == sorted(expect["edges"])

    gate = expect["gate"]
    by_key = {n.key: n for n in graph.nodes}
    assert by_key[gate["key"]].state == gate["state"]
    if gate["state"] == "blocked":
        assert gate["reason_contains"] in (by_key[gate["key"]].blocked_reason or "")
    else:
        assert by_key[gate["key"]].blocked_reason is None

    intents = {i.name: i for i in graph.intents(fixture.get("expected_intents", []))}
    assert {name: i.state for name, i in intents.items()} == expect["intents"]
    # The DB's reason_required constraint, enforced here at the source.
    assert all(i.reason for i in intents.values() if i.state == "blocked")


def test_blocked_intents_name_the_gate_and_its_reason() -> None:
    fixture = json.loads(
        (_FIXTURES[0].parent / "g_three_failed_verifications_auto_handoff.json").read_text()
    )
    graph = _build(fixture)
    blocked = {
        i.name: i for i in graph.intents(fixture["expected_intents"]) if i.state == "blocked"
    }
    assert blocked["Card block"].path == "Greeting → Identity verification → Card block"
    assert "Identity verification" in (blocked["Card block"].reason or "")
    assert "did not match" in (blocked["Card block"].reason or "")


def test_revisiting_a_state_reuses_the_node_and_adds_only_new_edges() -> None:
    graph = FlowGraph()
    obs = {
        "a": TurnObservation(node_key="a", label="A", kind="greeting"),
        "m": TurnObservation(node_key="m", label="Menu", kind="menu"),
        "b": TurnObservation(node_key="b", label="B", kind="intent"),
    }
    for i, key in enumerate(["a", "m", "b", "m", "b", "m"]):
        graph.observe(i, obs[key])
    assert [n.key for n in graph.nodes] == ["a", "m", "b"]
    assert sorted(graph.edges) == [("a", "m"), ("b", "m"), ("m", "b")]


def test_observe_reports_only_what_changed() -> None:
    graph = FlowGraph()
    greet = TurnObservation(node_key="g", label="G", kind="greeting")
    gate = TurnObservation(node_key="v", label="V", kind="gate", gate_result="pending")
    assert graph.observe(0, greet).changed == ["g"]
    assert graph.observe(1, gate).changed == ["v"]
    assert graph.observe(2, gate).changed == []  # a repeat fragment changes nothing
    rejected = gate.model_copy(update={"gate_result": "rejected", "reason": "nope"})
    assert graph.observe(3, rejected).changed == ["v"]  # mapped -> blocked


def test_layout_is_layered_and_stable_as_the_graph_grows() -> None:
    graph = FlowGraph()
    graph.observe(0, TurnObservation(node_key="a", label="A", kind="greeting"))
    graph.observe(1, TurnObservation(node_key="b", label="B", kind="menu"))
    before = {n.key: (n.x, n.y) for n in graph.nodes}
    graph.observe(2, TurnObservation(node_key="c", label="C", kind="intent"))
    after = {n.key: (n.x, n.y) for n in graph.nodes}
    assert all(after[k] == v for k, v in before.items())
    assert after["a"][0] < after["b"][0] < after["c"][0]


def test_consecutive_gate_questions_collapse_into_one_gate() -> None:
    """Models key each identity question separately; the gate must still be
    one node, with the verdict on it -- not N gates of which all but the
    last are reported blocked."""
    graph = FlowGraph()
    graph.observe(0, TurnObservation(node_key="hi", label="Hi", kind="greeting"))
    for i, key in enumerate(["ask_name", "ask_dob", "ask_phrase"], start=1):
        graph.observe(
            i, TurnObservation(node_key=key, label=key, kind="gate", gate_result="pending")
        )
    graph.observe(
        4,
        TurnObservation(node_key="bal", label="Balance", kind="intent", gate_result="passed"),
    )
    graph.finalize()
    assert [n.key for n in graph.nodes] == ["hi", "ask_name", "bal"]
    assert graph.nodes[1].state == "mapped"
    assert graph.edges == [("hi", "ask_name"), ("ask_name", "bal")]


def test_consecutive_intents_route_through_the_menu_when_there_is_one() -> None:
    graph = FlowGraph()
    steps = [
        ("menu", "menu"),
        ("bal", "intent"),
        ("card", "intent"),
        ("hours", "intent"),
    ]
    for i, (key, kind) in enumerate(steps):
        graph.observe(i, TurnObservation(node_key=key, label=key.title(), kind=kind))  # type: ignore[arg-type]
    assert sorted(graph.edges) == [
        ("bal", "menu"),
        ("card", "menu"),
        ("menu", "bal"),
        ("menu", "card"),
        ("menu", "hours"),
    ]
    intents = {i.name: i.path for i in graph.intents()}
    assert intents["Hours"] == "Menu → Hours"  # not Menu -> Bal -> Card -> Hours


def test_consecutive_intents_chain_when_the_agent_never_showed_a_menu() -> None:
    graph = FlowGraph()
    for i, key in enumerate(["bal", "card"]):
        graph.observe(i, TurnObservation(node_key=key, label=key.title(), kind="intent"))
    assert graph.edges == [("bal", "card")]
