# Cadence Backend — The Product Spine

The definitive backend document. It **adopts** `BACKEND_SPEC.md`'s architecture decisions (Next.js BFF, Python brain, SSE, split databases) with two amendments (§0), then goes all the way down: data model, streaming protocol, execution plane, engine internals, endpoint inventory, deployment, and the roadmap that takes Cadence past Bluejay and Cekura. It supersedes the earlier `CADENCE_API_ARCHITECTURE.md` where the two conflict (that doc assumed WebSockets and no BFF; the reconnect semantics, event vocabulary, and product-logic contracts from it are carried forward here).

Frontend state assumed (per `FRONTEND_OVERVIEW.md`): all 8 screens built on the mock repository + `createTurnPlayback` seams; real NextAuth v5 auth (Prisma/Neon); deployed on Vercel. The backend's job is to fill those two seams and become the brain.

---

## 0. Verdict on BACKEND_SPEC.md — adopted, with two amendments

| Decision in the spec | Verdict |
|---|---|
| Next.js stays BFF; browser never calls Python | ✅ Adopt. Auth is built, deployed, and working — centralizing session validation, rate limiting, and CSRF in one place is correct. Python stays a plain internal service. |
| Shared service token + forwarded `X-User-Id`/`X-User-Email` | ✅ Adopt. Sufficient because Python is never publicly routable (deploy-time guarantee, §9). Add: reject requests missing `X-User-Id` on write endpoints — a forgotten header should fail loudly, not create ownerless rows. |
| SSE over WebSocket | ✅ Adopt — the one-directional analysis is right, and SSE brings something the spec undersells: native reconnect via `Last-Event-ID`. **Amendment A makes that real** (below). Cancel/control goes over REST, which the spec implies but should state. |
| Separate Postgres for Python (not Prisma's DB) | ✅ Adopt. Two ORMs sharing migration ownership of one schema is a standing coordination tax. Cross-refs by value (`user_id` string) is the right call. |
| Pagination: none in v1 | ✅ Adopt, with one exception: `GET /v1/runs` gets `?limit=` from day one — runs are the only table that grows per usage, and the dashboard only wants 8. |
| FastAPI + Pydantic + SQLAlchemy/Alembic | ✅ Adopt, and exploit it: FastAPI **generates `openapi.json` natively**, which becomes the cross-repo contract mechanism (§2) — stronger than the spec's prose "maps 1:1 to repository functions" promise. |

**Amendment A — the event log is the spine, not an implementation detail.**
The spec's reconnect story ("call the detail endpoint and show what's done so far") loses the live tail and forces every consumer to handle two data shapes. Instead: every run appends to an ordered `run_events` table (§3), and the SSE stream is just a reader of that table. `id:` on each SSE frame = the event's `seq`; a dropped `EventSource` reconnects with `Last-Event-ID` and Python resumes from `seq+1` — lossless resume using the SSE feature built for exactly this, zero client code (browsers send the header automatically). The detail endpoint and the stream can then never disagree, every run is replayable for debugging, and "curated replay" demo mode (execution plan P5) falls out for free.

**Amendment B — mind the Vercel proxy on long streams.**
Piping SSE through a Next.js route handler means a Vercel function stays open for the whole run. Fine for MVP run lengths (1–5 min) **if** the stream route sets `maxDuration` explicitly and the plan supports it; but know the escape hatch before you need it: Next.js mints a short-lived signed stream token (JWT, 60 s expiry, scoped to one `runId`), and the browser opens the SSE connection **directly to Python**, which validates only that token. Auth issuance stays centralized in Next.js; Python still never validates sessions; the long-lived connection stops occupying a Vercel function. Build the proxy first (simpler, keeps Python fully private); switch per-route if duration/cost bites. The frontend's `EventSource` consumer is identical either way — only the URL changes.

---

## 1. System architecture

```
┌──────────────────────────── Browser ────────────────────────────┐
│            Next.js frontend (built) — Vercel                    │
└───────────────┬────────────────────────────┬────────────────────┘
                │ Server Actions /           │ EventSource
                │ Route Handlers             │ (SSE, proxied — or direct
                ▼                            ▼  w/ signed token, Amend. B)
┌─────────────────────────────────────────────────────────────────┐
│  Next.js server side (BFF) — NextAuth session ✓, rate limit ✓,  │
│  attaches Bearer PYTHON_SERVICE_TOKEN + X-User-Id/-Email        │
└───────────────┬─────────────────────────────────────────────────┘
                │ private network / authed HTTPS
                ▼
┌───────────────────────  cadence-brain (Python)  ────────────────┐
│  FastAPI control plane (/v1): CRUD, run lifecycle, SSE reader   │
│  ── Postgres (brain DB) ──                                      │
│     suites/scenarios/agents/runs/run_events/findings/…          │
│     job queue: runs claimed via FOR UPDATE SKIP LOCKED          │
│     event bus: run_events INSERT + LISTEN/NOTIFY → SSE readers  │
│  ── Worker pool (long-lived processes) ──                       │
│     sim worker │ discovery worker │ red-team worker             │
│     each = Test-Caller runtime (Pipecat pipeline):              │
│       persona/explorer/attacker LLM (Gemini Flash)              │
│       → Google TTS → LiveKit WebRTC (later SIP) → target agent  │
│       ← agent audio → Google STT (streaming, word timings)      │
│     + Judge/Scorer (LLM-as-judge, incremental + final)          │
│  ── Reference target agent (LiveKit) — dev/demo/CI fixture ──   │
└───────────────┬─────────────────────────────────────────────────┘
                │ OTel traces (every LLM + judge call)
                ▼
     Self-hosted Arize Phoenix          Customer voice agents
                                        (WebRTC now; SIP/PSTN later)
```

Two deployables in the brain repo — **api** (request-scoped, scale-to-zero-friendly) and **workers** (long-lived, hold WebRTC media connections) — sharing one database and one codebase.

## 2. Repo layout & cross-repo contract sync

```
cadence-brain/
  app/
    api/            FastAPI routers per domain (§8)
    workers/        claim loop, heartbeats, per-run-type executors
    engine/
      caller/       Pipecat pipeline: transports, VAD, STT, TTS, turn builder,
                    latency clock (audio-frame timestamps)
      judge/        rubric prompts (Jinja2), incremental + final scoring,
                    golden-file eval harness
      discovery/    explorer policy, flow-graph builder, gate detector
      redteam/      attack-pack loader (YAML), escalation logic, verdict grading
      reference_agent/   the controlled banking-style target (w/ planted leak)
    models/         SQLAlchemy models        schemas/  Pydantic (API + events)
    events.py       typed event constructors — the ONLY way events get emitted
  migrations/       Alembic
  contract/openapi.json   exported from FastAPI at build; committed; CI fails on drift
  attacks/          versioned attack packs (YAML)
  evals/            golden transcripts + labeled judge verdicts
```

**Contract sync (two repos, per the agreed model — direction now flips):** Python is the contract's source of truth. CI exports `contract/openapi.json` from the FastAPI app and fails if it differs from the committed file; contract changes tag `contract-vX.Y.Z`. The frontend's `pnpm sync:contract` fetches the pinned tag, runs `openapi-typescript` into `src/lib/api/generated/`, commits the output; `pnpm typecheck` is then the frontend's contract test. SSE event payloads are Pydantic models included in the exported schema (as named components), so stream types are generated too. BFF pass-through routes stay dumb pipes — they proxy `/v1` shapes verbatim and never re-map fields, or the contract guarantee dies at the middle layer.

## 3. Data model (brain DB)

By-value references to the auth world (`user_id` text = NextAuth id) — no cross-DB FKs. `org_id` on every table from day one, hardcoded to one org for MVP: multi-tenancy later becomes a WHERE clause, not a migration crisis.

```sql
agents(id, org_id, name, transport 'web'|'sip'|'phone', config jsonb,
       language, max_concurrency, status, last_seen_at, created_by_user_id, …)

suites(id, org_id, name, description, agent_id, created_by_user_id, …)
scenarios(id, suite_id, name, persona, persona_initials, script jsonb NULL,
          assertions jsonb, source 'manual'|'discovery_draft', source_draft_ref UNIQUE NULL)
          -- UNIQUE on source_draft_ref = idempotent "Add to suite"

personas(id, org_id, name, voice, language, accent, traits jsonb, builtin bool)

runs(id, org_id, type 'simulation'|'discovery'|'redteam'|'suite', status
       'queued'|'claimed'|'running'|'completed'|'cancelled'|'failed',
     agent_id, scenario_id NULL, parent_run_id NULL, config jsonb,
     idempotency_key UNIQUE NULL, created_by_user_id,
     claimed_by NULL, heartbeat_at NULL, started_at, ended_at,
     metrics jsonb NULL)                    -- score/avg_latency/wer/sentiment at completion

run_events(run_id, seq, type, data jsonb, ts,  PRIMARY KEY(run_id, seq))
     -- append-only spine: SSE frames, snapshots, replays, debugging — all read this

-- Materialized at completion for query/report convenience (events remain the truth):
turns(run_id, idx, role, text, latency_ms, flagged, flag_reason)
assertion_results(run_id, assertion_id, name, status, note, triggered_at_turn)
findings(id, run_id, category, severity, verdict, attack_prompt, agent_response,
         evidence NULL, suggested_fix, turn_refs int[])
         -- CHECK (verdict='blocked' OR evidence IS NOT NULL)   ← product logic in the schema
discovery_nodes(run_id, node_id, label, x, y, state, blocked_reason NULL)
discovery_edges(run_id, from_node, to_node)
discovery_intents(run_id, name, state, path, reason NULL)
discovery_drafts(run_id, draft_id, name, persona, assertions jsonb, added_scenario_id NULL)

attack_packs(id, name, version, category, spec jsonb, builtin bool)
schedules(id, org_id, suite_id, cron, enabled)          -- roadmap: regression runs
webhooks(id, org_id, url, events text[], secret)        -- roadmap: alerts/CI
api_keys(id, org_id, hash, scopes, created_by_user_id)  -- roadmap: public API
```

Notes: the `CHECK` on findings makes CLAUDE.md's "never bare pass/fail" a database guarantee, not a convention. Discovery's `blocked_reason`/`reason` columns are `NOT NULL WHEN state='blocked'` (trigger) for the same reason — the identity-gate explanation cannot be silently dropped.

## 4. Streaming protocol (SSE, resumable)

```
BFF route:  GET /api/runs/{runId}/stream            (Next.js, session-gated)
  → pipes:  GET /v1/runs/{runId}/stream             (Python, token-gated)
```

Frames — `id` is the `run_events.seq`, enabling `Last-Event-ID` resume:

```
id: 17
event: turn
data: {"index":8,"role":"agent","text":"…","latencyMs":1240,"flagged":true,"flagReason":"exceeded 1000ms target"}

id: 18
event: assertion
data: {"assertionId":"a3","name":"States card-block timeline","status":"passed","triggeredAtTurn":8}

: heartbeat                     ← comment frame every 15s keeps proxies alive
```

Event vocabulary (payloads are Pydantic models in the contract): `status`, `turn`, `metrics` (sim live bar), `assertion`, `node` / `intent` (discovery, **emitted progressively** — better UX and better demos), `attack`, `exposure` (red team), `done` (type-specific completion payload: score card | discoveryResult | findings+exposure), `error` (`fatal` bool). On connect without `Last-Event-ID`, Python replays all existing events for the run then follows live (LISTEN/NOTIFY wakeups); with it, replay starts at `seq+1`. A stream opened on a completed run replays everything and closes after `done` — which is precisely how Results-page "watch the replay" and offline demo mode work with zero extra machinery.

Control is REST, not stream: `POST /v1/runs/{id}/cancel`. One turn event = one row = one frame — persistence and streaming are the same write, so live view and Results can never disagree.

## 5. Execution plane (queue, workers, lifecycle)

- **Queue = the `runs` table.** `POST /v1/…/runs` inserts `status='queued'` and returns `202 {runId}` immediately. Workers claim with `SELECT … WHERE status='queued' AND type = ANY(my_types) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1` — the pattern already proven in this team's job pipeline. No Redis, no Celery broker to operate.
- **Heartbeats & reaping:** claimed/running workers update `heartbeat_at` every 10 s; a reaper marks runs with stale heartbeats (>60 s) as `failed` with `error` event `worker_lost` — no zombie "running" runs in the UI, ever.
- **Concurrency:** enforced at claim time against `agents.max_concurrency` (count of live runs per agent); `POST /runs` also pre-checks to fail fast with `409 concurrency_limit` + `retryAfterMs`.
- **Cancellation:** cancel sets `status='cancelled'` + NOTIFY; the executing worker checks a cancellation flag each turn, ends the call gracefully, runs the judge on partial data, emits `done` with partial metrics. Results renders partial runs.
- **Idempotency:** `Idempotency-Key` header → unique column; a double-clicked Run returns the original `runId`.
- **Run types as executors:** one worker binary, three executor classes (sim / discovery / red-team) sharing the caller runtime; `suite` runs (post-MVP) fan out child rows with `parent_run_id` and aggregate on children's completion.

## 6. Engine internals (the moat)

**Test-Caller runtime (shared by all three executors).** Pipecat pipeline: persona/explorer/attacker LLM (Gemini Flash) → Google TTS → LiveKit WebRTC room joined with the target agent ← agent audio → streaming Google STT with word timings → turn builder. **Latency is measured at the audio frame layer** (caller utterance end → agent's first audio frame), never at the LLM layer — the number must be what a human caller would experience. The 1 s flag threshold comes from org/environment config; the engine emits `flagged`, the UI just renders it. Barge-in/interruption counting via VAD overlap detection feeds the sim metrics bar.

**Judge/Scorer.** Two passes: an incremental per-turn judge (cheap, temperature 0) flips assertions live and moves the live score; a final holistic pass at call end produces the score on the card and per-assertion notes. Every verdict must cite `turn_refs`. Prompts are versioned Jinja2 templates (structured: signal definitions, distinguish-from clauses, mandatory analysis-before-JSON — the house style that's already proven in production eval work); every judge call traced to Phoenix with prompt version, inputs, and verdict. **Golden-file evals** (`evals/`): ~30 hand-labeled transcript+assertion pairs; CI runs them whenever a judge prompt changes — prompt edits are treated like schema migrations because scoring credibility *is* the product.

**Discovery executor.** Explorer policy LLM plans probe utterances against a running flow-graph model (nodes = states/intents, edges = observed transitions), detects verification gates from agent behavior, retries gated branches with the supplied dummy identity, and emits `node`/`intent` events as the map grows. A valid-format-but-wrong identity is **not an error** — the run proceeds and the gate comes back `blocked` with a human-readable `blocked_reason`; only format-invalid identity fails at `POST` time (`422` with per-field details). Turn budget (default 40) + frontier exhaustion guarantee termination. Drafts are generated from mapped/blocked paths with proposed assertions.

**Red-team executor.** Attack packs are versioned YAML: category, opening prompt, escalation follow-ups (multi-turn attacks — single-shot attacks are toy attacks), success signals for the judge, and the fix template. MVP ships ~10 hand-tuned attacks across the five UI categories, validated against the reference agent's planted leak; every attack/response pair is stored verbatim (it's just events), and the `findings` CHECK constraint makes evidence non-optional for leaks.

**Reference target agent.** A small controlled LiveKit banking-style agent (greeting → verification gate → 4–5 intents → one deliberately leaky behavior). First engine deliverable: it is the dev fixture, the CI target for the nightly live loop, the ground truth for judge validation, and the thing every prospect demo runs against so demos never depend on anyone else's infrastructure.

## 7. What the BFF layer adds (thin, but not zero)

Beyond proxying: Server Actions for mutations (already the pattern — `createRunAction` exists), the SSE pipe route with `maxDuration` set, per-user rate limits on run creation, and mapping Python error codes to UI states. It never re-shapes payloads (§2). The mock repository stays behind `API_MODE=mock` forever — it's the offline demo and UI-dev mode, now with true parity because replay-from-events matches its shape.

## 8. Endpoint inventory (`/v1`, Python)

| Domain | Endpoints |
|---|---|
| Agents | `GET/POST /agents` · `GET/PATCH/DELETE /agents/{id}` · `POST /agents/test-connection` (pre-save, sync ≤2 s handshake) · `POST /agents/{id}/test-connection` · `GET /agents/health` |
| Suites | `GET/POST /suites` · `GET/PATCH/DELETE /suites/{id}` · `POST /suites/{id}/scenarios` (manual **or** `{fromDraftId}`, idempotent) · `PATCH/DELETE /scenarios/{id}` · `POST /suites/{id}/run` (post-MVP fan-out) |
| Runs | `POST /simulations/runs` · `POST /discovery/runs` · `POST /redteam/runs` (bodies per screen; discovery requires `dummyIdentity`) · `GET /runs?type=&suiteId=&agentId=&status=&limit=` · `GET /runs/{id}` (full detail incl. type-specific sections) · `GET /runs/{id}/stream` (SSE) · `POST /runs/{id}/cancel` · `POST /runs/{id}/rerun` |
| Dashboard | `GET /metrics/dashboard` (cards + 14-bar volume + outcome donut, computed from runs) |
| Personas | `GET /personas` (built-ins for the sim voice select) |
| Later | `GET /runs/{id}/report` (PDF/JSON) · `POST /runs/{id}/share` · schedules · webhooks · api-keys (§10) |

Request/response JSON shapes: carry forward from `CADENCE_API_ARCHITECTURE.md` §4/§4.6 verbatim (they already match `src/types/index.ts`); the WS event table there maps 1:1 onto §4's SSE vocabulary. The screen-by-screen pass promised by BACKEND_SPEC.md should be written as FastAPI routers directly — the exported OpenAPI then *is* that document.

## 9. Deployment, security, observability

- **Topology:** `api` can run anywhere request-scoped (Railway/Fly/Cloud Run). **Workers need long-lived processes with real-time media** — Railway or Fly.io machines (or a small VM), not request-scoped serverless. LiveKit Cloud for media (self-host later if unit costs demand). Brain DB: second Neon project. Phoenix: existing self-hosted instance.
- **Network:** Python service private (VPC/private networking) or, where the host can't do private, public-but-token-only with the service token as the sole gate — and say which one in the runbook. Signed stream tokens (Amendment B) only if/when the proxy route hits duration limits.
- **Secrets:** `PYTHON_SERVICE_TOKEN` (Vercel + brain host), LLM/STT/TTS keys, LiveKit keys — env vars, rotated by redeploy.
- **PII discipline (regulated-industry table stakes):** red-team `evidence` can contain *real* data the customer's agent leaked — encrypt evidence columns at rest, access-log reads, exclude raw evidence from any future share links by default. Dummy identities are fake by definition but are stored in `runs.config`; treat with the same care (they're shaped like PII). Recordings, if kept (roadmap), get bucket-level encryption + TTL.
- **Observability:** OTel everywhere; every LLM/judge/STT call traced to Phoenix with run_id correlation; structured logs keyed by run_id; the `run_events` table itself is the first debugging tool (full replay of any run, forever).
- **CI:** unit tests + golden judge evals + OpenAPI-drift check per PR; **nightly live loop** — one sim + one red-team run against the deployed reference agent, alerting on failure — instead of flaky per-PR voice tests.

## 10. Roadmap — past MVP, past Bluejay/Cekura

Bluejay and Cekura both converge on the same triangle: simulation testing, evals, production monitoring. Matching the triangle is the MVP. Beating them means winning on axes they underweight — and the ones below are chosen because this team has an unfair advantage on each, not because they sound good on a slide.

**10.1 Realism as a product: condition & fault injection.** The team has already built voice-pipeline fault injection as Pipecat FrameProcessors (VAD/STT/LLM/TTS/tool-stage faults) for benchmarking eval platforms. Productize it: every scenario gets a **conditions profile** — background noise beds (street/call-center/home), codec/bandwidth degradation (narrowband 8 kHz PSTN simulation), packet loss/jitter, accent-varied TTS personas, barge-ins, mid-call topic switches, silence/dead-air probes. Competitors test the happy acoustic path; "your agent under a bad Airtel connection from a noisy branch office" is a demo no one else gives, and it comes almost free from work already done. This also unlocks **chaos suites**: the same scenario run across a condition matrix with a pass-rate-by-condition heatmap.

**10.2 Indic multilingual & code-switching as first-class, not a language dropdown.** Personas that speak Hindi, Hinglish, Tamil, Bengali… with mid-call code-switching (the actual behavior of Indian callers), language-consistency assertions ("agent must reply in the caller's language"), and STT/judge pipelines validated per language. US-centric competitors treat this as a checkbox; for Indian BFSI buyers it's the whole problem. Direct reuse of existing multilingual banking-agent experience.

**10.3 Compliance evidence layer.** Map red-team findings and assertion failures onto regulatory obligations (RBI digital-lending & KYC norms, IRDAI conduct rules, DPDP consent/data-minimization) and emit an **auditor-shaped evidence pack**: obligation → test performed → verbatim transcript evidence → verdict → remediation → retest link. This converts Cadence's output from "QA report" to "compliance artifact," changes the buyer from an engineering manager to a compliance officer, and is the SpectraAudit thesis given a distribution vehicle. No horizontal US competitor will out-local this.

**10.4 Production monitoring loop (their moat, closed differently).** Ingest real production calls (connector or S3/webhook drop of recordings+metadata) → run the same judge rubrics on them → drift dashboards (containment, latency percentiles, sentiment by intent) → and the differentiator: **auto-generate regression scenarios from real failed calls** (one click: this bad call becomes a test in a suite, with proposed assertions). Testing and monitoring stop being two products; every production failure permanently hardens the suite.

**10.5 CI/CD gate + developer platform.** Public API (`api_keys` table) + a GitHub Action: run a named suite against a staging agent on every deploy, fail the pipeline under a pass-rate threshold, post findings to the PR; webhooks to Slack/Teams on regression or new critical finding; scheduled runs (`schedules`) for nightly regression. Voice-agent teams ship weekly prompt changes with no regression net — being *in the deploy pipeline* is stickier than being a dashboard.

**10.6 Version intelligence.** Tag runs with an agent version/prompt hash; diff view between versions (which assertions flipped, latency delta per turn, new leaks); A/B a suite across two agent configs. Turns "did the new prompt break anything?" from vibes into a report.

**10.7 Attack intelligence.** Attack packs as versioned, shippable content (monthly drops: new jailbreak families, Indic-language social-engineering scripts, IVR-specific bypasses); custom attack authoring; retest-a-finding as a one-click follow-up. The YAML pack format (§6) is the seed of this content business.

**10.8 Enterprise deployment for banks.** VPC peering / single-tenant / on-prem worker images (the workers are just processes + Postgres — deliberately no exotic infra to make this possible), SSO (SAML/OIDC via the NextAuth side), RBAC (`org_id` already everywhere), audit logs, data-residency-in-India by default. Indian BFSI buys deployment posture as much as features; US competitors' cloud-only posture is an open flank.

**Sequencing honesty:** 10.1 and 10.2 first (unfair advantages, demoable, reuse existing work) → 10.5 (stickiness) → 10.4 (recurring-revenue engine) → 10.3 (changes the buyer) → 10.6/10.7 (compounding) → 10.8 (when a real enterprise deal demands it, not before). Do not start any of these before the MVP hero loop (real call → live turns → true latency → defensible score) is boringly reliable — that reliability is the actual moat; everything above is leverage on top of it.

## 11. Build order (updates execution-plan phases for this architecture)

- **B0** — Repo scaffold, Alembic schema v1 (§3), FastAPI skeleton with OpenAPI export + drift CI, service-token middleware, BFF env plumbing (`PYTHON_API_BASE_URL`, token), `API_MODE=dev` path in the frontend repository layer. **FakeRunner**: a worker that replays scripted event sequences through the real `run_events` → SSE path.
- **B1** — Agents + Suites + Runs CRUD real; dashboard metrics computed; SSE proxy route live; frontend `dev` mode renders everything from Python with FakeRunner streams. *(Contract pressure-tested here, amendments still cheap.)*
- **B2** — Reference agent deployed; caller runtime places a real call; sim executor streams real turns/latency; incremental judge flips assertions; final scoring writes metrics + materialized rows. **Hero loop real — the MVP existence proof.**
- **B3** — Real test-connection (WebRTC handshake), cancellation + reaper + concurrency hardening, nightly live loop in CI.
- **B4** — Red-team executor + first attack pack + findings (CHECK-constrained) + exposure scoring.
- **B5** — Discovery executor (progressive nodes, gate detection, drafts→suite) + soak (reconnect/resume, 5 concurrent runs) + demo polish. Cut line: discovery ships as curated replay (a real prior run replayed via events — first-class, not fake) before it ships fully live.