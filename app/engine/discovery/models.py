"""B5-01: what the explorer observes about each agent turn.

`TurnObservation` is both the structured-output schema the explorer LLM
fills in (B5-02) and the fixture format under evals/discovery/ -- one
shape, so the offline graph tests and the live classifier share it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

NodeKind = Literal["greeting", "gate", "menu", "intent", "handoff", "other"]
GateResult = Literal["pending", "passed", "rejected"]


class TurnObservation(BaseModel):
    # Stable slug for the conversational state the agent is in after this
    # turn (e.g. "verification", "balance_check"). The same state reached
    # twice MUST get the same key -- that is what makes loops and retries
    # collapse onto one node instead of growing the map.
    node_key: str
    label: str
    kind: NodeKind
    # Only meaningful when the turn is about identity verification:
    # "pending" = agent is asking for credentials, "rejected" = agent says
    # they did not match, "passed" = agent confirms the caller is verified
    # (may be set on a turn whose kind is menu/intent/handoff -- the
    # confirmation usually arrives together with the next state).
    gate_result: GateResult | None = None
    # Human-readable why, for a rejection. Becomes the blocked_reason.
    reason: str | None = None
