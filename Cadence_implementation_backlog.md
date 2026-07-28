# Cadence — Phase-wise Implementation Backlog

Granular, step-by-step tickets implementing `CADENCE_BACKEND_SPINE.md`, from the very first commit to a launch-ready MVP. Designed so a solo dev (or 2 devs) can pull tickets strictly in order within a phase; cross-phase dependencies are marked. Each phase ends with a **demo checkpoint** — do not start the next phase until it passes.

**Conventions**
- ID: `<phase>-<nn>` · Repo label: `[brain]` (Python backend), `[web]` (frontend repo), `[infra]` (accounts/deploy/CI), `[content]` (prompts/attacks/evals)
- Size: **S** ≤ half day · **M** ~1 day · **L** 2–3 days (split it if it feels bigger)
- Every ticket's implicit acceptance: typecheck/lint/tests pass, merged to `main`.
- "Done when" = the acceptance criteria to paste into your tracker.

---

## Phase B-1 — Ground zero (accounts & access)

**B-1-01 [infra] Provision hosts and accounts — S**
Create: brain host project (Railway or Fly — must support long-lived processes for workers), second Neon project (brain DB), LiveKit Cloud project, Google Cloud project w/ STT+TTS APIs enabled, Gemini API key.
*Done when: all keys/URLs collected in a private `ENVIRONMENT.md` checklist (values in the hosts' secret managers, never in git).*

**B-1-02 [infra] Generate and place the service token — S**
Generate `PYTHON_SERVICE_TOKEN` (32+ random bytes). Add to Vercel env (all environments) and brain host secrets.
*Done when: token exists in both secret stores; documented in `ENVIRONMENT.md` (name only).*

**B-1-03 [infra] Verify Phoenix reachability from the brain host — S**
Confirm the self-hosted Arize Phoenix instance accepts OTLP from the brain host's network (or decide to run a second Phoenix for the startup, separate from employer infra — **decide this explicitly**; do not reuse employer-owned infrastructure for the startup).
*Done when: a test OTLP trace from a local script appears in the chosen Phoenix.*

**Checkpoint:** you can authenticate to every external service from your machine.

---

## Phase B0 — Skeleton & contract loop (nothing real yet, everything wired)

**B0-01 [brain] Initialize repo — S**
`cadence-brain`: uv/poetry project, FastAPI app factory, `/v1/healthz` returning `{status:"ok"}`, ruff + mypy + pytest configured, pre-commit hooks.
*Done when: `pytest` green; `uvicorn` serves healthz locally.*

**B0-02 [brain] Service-token middleware — S**
Reject requests without `Authorization: Bearer <PYTHON_SERVICE_TOKEN>` with `401 {error:{code:"unauthorized"}}` (healthz exempt). Parse `X-User-Id`/`X-User-Email` into request context; write endpoints later require `X-User-Id`.
*Done when: tests cover missing/wrong/correct token + missing user header on a dummy write route.*

**B0-03 [brain] Error envelope & exception handlers — S**
Uniform `{error:{code,message,details?}}` for HTTPException, validation (422), and unhandled (500, logged).
*Done when: tests assert the shape for each class.*

**B0-04 [brain] Database bootstrap — M**
SQLAlchemy (async) + Alembic wired to brain Neon DB. Migration 001: `agents, suites, scenarios, personas, runs, run_events, turns, assertion_results, findings, discovery_nodes, discovery_edges, discovery_intents, discovery_drafts` per spine §3 — including the `findings` evidence CHECK, the discovery blocked-reason trigger, `scenarios.source_draft_ref UNIQUE`, `runs.idempotency_key UNIQUE`, and `org_id` on every table (hardcoded `org_default` for now).
*Done when: `alembic upgrade head` clean on a fresh DB; constraint tests prove the CHECK/UNIQUE rules reject bad rows.*

**B0-05 [brain] OpenAPI export + drift CI — M**
Script exports `contract/openapi.json` from the app (stable ordering). CI job fails if regeneration differs from the committed file. Tag convention `contract-v0.1.0` documented in CONTRIBUTING.
*Done when: CI red on an uncommitted route change, green after committing the regenerated file.*

**B0-06 [brain] Event emission core — M**
`events.py`: typed constructors for every event (`status,turn,metrics,assertion,node,intent,attack,exposure,done,error`) as Pydantic models registered into the OpenAPI components; `emit(run_id, event)` = INSERT into `run_events` (seq = per-run counter) + `pg_notify('run_events', …)`. This is the **only** write path to `run_events`.
*Done when: unit tests prove seq monotonicity under concurrent emits (two sessions, advisory-locked counter or `INSERT … RETURNING` pattern).*

**B0-07 [brain] SSE endpoint — L**
`GET /v1/runs/{id}/stream`: replay existing events (`seq > Last-Event-ID` if header present, else from 0), then follow live via LISTEN with a poll fallback; frame format `id:<seq> / event:<type> / data:<json>`; heartbeat comment every 15 s; close after `done`/`error(fatal)`; `404` unknown run.
*Done when: integration test — insert 3 events, connect, receive 3; insert 2 more, receive live; reconnect with `Last-Event-ID:3`, receive exactly 4–5.*

**B0-08 [brain] Runs table as queue + FakeRunner worker — L**
Worker process: claim loop (`FOR UPDATE SKIP LOCKED` on `status='queued'`), heartbeat updater, executor dispatch by `run.type`. First executor = **FakeRunner**: replays a scripted JSON event sequence (checked into `app/workers/scripts/`) at prototype cadence through `emit()`, then marks run completed and writes `runs.metrics`.
*Done when: insert a queued sim run → worker claims → SSE client sees scripted turns live → run completes; two workers never double-claim (test).*

**B0-09 [brain] Reaper — S**
Periodic task: runs `claimed/running` with `heartbeat_at` older than 60 s → `status='failed'` + `error{code:"worker_lost",fatal:true}` event.
*Done when: test with a killed worker shows the run failed within 90 s.*

**B0-10 [infra] Deploy skeleton — M**
Deploy `api` + one worker to the brain host; Alembic migrate on release; healthz public, everything else token-gated; Vercel gets `PYTHON_API_BASE_URL`.
*Done when: curling deployed healthz OK; token-less `/v1/suites` (stub) is 401.*

**B0-11 [web] Contract sync script — M**
`pnpm sync:contract`: fetch `contract/openapi.json` at the tag pinned in `contract.lock`, run `openapi-typescript` into `src/lib/api/generated/`, commit output.
*Done when: generated types compile; README section documents the bump flow.*

**B0-12 [web] `API_MODE` switch in the repository layer — M**
`mock` (existing) | `dev`/`demo` (HTTP to BFF routes). Add the BFF pass-through route handlers/Server Actions that attach token + identity headers and proxy `/v1` verbatim.
*Done when: with `API_MODE=dev` against the deployed skeleton, the app boots and screens show empty/error states gracefully (no data yet is fine — no crashes).*

**B0-13 [web] SSE consumer behind the playback seam — L**
New `TurnPlaybackController` implementation wrapping `EventSource` against `/api/runs/{id}/stream` (BFF pipe route with `maxDuration` set): maps SSE events → existing subscriber callbacks; auto-resume relies on Last-Event-ID; `reset` closes the source. Mode-switched with `API_MODE`; timer implementation stays for `mock`.
*Done when: fake-timer tests still green in mock mode; against deployed FakeRunner, the Simulations screen plays a scripted run end-to-end, including a mid-run hard refresh resuming cleanly.*

**Checkpoint B0 (deployed):** click Run in the live frontend → FakeRunner streams a scripted simulation through real API/DB/SSE → score card → (Results still mock). *The entire nervous system works before any voice exists.*

---

## Phase B1 — Real CRUD (the app runs on the brain)

**B1-01 [brain] Suites & scenarios endpoints — M**
`GET/POST /v1/suites`, `GET/PATCH/DELETE /v1/suites/{id}` (detail embeds scenarios with last-run fields), `POST /v1/suites/{id}/scenarios` (manual or `{fromDraftId}` — idempotent via `source_draft_ref`), `PATCH/DELETE /v1/scenarios/{id}`.
*Done when: CRUD tests incl. idempotent re-add returning the existing scenario (200, not 409 — matches the "Added ✓" toggle).*

**B1-02 [brain] Agents endpoints — M**
CRUD + `POST /agents/test-connection` (pre-save; **stubbed** result for now, real in B3) + `GET /agents/health`. Transport-specific config validated by discriminated Pydantic unions.
*Done when: invalid transport payloads 422 with per-field details.*

**B1-03 [brain] Runs read endpoints — M**
`GET /v1/runs?type=&suiteId=&agentId=&status=&limit=`, `GET /v1/runs/{id}` assembling full detail (metrics + materialized rows + type-specific sections).
*Done when: detail of a FakeRunner-completed run matches the frontend `Run` type exactly (contract test).*

**B1-04 [brain] Run creation endpoints — M**
`POST /v1/simulations/runs`, `/v1/discovery/runs` (validates `dummyIdentity` format → 422 `invalid_identity`), `/v1/redteam/runs` (categories enum). Idempotency-Key support; concurrency pre-check → 409 `concurrency_limit` + `retryAfterMs`; stamps `created_by_user_id`.
*Done when: double-submit with same key returns same runId; tests per error path.*

**B1-05 [brain] Cancel + rerun — S**
`POST /v1/runs/{id}/cancel` (sets status, NOTIFY; FakeRunner honors between events, emits partial `done`), `POST /v1/runs/{id}/rerun` (clones config → new queued run).
*Done when: cancelling mid-FakeRunner-stream yields `status:cancelled` + partial results in detail.*

**B1-06 [brain] Dashboard metrics — M**
`GET /v1/metrics/dashboard`: cards, 14-day run volume, outcome donut — computed from `runs`; `GET /v1/personas` (seeded built-ins for the sim select).
*Done when: numbers reconcile with a seeded fixture set (test).*

**B1-07 [brain] Materialization on completion — M**
On `done`, executor writes `turns`, `assertion_results`, and type-specific rows from the event log in one transaction with the status flip.
*Done when: replaying detail-from-tables vs reduce-from-events yields identical JSON (property test).*

**B1-08 [brain] Seed script — S**
Idempotent seed: 2 suites, 6 scenarios, 2 agents, built-in personas, 4 completed FakeRunner runs (so dashboard/results are non-empty on fresh envs).
*Done when: `python -m app.seed` twice → no duplicates.*

**B1-09 [web] Wire every screen to `dev` mode — L**
Repository functions call BFF routes for all reads/writes (suites, agents, runs, dashboard, personas); Simulations' `createRunAction` → `POST /v1/simulations/runs` + open stream; "Add to suite" → fromDraftId endpoint; delete the now-dead mock-only branches where trivial, keep `mock` mode functional.
*Done when: full Playwright smoke passes in `dev` mode against the deployed brain; `mock` smoke still green.*

**Checkpoint B1 (deployed):** the entire app runs on Python+Postgres with FakeRunner streams — real API, real DB, scripted engine. Amend the contract now if anything chafed; tag `contract-v1.0.0`.

---

## Phase B2 — The hero loop goes real (first real voice)

**B2-01 [brain] Reference target agent — L**
`engine/reference_agent/`: LiveKit voice agent, banking-style — greeting → verification gate (name+DOB+phrase) → intents: balance, card block, branch info, agent handoff → **one planted leak** (discloses another "customer's" masked record under a crafted pretext). Deployed as its own long-lived process with a stable room-join config.
*Done when: you can call it from the LiveKit playground and traverse all branches by hand; planted leak reproducible.*

**B2-02 [brain] Caller runtime: transport + audio loop — L**
Pipecat pipeline joining a LiveKit room as the caller: TTS out (Google), agent audio in → streaming STT (word timings), VAD, turn assembly. No LLM yet — a hardcoded 3-utterance script.
*Done when: scripted call against the reference agent produces an accurate transcript in logs.*

**B2-03 [brain] Latency clock — M**
Per-turn latency = caller utterance end (last audio frame sent) → agent first audio frame received, measured at the transport layer; interruption/barge-in counter via VAD overlap. Threshold (default 1000 ms) from org config → `flagged`/`flagReason`.
*Done when: harness with an artificially delayed reference-agent response measures within ±75 ms of the injected delay.*

**B2-04 [brain] Persona LLM — M**
Gemini Flash-driven caller: scenario persona + script/goal → next utterance; end-of-call detection; turn budget cap.
*Done when: persona completes the "block my card" scenario against the reference agent unscripted, transcript coherent.*

**B2-05 [content] Judge v1: incremental + final — L**
Jinja2 rubric templates (house style: signal definitions, distinguish-from, analysis-before-JSON): per-turn incremental judge (assertion flips + live score) and final holistic pass (final score, per-assertion notes, sentiment). Temperature 0, structured output, `turn_refs` mandatory. All calls traced to Phoenix with prompt version.
*Done when: on 3 recorded transcripts, verdicts match hand labels; malformed-JSON retry path tested.*

**B2-06 [content] Golden eval harness — M**
`evals/`: ≥30 labeled transcript+assertion pairs (record from reference-agent calls, hand-grade). `pytest -m judge_evals` scores agreement; CI requires it on any change under `engine/judge/`. Gate: ≥90% agreement, zero false "passed" on planted failures.
*Done when: harness runs in CI; deliberately breaking a prompt fails the build.*

**B2-07 [brain] Simulation executor — L**
Wire it all: claim sim run → caller runtime vs. configured agent → `emit(turn)` per turn with latency → incremental judge → `emit(assertion/metrics)` → call end → final judge → `emit(done{score,resultBadge})` → materialize + metrics. Cancellation checked per turn; errors → `error` event (fatal vs. continue).
*Done when: `POST /v1/simulations/runs` against the reference agent produces a complete, correctly scored run visible live in the deployed frontend and in Results after.*

**B2-08 [brain] Run-scoped observability — S**
OTel spans across executor stages keyed by `run_id`; structured logs; a `scripts/replay_run.py` that pretty-prints any run from `run_events`.
*Done when: one Phoenix trace waterfall shows caller/STT/judge spans for a full run.*

**Checkpoint B2 (deployed) — the MVP existence proof:** click Run → real synthetic voice call → live turns with true latency → assertions flip → defensible score → Results. Record a video of this the day it works.

---

## Phase B3 — Agents & operational hardening

**B3-01 [brain] Real test-connection (WebRTC) — M**
Pre-save handshake: join room / verify signaling+token, measure RTT, return negotiated detail ≤2 s; error codes `agent_unreachable|auth_failed|bad_config`. SIP/Phone tabs return `501 not_supported` with a clean message (UI shows "coming soon").
*Done when: good and bad configs against the reference agent return correct results in the Agents screen.*

**B3-02 [brain] Agent liveness — S**
Lightweight periodic probe updating `status/last_seen_at`; `GET /agents/health` reflects it.
*Done when: stopping the reference agent flips it to offline within 2 min.*

**B3-03 [brain] Concurrency enforcement at claim time — S**
Claim query respects per-agent live-run counts vs `max_concurrency` (pre-check in B1-04 was advisory; this is the real gate).
*Done when: 3 queued runs vs `max_concurrency=1` execute strictly serially (test).*

**B3-04 [brain] Soak & chaos pass — M**
Scripted soak: 5 concurrent sim runs; kill a worker mid-run (reaper verifies); drop SSE mid-run and resume; cancel under load.
*Done when: soak script green on the deployed environment; findings fixed or ticketed.*

**B3-05 [infra] Nightly live loop — S**
Scheduled CI: one sim run against the deployed reference agent; alert (email/Slack) on failure or score deviation beyond tolerance.
*Done when: first nightly passes; a forced failure alerts.*

**B3-06 [web] Error-state polish in dev mode — M**
Concurrency 409 (with retry hint), agent offline, run failed (worker_lost), stream error frames, cancelled/partial Results rendering.
*Done when: each state has a designed (not blank/crash) UI, verified by forcing each on dev.*

**Checkpoint B3:** a stranger can connect their own WebRTC agent and run a scored simulation against it without you touching anything.

---

## Phase B4 — Red team real

**B4-01 [brain] Attack-pack format + loader — M**
Versioned YAML: `id, category, opening, escalations[], success_signals, severity_if_success, fix_template`. Loader validates + registers into `attack_packs`.
*Done when: malformed pack fails validation with a useful error; packs listable.*

**B4-02 [content] Attack pack v1 — L**
~10 hand-tuned multi-turn attacks across the 5 UI categories, developed against the reference agent (its planted leak must be found by the PII pack; auth-bypass pack must defeat a weakened gate variant).
*Done when: each attack demonstrated against the reference agent with expected outcome, recorded as fixtures.*

**B4-03 [content] Verdict judge — M**
Grades each attack/response exchange → `blocked|leaked|bypassed` + severity + extracted verbatim `evidence` span + concretized fix from template. Golden evals extended with ≥15 labeled attack exchanges (same CI gate as B2-06).
*Done when: planted-leak exchange yields `leaked` + exact evidence quote; benign refusal yields `blocked`.*

**B4-04 [brain] Red-team executor — L**
Claim → run enabled categories' attacks sequentially through the caller runtime → `emit(attack)` per pair (with interim verdict) → `emit(exposure)` updates → final verdicts → findings rows (CHECK-constrained) → `emit(done{findings,exposure})`.
*Done when: full red-team run from the deployed UI shows live attack bubbles, exposure sidebar movement, and a findings report where the critical finding quotes the reference agent's actual leaked text.*

**B4-05 [brain] Evidence protection — M**
Encrypt `findings.evidence` + `agent_response` at rest (app-layer, key in secrets); access-logged read path; dummy identities in `runs.config` stored encrypted likewise.
*Done when: raw DB dump shows ciphertext; API detail still returns plaintext; access log rows written on read.*

**Checkpoint B4:** launch attack → live red bubbles → findings with real verbatim leaked evidence + concrete fix. This is the sales demo. Record it.

---

## Phase B5 — Discovery real + MVP close-out

**B5-01 [brain] Flow-graph model + gate detector — M**
In-memory graph (nodes/edges/state) built from observed transitions; verification-gate detection from agent behavior patterns; layout coords for the UI map (simple layered layout).
*Done when: unit tests on recorded reference-agent transcripts reconstruct the known ground-truth flow.*

**B5-02 [brain] Explorer policy — L**
LLM plans probe utterances breadth-first against the graph frontier; retries gated branches with the dummy identity; marks `blocked` + human-readable reason on gate rejection; turn budget (40) + frontier exhaustion termination.
*Done when: against the reference agent with a correct identity: all branches mapped; with a wrong-value identity: gated branches `blocked` with accurate reasons — matching known ground truth.*

**B5-03 [brain] Discovery executor — M**
Progressive `emit(node/intent)` during the run; drafts generated from mapped+blocked paths with proposed assertions; `emit(done{discoveryResult})`; materialize; drafts→suite path proven end-to-end (`fromDraftId` → scenario → runnable in Simulations).
*Done when: full loop in the deployed UI: dry run → map grows live → drafts → Add to suite → run that scenario as a real simulation.*

**B5-04 [brain] Curated-replay fallback — S**
Flag a completed discovery run as the demo replay; starting discovery in `demo` environments replays its events at original cadence (uses the existing stream replay — no special code path).
*Done when: demo env discovery is deterministic with the flag on.*

**B5-05 [web] Suite authoring un-inerted (minimal) — M**
"New suite" (name/description/agent) and "Add scenario" (name/persona/assertions textarea) wired to B1-01 endpoints — minimal forms, no builder UI. Closes DECISIONS.md #3 at MVP level.
*Done when: a suite authored in the UI runs a simulation end-to-end.*

**B5-06 [brain][web] MVP close-out sweep — M**
Empty states with real data everywhere; `GET /runs` limit param honored in dashboard; delete dead mock branches where safe; DECISIONS.md reconciled; README quickstart for both repos; demo script written + rehearsed twice.
*Done when: a full cold demo (connect → discover → author → simulate → red-team → results) runs from the script in <12 min without improvisation.*

**Checkpoint B5 = MVP.** Tag both repos `mvp-1.0`. Everything below is post-MVP.

---

## Post-MVP epics (sequenced; break into tickets only when their turn comes)

| # | Epic | First 3 tickets when opened |
|---|---|---|
| E1 | **Condition & fault injection** (spine §10.1 — port the FrameProcessor fault work): condition profiles on scenarios (noise beds, 8 kHz/PSTN codec sim, packet loss, accents), condition-matrix "chaos suites," pass-rate-by-condition heatmap | profile schema + 2 noise beds → codec degradation processor → conditions selector in sim UI |
| E2 | **Indic multilingual** (§10.2): Hindi/Hinglish personas with code-switching, language-consistency assertions, per-language judge validation | Hinglish persona + TTS voice → code-switch mid-call behavior → language-consistency rubric + evals |
| E3 | **CI/CD gate & platform** (§10.5): API keys, suite-run public endpoint, GitHub Action, Slack webhook on regression/critical finding, scheduled runs | api_keys auth path → `POST /public/suites/{id}/run` → GitHub Action |
| E4 | **Suite batch runs**: `POST /suites/{id}/run` fan-out with `parent_run_id`, aggregate results view | fan-out executor → aggregate detail endpoint → suite-run UI row |
| E5 | **Production monitoring** (§10.4): call ingestion, judge-on-production, drift dashboards, one-click "failed call → regression scenario" | ingestion endpoint + storage → batch judge job → failed-call-to-scenario generator |
| E6 | **Compliance evidence layer** (§10.3): obligation mapping (RBI/IRDAI/DPDP), auditor evidence packs, retest links | obligation taxonomy table → finding↔obligation mapper → evidence-pack PDF |
| E7 | **SIP/PSTN transports**: LiveKit SIP bridge, phone dial-out, un-disable the tabs | SIP config + handshake → sip test-connection → sim over SIP |
| E8 | **Version intelligence** (§10.6): agent version tags, run diffing, A/B suites | version tag on runs → diff endpoint → diff UI |
| E9 | **Attack intelligence** (§10.7): custom attack authoring, pack updates as content drops, one-click retest-a-finding | retest endpoint → pack update pipeline → authoring UI |
| E10 | **Enterprise posture** (§10.8): real orgs/RBAC, SSO, audit logs, single-tenant deploy story, share links + PDF reports (with evidence-exclusion default) | orgs + membership → RBAC middleware → report/share endpoints |

**Standing rules across all phases:** contract changes always land in `cadence-brain` first and tag before the frontend pins; the Friday deployed demo never breaks (fixing it outranks the next ticket); judge-prompt changes never merge without the golden evals; nothing from the epics starts while the hero loop is flaky.