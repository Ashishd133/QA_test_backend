"""B2.7-05: the nine built-in metrics, packaged -- not new judging. Six are
`llm_judge`-kind and follow the exact Definition/Distinguish-from house
style already established by evals/assertions.py and
app/engine/judge/templates/final.jinja2 (B2.7-06 renders `spec.description`/
`spec.distinguish_from` into a signal block the same way an assertion is
rendered today). Two are `builtin`-kind (code-evaluated) against data the
executor already records (Turn.latency_ms, Turn.text). One
(`transcription_accuracy_wer`) is registered -- it appears in `GET
/v1/metrics` per this ticket's own done-when -- but is honestly
`not_computable` until a reference transcript pipeline exists to diff
against; there is no such pipeline today (RunDetail.wer is hardcoded "-"),
and fabricating a number would be worse than not scoring it.

`app/seed.py` inserts these as real, builtin=true `metrics` rows (deterministic
uuid5 ids via its own `_id()` helper, `ON CONFLICT (id) DO NOTHING`) --
the same dual-path convention as the three built-in personas: idempotent
seed data, not a schema migration, since these are content rows a fresh
environment gets by running the seed script, not by upgrading its schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["builtin", "llm_judge"]
OutputType = Literal["boolean", "numeric", "enum", "tri_state"]


@dataclass(frozen=True)
class BuiltinMetricDef:
    key: str
    name: str
    kind: Kind
    output_type: OutputType
    docs: str
    spec: dict[str, object]


BUILTIN_METRICS: list[BuiltinMetricDef] = [
    BuiltinMetricDef(
        key="expected_outcome",
        name="expected_outcome",
        kind="llm_judge",
        output_type="tri_state",
        docs=(
            "Holistic judgment of whether the call reached a reasonable "
            "resolution of the caller's actual request. Pass/Review/Fail, "
            "not a binary -- some calls are genuinely ambiguous (e.g. a "
            "legitimate escalation with no clear resolution yet) and "
            "forcing those into pass/fail would just be noise."
        ),
        spec={
            "description": (
                "By the end of the call, the caller's actual request was "
                "either fulfilled, or handed to something that will "
                "resolve it (a clear escalation/next step the caller "
                "agreed to). Pass = clearly resolved or clearly and "
                "correctly handed off. Fail = the call ended with the "
                "caller's request neither addressed nor handed off "
                "anywhere (e.g. it just dropped, or looped). Review = "
                "anything genuinely ambiguous between those two -- state "
                "in `analysis` exactly what's ambiguous about it."
            ),
            "distinguish_from": (
                "Not the same as every assertion passing -- a call can "
                "satisfy every individual assertion and still not reach "
                "the outcome the caller actually wanted (see the "
                "goal/assertions split, B2.7-09). Judge the outcome, not "
                "the process."
            ),
            "threshold": None,
        },
    ),
    BuiltinMetricDef(
        key="response_latency",
        name="response_latency",
        kind="builtin",
        output_type="numeric",
        docs=(
            "Mean agent response latency in milliseconds across the call, "
            "from each agent turn's transport-layer latency_ms (LatencyClock, "
            "app/engine/caller/latency_clock.py -- caller-stopped-speaking "
            "to target's first real audio frame, not an LLM-layer measurement). "
            "`threshold` flags a call whose mean exceeds it."
        ),
        spec={"threshold": 3000, "aggregation": "mean"},
    ),
    BuiltinMetricDef(
        key="transcription_accuracy_wer",
        name="transcription_accuracy_wer",
        kind="builtin",
        output_type="numeric",
        docs=(
            "Word error rate of the agent's speech-to-text transcript "
            "against a reference transcript. Not computable today: no "
            "reference transcript is recorded anywhere to diff against "
            '(RunDetail.wer is hardcoded "-" for the same reason) -- '
            "every call scores `not_computable` until a reference-transcript "
            "pipeline exists. Registered now so it appears in the metric "
            "library and the UI can render its type, not because it "
            "actually scores anything yet."
        ),
        spec={"threshold": 0.15, "not_computable": True},
    ),
    BuiltinMetricDef(
        key="talk_ratio",
        name="talk_ratio",
        kind="builtin",
        output_type="numeric",
        docs=(
            "Agent word count as a fraction of total (agent + caller) word "
            "count across the transcript. A text-length proxy, not an "
            "audio-duration measurement -- no per-turn audio duration is "
            "recorded (Turn has no duration column), so this is computed "
            "from Turn.text word counts, which is what's actually available. "
            "`threshold` flags a call where the agent talks more than its share."
        ),
        spec={"threshold": 0.6},
    ),
    BuiltinMetricDef(
        key="interruption_recovery",
        name="interruption_recovery",
        kind="llm_judge",
        output_type="boolean",
        docs=(
            "Whether the agent recovered gracefully after being interrupted "
            "by the caller -- stayed coherent with what it was doing, didn't "
            "lose its place or repeat itself confusedly. Interruption count "
            "is already tracked (LatencyClock.interruption_count, materialized "
            "into runs.metrics.interruptions) and available as context; this "
            "metric judges recovery quality, not whether an interruption "
            "happened at all."
        ),
        spec={
            "description": (
                "After any point where the caller spoke over the agent "
                "(an interruption), the agent's subsequent turns stayed "
                "coherent -- it didn't repeat what it had already said "
                "verbatim, contradict itself, or lose track of what it "
                "was doing before the interruption."
            ),
            "distinguish_from": (
                "This is a prohibition-shaped signal: if no interruption "
                "occurred anywhere in the call, that is a pass by default "
                "-- there was nothing to recover from. Only mark it failed "
                "if an interruption actually happened and the agent "
                "visibly mishandled the recovery."
            ),
            "threshold": None,
        },
    ),
    BuiltinMetricDef(
        key="language_match",
        name="language_match",
        kind="llm_judge",
        output_type="boolean",
        docs=(
            "Whether the agent responded in the same language the caller "
            "used, throughout the call, including any mid-call language "
            "switch by the caller."
        ),
        spec={
            "description": (
                "Every agent turn is in the same language the caller most "
                "recently spoke in. If the caller switches language "
                "mid-call, the agent's next turn follows that switch."
            ),
            "distinguish_from": (
                "A single borrowed word or phrase (a proper noun, a "
                "product name) in an otherwise correctly-matched turn does "
                "not fail this -- judge the turn's dominant language, not "
                "isolated words."
            ),
            "threshold": None,
        },
    ),
    BuiltinMetricDef(
        key="hallucination",
        name="hallucination",
        kind="llm_judge",
        output_type="boolean",
        docs=(
            "Whether the agent stated any factual claim about the "
            "caller's account, bank policy, or process that is not "
            "supported by anything in the conversation or reasonable "
            "domain knowledge for a bank support agent. Pass = no such "
            "claim found."
        ),
        spec={
            "description": (
                "Every specific factual claim the agent makes about the "
                "caller's account, a policy, a timeline, or a process is "
                "either grounded in something the caller said, a "
                "reasonable domain default for bank support (e.g. "
                "'replacement cards typically arrive in 5-7 business "
                "days' is fine even if never stated by the caller), or "
                "correctly hedged as uncertain."
            ),
            "distinguish_from": (
                "Not the same as being wrong about something the caller "
                "already told the agent (that's a comprehension failure, "
                "a different problem) -- this is specifically about the "
                "agent inventing specifics (an account number, a balance, "
                "a policy detail) that were never established by anyone."
            ),
            "threshold": None,
        },
    ),
    BuiltinMetricDef(
        key="relevancy",
        name="relevancy",
        kind="llm_judge",
        output_type="boolean",
        docs=(
            "Whether the agent's responses stayed relevant and on-topic "
            "to what the caller actually asked, without extended "
            "irrelevant tangents."
        ),
        spec={
            "description": (
                "Each agent turn responds to what the caller most "
                "recently said or asked -- it doesn't wander into "
                "unrelated topics, over-explain things the caller didn't "
                "ask about, or ignore the caller's actual question in "
                "favor of a scripted-sounding tangent."
            ),
            "distinguish_from": (
                "Required identity verification or standard compliance "
                "steps are relevant by definition, even though the caller "
                "didn't ask for them -- this signal is about tangents "
                "beyond what the call actually requires, not about the "
                "agent ever doing anything the caller didn't explicitly "
                "request."
            ),
            "threshold": None,
        },
    ),
    BuiltinMetricDef(
        key="response_consistency",
        name="response_consistency",
        kind="llm_judge",
        output_type="boolean",
        docs=(
            "Whether the agent avoided contradicting itself across turns "
            "-- stating different account details, different next steps, "
            "or reversing an earlier statement without acknowledging it."
        ),
        spec={
            "description": (
                "Every statement the agent makes stays consistent with "
                "what it said earlier in the same call -- the same "
                "account details, the same stated next steps, the same "
                "explanation for why something is or isn't possible."
            ),
            "distinguish_from": (
                "The agent correcting itself out loud ('actually, let me "
                "check that again -- it's X, not Y') is not a failure of "
                "this signal; an unacknowledged, silent contradiction is."
            ),
            "threshold": None,
        },
    ),
]
