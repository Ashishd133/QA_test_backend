"""B5-01: the flow-graph model, gate detector and layered layout.

Pure and synchronous: no LLM, DB or LiveKit. The explorer (B5-02) feeds it
one `TurnObservation` per agent turn and emits an event for whatever
`observe()` reports as new/changed, so the map grows progressively.

Gate rule (spine §6): a verification gate is `mapped` once the agent
accepts the caller at any point (a failed attempt followed by a success
still maps), and `blocked` -- always with a human-readable reason, the DB
requires one -- when the agent rejected the caller and never accepted
them, or the call ended with the gate unresolved. A wrong identity is not
an error; it is exactly what produces a blocked gate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.engine.discovery.models import NodeKind, TurnObservation

NodeState = Literal["mapped", "blocked"]

# Layout: layer = depth in the discovery tree, rows stack within a layer in
# discovery order. Appending a node never moves an existing one, so a
# progressively-emitted map does not jump around.
_LAYER_WIDTH = 240.0
_ROW_HEIGHT = 110.0

_DEFAULT_REJECTION = "The agent did not accept the supplied details."
_UNRESOLVED_GATE = "The call ended before the agent accepted or rejected the supplied details."


@dataclass
class GraphNode:
    key: str
    label: str
    kind: NodeKind
    depth: int
    row: int
    parent: str | None
    first_turn: int
    incoming: list[str] = field(default_factory=list)
    visits: int = 1
    gate_passed: bool = False
    gate_rejection: str | None = None
    gate_unresolved: bool = False
    state: NodeState = "mapped"
    blocked_reason: str | None = None

    @property
    def x(self) -> float:
        return self.depth * _LAYER_WIDTH

    @property
    def y(self) -> float:
        return self.row * _ROW_HEIGHT

    def _refresh_gate_state(self) -> None:
        if self.gate_passed:
            self.state, self.blocked_reason = "mapped", None
        elif self.gate_rejection is not None:
            self.state, self.blocked_reason = "blocked", self.gate_rejection
        elif self.gate_unresolved:
            self.state, self.blocked_reason = "blocked", _UNRESOLVED_GATE
        else:
            self.state, self.blocked_reason = "mapped", None


@dataclass(frozen=True)
class IntentSummary:
    name: str
    state: NodeState
    path: str
    reason: str | None


@dataclass(frozen=True)
class ObserveResult:
    """Keys of nodes the caller should (re-)emit: newly created, or an
    existing one whose state or incoming edges changed."""

    changed: list[str]


class FlowGraph:
    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._rows_per_depth: dict[int, int] = {}
        self._current: str | None = None
        self._last_gate: str | None = None

    @property
    def nodes(self) -> list[GraphNode]:
        return list(self._nodes.values())

    @property
    def edges(self) -> list[tuple[str, str]]:
        return [(src, node.key) for node in self._nodes.values() for src in node.incoming]

    @property
    def current(self) -> str | None:
        return self._current

    def observe(self, turn_index: int, obs: TurnObservation) -> ObserveResult:
        changed: list[str] = []
        # Identity verification is one state however many questions it
        # takes (name, then date of birth, then phrase...). Models tend to
        # mint a key per question; consecutive gate observations are folded
        # into the gate we are already in, so the outcome lands on one node.
        here = self._nodes.get(self._current) if self._current is not None else None
        if obs.kind == "gate" and here is not None and here.kind == "gate":
            obs = obs.model_copy(update={"node_key": here.key, "label": here.label})
        prev = self._current
        returns_to_menu: str | None = None
        # An intent reply usually ends with "anything else?", i.e. the agent
        # is back at its menu even though that turn was classified as the
        # intent. When the agent has shown a menu, route one topic to the
        # next through it (intent -> menu -> next) instead of chaining
        # topics, which would make every path as long as the call.
        if obs.kind in ("intent", "handoff") and here is not None and here.kind == "intent":
            menu = next((n for n in self._nodes.values() if n.kind == "menu"), None)
            if menu is not None and obs.node_key != here.key:
                returns_to_menu = menu.key
                prev = menu.key

        node = self._nodes.get(obs.node_key)
        if node is None:
            node = self._create(turn_index, obs, prev)
            changed.append(node.key)
        else:
            node.visits += 1

        if returns_to_menu is not None and here is not None:
            menu_node = self._nodes[returns_to_menu]
            if here.key not in menu_node.incoming:
                menu_node.incoming.append(here.key)
                changed.append(menu_node.key)
        if prev is not None and prev != node.key and prev not in node.incoming:
            node.incoming.append(prev)
            if node.key not in changed:
                changed.append(node.key)
        self._current = node.key

        if node.kind == "gate":
            self._last_gate = node.key

        before = self._gate_snapshot()
        self._apply_gate_result(node, obs)
        for key, snap in self._gate_snapshot().items():
            if before.get(key) != snap and key not in changed:
                changed.append(key)
        return ObserveResult(changed=changed)

    def finalize(self) -> list[str]:
        """Call when the call has ended. A gate still unresolved becomes
        blocked (with a reason). Returns keys whose state changed."""
        changed = []
        for node in self._nodes.values():
            if node.kind != "gate" or node.gate_passed:
                continue
            before = (node.state, node.blocked_reason)
            node.gate_unresolved = True
            node._refresh_gate_state()
            if (node.state, node.blocked_reason) != before:
                changed.append(node.key)
        return changed

    def intents(self, expected: Sequence[str] = ()) -> list[IntentSummary]:
        """Every reached intent/handoff node, plus any `expected` intent the
        explorer wanted but never reached while a gate stands blocked --
        those are `blocked` with the gate's reason (the identity-gate
        requirement: blocked branches say *why*). Expected intents that were
        simply not reached with no blocked gate are omitted, not guessed."""
        out = [
            IntentSummary(n.label, n.state, self._path(n), n.blocked_reason)
            for n in self._nodes.values()
            if n.kind in ("intent", "handoff")
        ]
        seen = {i.name.casefold() for i in out}
        gate = self._blocking_gate()
        if gate is not None:
            for name in expected:
                if name.casefold() in seen:
                    continue
                seen.add(name.casefold())
                out.append(
                    IntentSummary(
                        name,
                        "blocked",
                        f"{self._path(gate)} → {name}",
                        f"Behind the {gate.label} step: {gate.blocked_reason}",
                    )
                )
        return out

    def _blocking_gate(self) -> GraphNode | None:
        for node in self._nodes.values():
            if node.kind == "gate" and node.state == "blocked":
                return node
        return None

    def _create(self, turn_index: int, obs: TurnObservation, parent_key: str | None) -> GraphNode:
        parent = self._nodes.get(parent_key) if parent_key is not None else None
        depth = parent.depth + 1 if parent is not None else 0
        row = self._rows_per_depth.get(depth, 0)
        self._rows_per_depth[depth] = row + 1
        node = GraphNode(
            key=obs.node_key,
            label=obs.label,
            kind=obs.kind,
            depth=depth,
            row=row,
            parent=parent.key if parent is not None else None,
            first_turn=turn_index,
        )
        self._nodes[node.key] = node
        return node

    def _apply_gate_result(self, node: GraphNode, obs: TurnObservation) -> None:
        if obs.gate_result is None:
            return
        gate = node if node.kind == "gate" else self._nodes.get(self._last_gate or "")
        if gate is None:
            return
        if obs.gate_result == "passed":
            gate.gate_passed = True
        elif obs.gate_result == "rejected":
            gate.gate_rejection = obs.reason or _DEFAULT_REJECTION
        gate._refresh_gate_state()

    def _gate_snapshot(self) -> dict[str, tuple[NodeState, str | None]]:
        return {
            n.key: (n.state, n.blocked_reason) for n in self._nodes.values() if n.kind == "gate"
        }

    def _path(self, node: GraphNode) -> str:
        labels = [node.label]
        cur = node
        while cur.parent is not None:
            cur = self._nodes[cur.parent]
            labels.append(cur.label)
        return " → ".join(reversed(labels))
