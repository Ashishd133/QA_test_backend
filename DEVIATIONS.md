# Plan Deviations

Tracks where the actual implementation (this repo + `cadence-brain`) has drifted
from `docs/Cadence_implementation_backlog.md` / `docs/BACKEND_SPEC.md`, and what
needs to happen to reconcile each one. Unlike `DECISIONS.md` (records a choice
that was made and is settled), this file tracks **open gaps between plan and
reality** — update it whenever new drift is discovered, and move an entry to
Resolved when it's fixed.

Format per entry: what deviates, why, the concrete fix, which repo it belongs
to, and status.

## Open

### 1. `docs/BACKEND_SPEC.md`'s streaming protocol is stale
**Deviates:** §3 describes `event: turn` / `data:{index,payload}` frames and
per-screen-prefixed routes (`/v1/{screen}/runs/{id}/stream`).
**Reality:** the backlog's B0-06/07 typed event catalog
(`status,turn,metrics,assertion,node,intent,attack,exposure,done,error`),
`id:<seq>` frames, and a flat `/v1/runs/{run_id}/stream` route — confirmed
against the live `contract/openapi.json` on `cadence-brain`.
**Fix:** update or retire `docs/BACKEND_SPEC.md`'s §3; treat the backlog +
live contract as source of truth, not this doc.
**Repo:** frontend (docs only). **Status:** open.

### 2. Provisional REST endpoint map (`src/lib/api/endpoints.ts`) — B1 shipped, map needs a pass
**Update (re-verified against live `main`):** B1's CRUD/run-creation endpoints
are now live: `suites`, `suites/{id}`, `suites/{id}/scenarios` (POST only),
`scenarios/{id}` (PATCH/DELETE), `agents` + `agents/{id}` + `agents/health` +
`agents/{id}/test-connection`, `runs` (GET list) + `runs/{id}` + `runs/{id}/cancel`
+ `runs/{id}/rerun`, `metrics/dashboard`, `personas`, and all three
`POST /v1/{simulations|discovery|redteam}/runs`. Diffing against
`endpoints.ts`, three concrete mismatches:
- `dashboardRuns()` guessed `/v1/dashboard/runs`, which doesn't exist — the
  real source is `GET /v1/runs?limit=N` (a query, not a dedicated path).
- `dashboardMetrics()` guessed `/v1/dashboard/metrics`; real path is
  `/v1/metrics/dashboard` (segment order reversed).
- `suiteScenarios()` is used for reading a suite's scenarios in
  `getScenarios()`; there's no such GET — scenarios come **embedded** in
  `GET /v1/suites/{id}` per B1-01 ("detail embeds scenarios with last-run
  fields"). `getScenarios()` needs rework, not just a path fix.
- `discoveryDraft()` still has no live counterpart — discovery drafts aren't
  in this endpoint set at all yet.
- `createRun()`'s dev/demo branch POSTs to `/v1/runs`, which doesn't exist
  (no generic run-creation route — only the three type-specific ones,
  already correctly used by `startRun()`). Currently harmless: this branch
  is unreachable in dev/demo mode today (`simulations-screen.tsx` gates the
  mock-only "persist completed run" call to `apiMode === "mock"`), but it's
  dead-wrong code that should be cleaned up, not left as a landmine.
**Fix:** small, mechanical `endpoints.ts` pass (4 path/shape fixes) +
`getScenarios()` rework to read from suite detail instead of a separate call.
**Repo:** frontend. **Status:** open, now unblocked (was blocked on brain B1).

### 3. `POST /v1/{simulations|discovery|redteam}/runs` — RESOLVED, now live
Confirmed live on `main`. B0-13's `startRun` already targets this shape
correctly. No frontend change needed — just re-run the B0-13 live
verification (start a real Simulations run end-to-end) now that it'll
actually succeed instead of 404.

### 4. `contract-v0.1.0` still never tagged, despite B1 being functionally done — RESOLVED
`contract-v1.0.0` (B1) and now `contract-v1.0.1` (B2-06) are both tagged and
pulled successfully. `contract.lock` bumped to `contract-v1.0.1` and
`pnpm sync:contract` re-run as part of W-01 (2026-08-06); nothing in this
repo consumes `generated/schema.d.ts` yet (grepped — zero imports), so
typecheck staying green on the bump was trivially true, not a real signal.
The item-2 mismatches above still need their own manual pass whenever
someone picks that ticket up.

### 5. Discovery and Red Teaming still use the mock timer even in `dev`/`demo` mode
**Deviates:** B0-13's Done-when only required Simulations; `useRunPlayback`
and `createSSEPlayback` are generic enough to reuse for the other two
screens but haven't been wired up.
**Fix:** repeat B0-13's pattern (split page into server wrapper + client
screen, wire `startRun`/SSE) for `/discovery` and `/red-team`. Not yet a
ticket ID in the backlog — should get one before starting.
**Repo:** frontend. **Status:** open.

### 6. `e2e/smoke.spec.ts` has no auth fixture — RESOLVED
Resolved while merging W2.5 to `main` ahead of W2.6: `e2e/global-setup.ts`
seeds a dedicated `e2e-test@cadence.dev` account via the project's own
`prisma db seed` and signs it in once via `/api/login/password`;
`playwright.config.ts` reuses that `storageState` across every test. Fixing
this uncovered a real, previously-masked bug: `/runs/tr-4821` in the route
list 404'd (`tr-4821` was never a seeded id — decisions #24/#25 collapsed
the Test Run permalink onto the same id space as `/results/[runId]`), fixed
to `/runs/run-4821`. See DECISIONS.md #27/#28.

### 7. "No raw audio stored" policy is being reversed, not yet anywhere but a plan file
**Deviates:** `src/components/agents/connect-screen.tsx` still states
*"Cadence never stores raw audio by default... PII is redacted before
scoring."* The UI-direction plan
(`~/.claude/plans/1-there-should-be-stateless-quokka.md`, section B)
reverses this per explicit user direction, but it's gated on Phase B2 (real
voice — no real audio exists before then) and on new object-storage infra
neither of which exist yet.
**Fix:** when actually implemented — log the reversal in `DECISIONS.md`
(superseding whichever entry documents the current policy), update the
`connect-screen.tsx` copy, add an object-storage ticket to `B-1-01`'s
account list, extend `B2-02`/`B1-03` per the plan file.
**Repo:** both. **Status:** open, not started.

### 8. "Agent switcher" isn't in the backlog under any ticket ID
**Deviates:** net-new scope from ideation (reuses the existing
`ConnectedAgent` type — see plan file section C), not part of
`docs/Cadence_implementation_backlog.md` at all.
**Fix:** once implementation is greenlit, add it as a real ticket (e.g.
`B1-09b`, since it depends on `ConnectedAgent` already being real) so the
backlog stays the single source of truth instead of drifting out of sync
with the separate plan file.
**Repo:** frontend. **Status:** open, not started.

### 9. Credit/usage bar has no backend counterpart anywhere — RESOLVED, see `#28`
**Deviates (original, now stale):** entirely new concept (metering/
billing) — zero mention in the backlog's phases or post-MVP epics table.
**Amendment (W2.8), then resolved (contract-v1.4.0):** `SpendSummary` is
now a real alias (`components["schemas"]["SpendSummary"]`) — credits
remaining is a project-level configured budget minus summed
`runs.cost.estUsd` for the window, `null` when no budget is configured,
bar hidden (not zeroed) when `null`, exactly as `B2.8-04` designed it.
Field names reconciled to `costPerCall`/`windowSpend` (no `Usd` suffix);
`costPerCall` is independently nullable from `windowSpend`. Full
reconciliation writeup under `#28`'s `## Resolved` entry.
**Repo:** both. **Status:** resolved.

### 10. LLM prompt-improve (`#5`) has a target surface but no ticket
**Deviates:** net-new capability; target surface decided (scenario test
script in Test Suites authoring, `B5-05`), but no backend endpoint or
ticket ID assigned.
**Fix:** add as a follow-on ticket after `B5-05` per the plan file section F
(e.g. `B5-05b`).
**Repo:** both. **Status:** open, not started.

### 11. W-02/W-03: `contract-v1.0.1` ships less than the two tickets assumed
**Deviates:** W-02 asks the repository layer to "send/accept projectId" so
"all reads/writes carry the project scope." The live contract has no such
input anywhere: `GET /v1/runs` takes no `projectId` query param, and none of
`SimulationRunCreate`/`DiscoveryRunCreate`/`RedteamRunCreate` accept one —
`projectId` is output-only on `RunDetail`/`DashboardRunRow`, applied
server-side via the migration's DB default. Separately, W-03 asks for a
transport/method field (e.g. "Web RTC") and separate created/started/ended
timestamps; `RunDetail` only has `createdAt` + `duration` (no `startedAt`,
`endedAt`, or transport field at all — the closest thing is the free-text
`agentMeta` string, e.g. `"Clinic Concierge (WebRTC)"`, already displayed).
**Fix implemented:** built what's real — `getDefaultProject()` (fetch-once
via `GET /v1/projects`, mock returns `proj_default`) as the swap-in point
for a future switcher; mock-mode `createRun` now stamps `projectId` on
created runs for parity with the backend's server-side default; `endReason`/
`cost`/`projectId` render wherever the backend actually returns them, null
fields rendering nothing. Transport/method and started/ended timestamps were
**not** added — there's no data to back them. If real project-scoped
reads/writes or those extra timestamp fields are wanted, that needs a
contract change on `cadence-brain` first (new query param + create-body
field, or new `RunDetail` columns) — no amount of frontend plumbing can
produce a value the backend never sends.
**Repo:** frontend (this doc) now; brain if/when the scoping or extra
timestamp fields are actually wanted. **Status:** open by design — not a bug,
a scope note for whoever revisits either ticket.

### 12. W-04: no Playwright coverage added — blocked on #6, not skipped by choice
**Deviates:** W-04's done-when calls for Playwright covering both the
drawer-open and full-page-load paths. `e2e/smoke.spec.ts` still has no auth
fixture (#6 above, pre-existing and unrelated to this change) — every route
redirects to `/login` before rendering, so a new spec here would silently
only exercise the login redirect and report false-green coverage.
**Fix:** added `src/components/results/run-detail-drawer.test.tsx` (RTL)
instead, covering the actual open/fetch/close/error mechanics. Real
Playwright coverage for both paths should get added now that #6's auth
setup project has landed — same fixture, one piece of work, not duplicated
per ticket.
**Repo:** frontend. **Status:** open, unblocked (#6 resolved).

### 13. `recordingUrl` (W-05) — live on `main`, not yet in a tagged contract
**Deviates:** `recordingUrl: string | null` on `RunDetail` is confirmed live
on the backend's `main` branch (re-checked directly: present in `RunDetail`'s
schema, optional + nullable) and confirmed first-hand by the user (exact
field, format `audio/ogg` via GCS signed URL, ~1hr expiry). `contract.lock`
is still pinned to `contract-v1.0.1`, which predates this field — no tag
exists yet that includes it. Hand-added to `src/types/index.ts` ahead of a
formal tag, same precedent as W-03's `cost`/`endReason`/`projectId` —
nothing in this repo consumes the generated `schema.d.ts` yet, so this
doesn't block on a sync (unlike a real ambiguity — the shape here is
confirmed correct, just not packaged).
**Fix:** once backend tags a new contract version including this field,
bump `contract.lock` and re-run `pnpm sync:contract` — mechanical, the
hand-added type already matches.
**Repo:** frontend (this doc); brain (the tag). **Status:** open, low
urgency.

### 16. Pre-B2.5 types (`Suite`, `Persona`, `AgentHealth`, `ConnectedAgent`,
...) still hand-authored despite having real `contract-v1.1.0` schema
equivalents now
**Deviates:** item #15's fix (below, resolved) only wired up the types
touched by the W2.5 reconciliation (`RunDetail`, `RunAggregate`,
`DashboardRunRow`, `Organization`, `Project`, `RunCost`, `TranscriptTurn`,
`ResultAssertion`). Everything else in `src/types/index.ts` — `Suite`,
`Scenario`, `Persona`, `AgentHealth`, `ConnectedAgent`, etc. — is still a
hand-authored guess never diffed against `generated/schema.d.ts`, even
though the contract now has real schemas for most of them.
**W2.8 amendment:** `DashboardMetrics` dropped from this list — not
reconciled in place, *deleted outright* and replaced by `DashboardV2`
(hand-authored ahead of `contract-v1.4.0`, see `#28`), since the real
`B2.8-01` endpoint supersedes the v1 shape entirely rather than extending
it. `Persona` and `Metric`/`TestProfile` have also since left this list
(real aliases now, `#25`/`#26`/`#27`) — `Suite`, `AgentHealth`,
`ConnectedAgent` remain.
**Fix:** a follow-on pass, not urgent — none of these are known-wrong, they
just haven't been checked. Do it opportunistically per-screen as each one's
phase comes up (e.g. `Persona` when `W2.7-03`'s library screen lands), same
pattern this reconciliation used for the Run/Call types, rather than one
big sweep.
**Repo:** frontend. **Status:** open.

**W2.6-01 addendum:** the run-config modal's phase is exactly the
"opportunistically per-screen" trigger #16 names — two pieces landed now,
the rest deliberately didn't:
- `ConnectedAgent` gained an *optional* `maxConcurrency` (the modal's
  concurrency warning wants a real number). **Correction, found by testing
  the deployed app against the real backend, not just reading the
  schema:** this field was originally added as *required*, on the claim
  that "`AgentDetail.maxConcurrency` is required, real, on the wire
  already." That's true of `AgentDetail` — but wrong to apply to
  `ConnectedAgent`. `getConnectedAgents()` calls `GET /v1/agents`
  (`list_agents` in the generated schema), whose response type is the
  schema's own `ConnectedAgent`, which has no `maxConcurrency` field at
  all — confirmed live: a real deployed agent's JSON simply omits the
  key. `maxConcurrency` only exists on `AgentDetail`, the *single*-agent
  `GET /v1/agents/{id}` response, which nothing in this app calls. Fixed
  by making the field optional and auditing every read site
  (`run-config-modal.tsx`'s dropdown label and concurrency-warning calc)
  to treat `undefined` as "unknown," not crash or render "(max undefined
  concurrent)." The warning simply never fires in real/dev mode now,
  rather than guessing at a number nobody sent — consistent with #20's
  "don't fabricate data that makes mock look more complete than real"
  principle. Nothing else on `ConnectedAgent` was touched.
- `Suite.agent` is still a display name string, not an id, and
  `SuiteRunCreate.agentId` needs a real one. `repository.ts`'s
  `agentIdForName` resolves it by matching `ConnectedAgent.name` — the one
  reconciliation point, same pattern `getRunsDelta`'s mapping comment
  describes — rather than every caller guessing. This is a real gap, not a
  cosmetic one: two agents with the same display name would collide, and
  nothing enforces uniqueness on that field today. Fixed properly once
  `Suite` carries its own `agentId`.

### 17. Mock seed ids aren't valid UUIDs (`run-4821`, `tr-4821`, `org_acme`,
`proj_default`, ...) — real contract path params are `format: uuid`
**Deviates:** `contract-v1.1.0`'s `run_id`/`project_id`/etc. path parameters
are typed `format: uuid` in the OpenAPI spec; this repo's mock-mode ids are
plain strings with prefixes.
**Reality:** harmless by construction — mock mode never sends its ids to
the real backend (isMock() branches short-circuit before any `pythonFetch`
call), and dev-mode ids always come from the real backend's own responses
(e.g. the org/project switcher's dev-mode options come from
`getOrganizations()`'s live call, never from mock seed data). `e2e/smoke.spec.ts`
hardcodes `/runs/run-4821` and `/results/run-4821` in its route list — these
only pass because the e2e run defaults to `API_MODE=mock` locally; they
would 422 against a real backend.
**Fix:** none needed unless mock and dev ids ever need to be
interchangeable (e.g. a future fixture-replay mode). Noting it so nobody
"fixes" the mock ids into uuid-shaped strings for cosmetic consistency and
loses their readability for no reason.
**Repo:** frontend. **Status:** open, low urgency, informational.

### 18. Phase W2.6 (batch runs & the call queue) built ahead of `contract-v1.2.0` — RESOLVED
Resolved 2026-08-14: `QA_test_backend` tagged and pushed `contract-v1.2.0`
(B2.6-01/02/04). `contract.lock` bumped, `pnpm sync:contract` re-run, and
every hand-authored piece this entry originally flagged reconciled against
the real schema — same mechanical process #14 used. **What the real tag
confirmed vs. what it left open:**
- **Now real:** `POST /v1/suites/{suite_id}/run` (B2.6-01) — `SuiteRunCreate`
  {agentId, scenarioIds?, personaIds?, conditionProfileIds?} ->
  `SuiteRunCreateResponse` {parentRunId, callCount}, `Idempotency-Key`
  header, 422 on non-empty `personaIds`/`conditionProfileIds` until B2.7
  wires them to scenarios (server rejects rather than silently drops them —
  its own docstring says so). `RunTrigger` (manual/schedule/ci/api) — real,
  **required** (not optional) on both `RunDetail` and `DashboardRunRow`,
  which is what made this a mechanical typecheck-driven reconciliation
  rather than a guess: every hand-authored `RunDetail` literal in
  `seed.ts`/test fixtures needed a `trigger` added, `tsc --noEmit` named
  every site. `rerun_run` gained `?only=failed|all` and its 202 response is
  now `RunCreateResponse | SuiteRunCreateResponse` (single vs. batch
  rerun) — matches `cancelRun`/`rerunRun`'s already-real usage from this
  same phase, extended rather than replaced.
- **Still open, and this is the one this entry's fix didn't close:** no
  schema in `contract-v1.2.0` carries per-call identity on a *list* row.
  `RunDeltaRow` and `DashboardRunRow` still lack scenario/persona; `RunDetail`
  still has no `persona` field anywhere. `SuiteRunCreate` accepts
  `scenarioIds`/`personaIds` on the way in (so the backend does know a call's
  scenario/persona) but `SuiteRunCreateResponse` only returns
  `{parentRunId, callCount}` — no per-call breakdown. `QueueCallRow`'s
  `scenarioName`/`persona`/`agentName` fields (`src/types/index.ts`) stay
  hand-authored; `getRunsDelta`'s dev-mode branch still maps a real
  `RunDeltaRow` with those three `null`. Tracked onward as #19 rather than
  left inside this now-resolved entry, so it doesn't get lost the way #17
  warned about.
- **`RunLifecycleStatus`** (`src/types/index.ts`) is unaffected by this
  sync — still aliased from `StatusData.status`, still an assumption about
  `RunDeltaRow.status` specifically (that field stayed `string` on the wire
  in v1.2.0 too). Also carried into #19.

### 19. No per-call identity (scenario/persona) on any real list row, even after `contract-v1.2.0`
**Deviates:** `QueueCallRow`'s `scenarioName`/`persona`/`agentName` and the
`RunLifecycleStatus` narrowing of `RunDeltaRow.status` remain hand-authored
— see #18's resolution for the full history of what v1.2.0 did and didn't
settle. Not a regression, just the piece #18 couldn't close because the
backend hasn't shipped it yet.
**Fix:** once a future contract tag adds per-call identity to a list-row
schema (`RunDeltaRow` or a successor), fold `QueueCallRow`'s three identity
fields and the `RunLifecycleStatus` cast in `getRunsDelta` into real types —
`getRunsDelta`'s mapping function is the single reconciliation point by
design (see its comment in `repository.ts`).
**Repo:** frontend (this doc) now; brain for the schema. **Status:**
resolved backend-side (2026-10-14, branch `fix/deviations-19-20-21-37`,
commit `3b555a8`) — pending merge + a contract tag; frontend reconciliation
still to do after that.

**Resolution (backend):** `RunDeltaRow` now carries `scenarioName`
(nullable), `persona` (nullable) and `agentName`, via joins in
`_DELTA_ROWS_SQL` (`app/api/runs.py`). Once tagged, fold `QueueCallRow`'s
three identity fields into the real type; the `RunLifecycleStatus` cast in
`getRunsDelta` is unchanged by this.

**W2.6-03 addendum:** the call drawer's live-transcript path
(`useLiveCallTranscript`, gated on `apiMode !== "mock"` and the opener's
`live` hint) is contract-real — it opens the same `createSSEPlayback`
`EventSource` the results page already uses — but it is currently
unexercisable end-to-end in dev mode too: `getRunsDelta` only returns rows
once a real batch exists, and no ticket before W2.6-01 (run config + fan-out)
produces one. So there is today no reachable non-terminal row to open the
drawer on with `live: true` outside of a unit test. The standing proof it
works is `use-live-call-transcript.test.ts`'s 7 tests (open/close/dedupe
against a fake EventSource), not a manual click-through — don't read the
absence of a demoable path as the feature being broken.

### 20. `DashboardRunRow` can't carry the History table's own column list
**Deviates:** W2.6-04's ticket lists nine columns for the History table —
name, suite, trigger, calls, pass rate, p95, duration, cost, started.
`DashboardRunRow` only has four of the identity/metric fields (`suite`,
`agent`, `passRate`, `duration`) plus `trigger`/`cost`/`status`; it has no
`title`/name distinct from `suite` (confirmed against
`Cadence.redesign.html`'s History markup, which draws "Test run" and
"Suite" as two visibly different strings — "Nightly regression" vs "Block &
replace, KYC gate" — not one column shown twice), no call count, no p95
latency, and no timestamp of any kind (not even a display string — `RunDetail`
itself only has `createdAt`, and that isn't threaded onto the row type).
The backend already computes `callCount`/`latencyP95Ms` on every read
(`RunAggregate`, per `_compute_aggregate` — see `/runs/[testRunId]/page.tsx`'s
docstring); it just isn't exposed on this particular row shape. `title` and
a `started`/`createdAt` timestamp aren't computed anywhere for a list row
either.
**Not fixed by enriching mock:** `toDashboardRunRow` is the one shared
mapping every consumer (Dashboard, History, this ticket) reads through —
giving it mock-only extra fields would make History look complete against
the fixture and silently degrade to placeholders against the real backend,
which is the exact mismatch DEVIATIONS.md #19's addendum was written to
call out elsewhere. `history-table.tsx` drops the separate "Test run" name
column entirely (Suite stays the one identity column, as it already was —
a column that would read "—" on every single row isn't worth the width) and
shows "—" for Calls/p95/Started, which are real, named columns, in every
mode, mock included, rather than let the mock fixture imply a completeness
the wire doesn't have. Cost is the same gap in miniature for `run-batch1`
specifically: a batch parent's own `cost` field is genuinely `undefined`
(nothing to sum children into — `aggregateForBatch`'s `totalCostUsd` stays
`null`, its own real gap, not this one's), so its Cost cell reads "—" too,
consistently, rather than fabricating a parent-level number no aggregate
carries.
**Fix:** add `title` (or a `name` distinct from `suite`), `callCount`,
`latencyP95Ms`, and a `startedAt`/`createdAt` timestamp to `DashboardRunRow`
— a schema ask against the brain repo, not something the frontend can
resolve by itself.
**Amendment (contract-v1.4.0, `#28`):** resolved for the Dashboard
specifically — `B2.8-01` gave the dashboard's own recent-runs widget a
dedicated `RecentRun` schema (`{id, name, agentName, trigger, callCount,
passRate, status}`) instead of reusing `DashboardRunRow`, and it genuinely
carries `callCount`. This entry's gap still stands for `DashboardRunRow`
itself — the History table (`history-table.tsx`) still has no `callCount`/
`latencyP95Ms`/`title`/timestamp, since that table wasn't in `B2.8-01`'s
scope.
**Repo:** frontend (this doc) now; brain for the schema. **Status:**
resolved backend-side for History too (2026-10-14, branch
`fix/deviations-19-20-21-37`, commit `f0e4c5e`) — pending merge + a
contract tag; resolved for the Dashboard's recent-runs widget earlier.

**Resolution (backend):** `DashboardRunRow` gained `title` (same
`_run_title` as `RunDetail.title`: scenario name, else "Suite Run" etc.),
`callCount` (child count, or 1 for a childless run), `latencyP95Ms` (p95
over the run's own turns, or its children's for a batch parent; null with
no turns yet) and `createdAt` (ISO string, like `RunDetail.createdAt`; used
instead of `started_at`, which is null while queued). `history-table.tsx`
can drop its "—" placeholders after the tag.

### 21. W2.6-01's speculated `409 concurrency_limit` and `422 batch_too_large` don't exist in `contract-v1.2.0`
**Deviates:** the ticket (written speculatively ahead of the real contract,
per its own `(speculative)` tag) asks to "handle 422 `batch_too_large` and
409 `concurrency_limit` with the retry hint." Checked directly against
`generated/schema.d.ts`'s `run_suite` operation: the only response codes
declared are `202` and a generic `422` whose body is `HTTPValidationError`
— FastAPI's plain pydantic `detail: ValidationError[]`, not a structured
`{code: "batch_too_large"}`. No `409` is declared anywhere for this
endpoint, or anywhere in the schema at all. The *only* 422 the contract
actually documents for `run_suite` is the one named in `SuiteRunCreate`'s
own docstring — `not_supported`, for non-empty `personaIds`/
`conditionProfileIds` — a business rule this ticket's own persona picker
avoids entirely (see the modal's docstring: personas render as a disabled
placeholder, never submitted, so that specific 422 shouldn't be reachable
through normal use of this UI).
**Fix (what this ticket builds instead):** a single generic 422 handler
that surfaces `detail[0]?.msg` verbatim — the backend's own validation
message, not a fabricated "the cap is X" hint sourced from a field that
doesn't exist — plus the client-side "concurrency warning" the ticket's
body text also asks for (`N` vs the selected agent's real
`maxConcurrency`, entirely pre-submit, never a server response). No `409`
branch is built; building one for a status the server can't produce would
be the same kind of fiction this repo's own conventions already reject for
data fields, just applied to an error code instead.
**Repo:** frontend (this doc) now; brain if a real concurrency-limit
response is ever added. **Status:** see the "Stale as of `contract-v1.5.0`"
note below — the backend contract half is resolved.

**Stale as of `contract-v1.5.0` — re-check, not yet reconciled:** both
status codes this entry said didn't exist now do. Confirmed directly
against the current backend source (not re-checked against a regenerated
schema yet): `app/api/runs.py` returns a real `409 concurrency_limit` on
single-run create (keyed off `agent.max_concurrency`), and `app/api/
suites.py`'s `run_suite` returns a real `422 batch_too_large` (keyed off
`suite_run_batch_cap`) — the exact two codes this entry's own ticket
originally speculated and this entry said the contract didn't back up.
Checked the currently-synced `contract-v1.5.0` schema too, not just the
code: neither code appears in `schema.d.ts` either — this isn't a "re-run
`pnpm sync:contract`" fix, the OpenAPI spec itself has never documented
behavior the backend actually has at runtime, a real contract drift, not
a staleness problem on the frontend's side. `run-config-modal.tsx`'s own
comment ("contract-v1.2.0 never shipped [concurrency_limit]... entirely
client-side") is now factually wrong and needs updating once this is
reconciled. Today the real `409` falls through to the modal's generic
422/error-message handler rather than the originally-designed "retry
hint" UX — not a crash, just the specific affordance this ticket's own
text asked for, unbuilt. **Status:** open — needs the backend's own
OpenAPI spec corrected to declare these responses, then a frontend
reconciliation pass (confirm the exact error-envelope shape for each,
build the dedicated 409 state).

**Resolution (backend, 2026-10-14, branch `fix/deviations-19-20-21-37`,
commit `e9a0ecc`) — pending merge + a contract tag:** the standard error
envelope is now a declared schema (`ApiErrorResponse` =
`{error: {code, message, details?}}`, `app/schemas/errors.py`) and is
declared where it is raised: `409` (`concurrency_limit`,
`error.details.retryAfterMs`) on `POST /v1/{simulations,discovery,redteam}
/runs` and `POST /v1/runs/{id}/rerun`; `422` (`batch_too_large`,
`not_supported`, `validation_error`) on `POST /v1/suites/{id}/run` and
rerun. `run_suite` itself has no 409. Runtime behavior is unchanged.
**Frontend note:** on those two endpoints the `422` body is now typed as
the envelope, not `HTTPValidationError`. That matches the real runtime
body (`app/errors.py`'s validation handler always returned the envelope),
so a handler reading `detail[0]?.msg` was already wrong; it should read
`error.message`/`error.details`. Remaining work is frontend-only: update
`run-config-modal.tsx`'s comment and build the dedicated 409 retry state.
**Status:** resolved backend-side.

### 22. In mock mode, a Route Handler's write to `repository.ts`'s `db` is invisible to a Page Server Component's read — Route Handlers and Pages compile into separate module graphs
**Deviates:** nothing in any prior ticket's `Mock:` line, but it's the
reason one of them (`rerunRun`, W2.6-04) reads the way it does. Discovered
building W2.6-01's "Submit -> route to the new Test Run": `startSuiteRun`'s
mock branch wrote a real parent + children into `db.runs` from
`/api/suites/[suiteId]/run`'s Route Handler; `router.push`ing to
`/runs/{parentRunId}` right after 404'd, because `/runs/[testRunId]/page.tsx`
(a Page Server Component) read from a *different instance* of the same
`repository.ts` module — its own `db` never saw the write. Confirmed with
curl, not inferred: `POST /api/suites/{id}/run` then `GET /api/runs/{id}`
(both Route Handlers) sees the write; `GET /runs/{id}` (the Page) 404s on
the identical id. This app's dev server (`next dev`, no Turbopack) compiles
Route Handlers and Pages as separate entries, and a module-level mutable
singleton like `db` doesn't survive across that boundary even within one
Node process — this is a structural property of how Next's App Router
separates route types (mirroring how they're literally separate functions
in a production deployment), not a bug introduced by anything in this repo.
In hindsight, this is almost certainly *why* `rerunRun`'s own mock branch
(W2.6-04) never materializes a batch and its caller (the drawer's footer)
deliberately shows an inline message instead of navigating — that choice
predates this writeup but was never explained until now.
**Fix:** `startSuiteRunAction` (`src/lib/actions.ts`) — a Server Action
calling the same `startSuiteRun`, invoked from the modal instead of
`fetch`. Server Actions compile into the *page* module graph, so the
write and the read that follows share the same `db` instance; verified
against the same curl-style reproduction (submit through the UI, land on
`/runs/{id}`, see the real aggregate).

**Addendum:** the `/api/suites/[suiteId]/run` Route Handler that
originally hit this bug was kept, briefly, on the reasoning that it's
real and contract-shaped and some other caller might reach it over HTTP.
That reasoning didn't survive review: nothing in this repo calls it once
the modal moved to `startSuiteRunAction`, no test exercises it, and its
own mock branch has the identical unreachable-write problem this entry
describes — a POST that a plausible in-app follow-up read could never
see. Keeping an unreferenced, untested write endpoint around because it
might have a use someday is exactly the "looks more complete than it is"
failure #20 already rejected for fabricated data; the same principle
applies to fabricated-looking routes. Deleted rather than kept-and-
labeled. If a real external caller needs `POST /suites/{id}/run` over
HTTP, it should be re-added at the point that caller exists, tested
against that caller's actual read path, not speculatively ahead of one.
**Watch for:** any *future* mock-mode feature that writes via a Route
Handler and expects a Page Server Component to see it in the same request
cycle will hit this exact wall. The fix is always the same — go through a
Server Action (or a Server Component's own direct `repository.ts` call)
instead of `fetch`ing a Route Handler — not a `db` redesign.
**Repo:** frontend. **Status:** resolved for this ticket; open as a
standing constraint on mock mode's architecture.

### 23. `upsertAgent`'s real branch (agent connect/edit) doesn't match the real contract at all — wrong verb, wrong endpoint for new agents, wrong body shape
**Deviates:** found by schema inspection while post-deploy-verifying
W2.6-01 against the live production backend (not live-tested itself —
see Status). `repository.ts`'s `upsertAgent(agent: ConnectedAgent)` real
branch always does `PUT pythonEndpoints.agent(agent.id)` —
`PUT /v1/agents/{agent_id}` — regardless of whether `agent` is a brand
new connection or an edit of an existing one. Three separate problems,
each real on its own:
1. **Wrong verb.** The generated schema marks `put?: never` for
   `/v1/agents/{agent_id}` — only `get`/`delete`/`patch` (`update_agent`)
   exist there. A `PUT` would be rejected before the backend even looks
   at the body.
2. **No create path for new agents.** `connect-screen.tsx`'s
   `saveAgent()` — the only caller — always builds a brand-new,
   client-generated id (`` `agent-${Date.now()}` ``, not a real UUID) and
   calls `upsertAgent`, which always targets the single-agent endpoint.
   The real contract's create path is a *different* endpoint entirely —
   `POST /v1/agents` (`create_agent`) — never called anywhere in this
   repo. "Connect a voice agent" has no working real-mode path at all,
   not just a wrong verb on an existing one.
3. **Wrong body shape even for a genuine edit.** `upsertAgent` does
   `JSON.stringify(agent)`, sending the full hand-authored `ConnectedAgent`
   shape (`id`, `name`, `meta`, `tag`, `status`, optionally
   `maxConcurrency`). The real `update_agent` operation's body is
   `AgentUpdate`: `{ config?, language?, maxConcurrency?, name? }` — no
   `id`/`tag`/`status`, and critically no `meta` string at all. The
   connection details this screen exists to capture (WebRTC/SIP/phone
   address, credentials) live in `AgentUpdate.config`, a discriminated
   union (`WebConfig | SipConfig | PhoneConfig`) — a shape `ConnectedAgent`
   has never modeled. Fixing only the verb would not make this screen
   work; it would most likely 422 on the unrecognized/missing fields, or
   — worse — succeed while silently doing nothing to the agent's actual
   connection config, since `meta` isn't a field the backend reads.

**Why this surfaced now, and what it almost made worse:** W2.6-01 added
`ConnectedAgent.maxConcurrency` and, until reviewed, had
`connect-screen.tsx` send a hardcoded `maxConcurrency: 1` on every save.
`AgentUpdate.maxConcurrency` is a real, backend-stored field, not
display-only — had `upsertAgent`'s verb been fixed without also
fixing the body, every agent connected through this form would have had
its real concurrency capacity silently set to 1 the moment the request
started actually reaching the backend. Removed the hardcoded value
instead (see `ConnectedAgent.maxConcurrency` going optional, this file's
#16 addendum) — the form has no concurrency control, so it sends none,
rather than inventing a number that happens to type-check.
**Fix:** see `#31` (`W3-01`) — resolved. `upsertAgent` replaced with a real
`createAgent` (`POST /v1/agents`, `AgentCreate` body). `updateAgent`/
`AgentUpdate` — the gap this entry originally flagged — is now built too,
see `#33` (`W3-01c`).
**Status:** resolved (not live-tested against production — see `#31`;
the standing "no live writes without explicit go-ahead" rule still
applies to the real branch).

### 24. `RunDetail.latencySeries` is milliseconds; the turn-latency bar chart assumed seconds — RESOLVED
**Deviates:** found by actually clicking through the deployed app (not
schema inspection this time — the schema just says `latencySeries:
number[]`, no unit). Navigated to a real call's Test Run detail page and
the page rendered with a solid-color block covering roughly the right
half of the screen top to bottom. Traced it to `RunLatencyCard`
(`run-detail.tsx`) — its `BarChart` bar for one turn had `height:
"75385%"` and a computed pixel height in the tens of thousands,
swallowing everything below and to the right of it in the page's flow.
`CallLatencyTab` (`call-drawer-tabs.tsx`, W2.6-03's drawer) had the
identical logic, copy-pasted — same bug, same blast radius, just not yet
clicked into during that phase's own live check.
**Root cause:** both call sites computed
`heightPct: Math.round((x / 1.3) * 100)` and `color: x > 1 ? amber :
accent` — correct only if `x` is *seconds*. Confirmed via the raw API
response (`GET /api/runs/{id}`, not inferred from pixels) that a real
call's `latencySeries` is `[420, 980]` — milliseconds, consistent with
that same call's transcript turn labeled "420ms" and its aggregate's
`latencyP95Ms: 952`. `seed.ts`'s `latencySeriesSeed` was authored as
`[0.7, 0.8, 0.8, 1.2, ...]` — small decimals that happen to already look
like plausible seconds, so mock mode never disagreed with the (wrong)
seconds assumption in the chart code. Neither side was ever checked
against what the real backend actually sends; both carried the same
wrong unit, so nothing ever caught it locally, in a unit test, or in
`e2e/smoke.spec.ts` (all three run in mock mode). The exact failure mode
DEVIATIONS #16/#22/#23 have each independently surfaced this session:
mock-mode fixtures make an assumption look confirmed when it never was.
**Fix:** extracted a shared `latencyBars()` (`charts/bar-chart.tsx`),
used by both call sites, that treats the input as milliseconds
(`heightPct` scaled against a 1300ms ceiling, matching the original
"1.3" intent expressed in the real unit; `color` threshold at 1000ms;
label `${ms}ms` not `${x}s`) and **clamps to 100%** regardless — so a
future real call whose slowest turn genuinely exceeds the ceiling caps
out visually instead of reproducing this exact failure again.
`seed.ts`'s `latencySeriesSeed` (and `simulations-screen.tsx`'s
copy-pasted inline duplicate of the same fixture) converted to
milliseconds to match. New test (`bar-chart.test.tsx`) pins the exact
real payload (`[420, 980]`) to a sane, clamped result.
**Repo:** frontend. **Status:** resolved.

### 25. Phase W2.7 (suite screen restructure + scenario editor drawer) built ahead of `contract-v1.3.0`
**Deviates:** originally implemented backlog tickets `W2.7-01`/`W2.7-02`
against `Cadence.redesign.html`'s `#v-suites`/`#dr-scen` sections, ahead
of `contract-v1.3.0` (`B2.7-02/03/04/08/10/11/12`) tagging — same "build
ahead, reconcile later" precedent as `#14`/`#18`. `contract-v1.3.0` has
since tagged and been synced (`pnpm sync:contract`); this entry now
tracks what's been reconciled against it versus what's still genuinely
hand-authored. **The repo is no longer pinned to `contract-v1.2.0`.**

**Reconciled this pass** (each a separate commit on
`w2.7-suite-screen-restructure`, "implement → verify → commit"):
- **Suites** — `folder` and `rubric` (opaque `{[key:string]:unknown} |
  null`) are real fields now; `createSuite`/`updateSuite`/`deleteSuite`
  are real POST/PATCH/DELETE, same create-vs-update endpoint split as
  every other resource this pass (the `#23` lesson). `rubricSummary`
  (hand-authored string) is **gone**, replaced by the real `rubric`
  field — see the rubric-convention note below.
- **Scenario writes** — `upsertScenario`/`deleteScenario`/
  `duplicateScenario` are real (`POST /v1/suites/{id}/scenarios` create,
  `PATCH /v1/scenarios/{id}` update, `DELETE`, `POST .../duplicate`).
  `goal` and `assertions` are genuinely editable and persist within a
  session in both modes. **But see the Backend gap below** — this is
  where it actually bites, not just a "some fields still fake" note.
- **Scenario generation** — `generateScenarios` (`POST
  /v1/suites/{id}/scenarios:generate`) is real; acceptance reuses
  `upsertScenario` with `fromDraftId` (idempotent both server-side via
  `source_draft_ref` UNIQUE and mock-side via a tracked draftId ->
  scenarioId map).
- **Test Profiles** — full real CRUD now backs a real `/settings` screen
  (was a bare placeholder) — see the `fields`-bag convention note below.
- **Metrics** — see `#27`, amended separately.

**Backend gap to file — this is the session's most consequential
finding, not a footnote:** `update_scenario`/`add_scenario` both return
`ScenarioSummary`
(`id`/`suiteId`/`name`/`persona`/`personaId`/`assertCount`/`status`/`score`/`runId`
only) — there is no `ScenarioDetail` and no `GET /v1/scenarios/{id}`.
A real write to `goal`/`assertions`/`testProfileId` succeeds, but nothing
can ever read it back — not a suite refetch, not a page reload. This
single gap dictated real architecture: `suite-screen.tsx` holds its own
`scenarios` local state (not a direct read of the `suite` server prop)
specifically so a saved scenario's edit has somewhere to live, and
scenario saves deliberately **never** call `router.refresh()` in either
mode (a refresh would immediately re-render from the bare
`ScenarioSummary` and wipe the edit that just succeeded, in the same
interaction, not just on a later reload). `upsertScenario`'s real-mode
return is honest about the gap: it's a plain object literal carrying
*only* the keys `ScenarioSummary` actually has, as genuinely absent keys
(not `undefined` values), so the drawer's three-way merge
(`{...scenario, ...appShapedChanges, ...result}`) can spread it last
without clobbering what it can't echo. **What to ask the backend for:**
a `GET /v1/scenarios/{id}` returning the full write shape back, or
`ScenarioSummary` growing the missing fields directly. **Second,
independent gap on the same shape:** `ScenarioSummary` also carries no
`metrics` — there is no read path for a scenario's
`ScenarioMetricAttachment[]` at all, which is the other (not the only)
reason "Attach metric" stays disabled below; fixing the config/result
shape mismatch alone would not unblock it without this too.

**App-invented conventions living inside otherwise-real, contract-opaque
bags** — each of these is this app's own schema for a field the wire
only describes as `{[key:string]:unknown}`, so each is a silent-drift
risk the day the backend starts writing to it on its own terms:
- **`Suite.rubric`** — `SuiteRubric = {gatingMetricIds: string[],
  minPassRate: number, failOnCritical: boolean}` (`src/lib/suites.ts`'s
  `getSuiteRubric`/`formatSuiteRubricSummary`, read defensively).
- **`TestProfileDetail.fields`** — `dob`/`accountMasked`/
  `verificationPhraseMasked`/etc. (`src/lib/test-profiles.ts`'s
  `getTestProfileFields`, same defensive-read discipline).
- **`Metric.spec`** — reserved for `promptTemplate`/`pythonCode`/
  `threshold` per `#27`, but nothing writes to the real `spec` bag yet;
  those three fields are still plain hand-authored `Metric` properties,
  not yet read through `spec` at all.

**Not built, deliberately** (down from the original list — New suite,
Edit suite, Rubric, Generate scenarios, Add assertion, and Save scenario
are all wired now):
- **Add scenario** (the scenario table's toolbar button) — no manual
  "new scenario from scratch" UI has been scoped; `upsertScenario`
  itself is real and already used by the generate-accept flow. See
  `scenario-filter-bar.tsx`'s own comment.
- **Attach metric** — blocked by two independent things, not one: (1)
  `ScenarioUpdate.metrics: ScenarioMetricAttachment[]`
  (`{metricId, gating}`, a gating *config*) is not the same shape as
  this app's own `scenario.scenarioMetrics` (`{metricId, result,
  valueLabel}`, a per-run *result*) despite sharing `metricId`; (2) the
  Backend gap above means there's nowhere to read an attachment back
  from even if the write shape were solved. See
  `scenario-drawer-tabs.tsx`'s `MetricsTab` comment.
- The scenario editor's Persona/Test profile tab selections *do* persist
  now (via the same `upsertScenario` "Save scenario" call) — the closing
  dirty-guard (`window.confirm`, no `AlertDialog` primitive in this repo
  yet) still applies, now meaningfully guarding a real write instead of
  a discarded local selection.

**Fix (residual):** the `Scenario` fields still genuinely hand-authored
with no wire counterpart on `contract-v1.3.0` —
`callerInstructions`/`turnBudget`/`source`/`goalMetLastRun`/
`goalCitedTurns`/`scenarioMetrics`/`conditionsSummary`/
`conditionProfile` (see `types/index.ts`'s own comment on `Scenario`).
Once the Backend gap above closes (a real scenario read shape), revisit
whether any of these have real counterparts to fold in, and unblock
"Attach metric" if `ScenarioMetricAttachment` gets a read path.
**Repo:** frontend (this doc) now; brain for the schema/endpoints.
**Status:** partially reconciled.

### 26. `W2.7-03` (Personas library screen) built ahead of `contract-v1.3.0`/`B2.7-02`
**Deviates:** implements backlog ticket `W2.7-03` against
`Cadence.redesign.html`'s `#v-personas` section — same "build ahead,
reconcile later" precedent as `#14`/`#18`/`#25`. Confirmed directly
against the schema first: the real `Persona` schema
(`GET /v1/personas`, the only real persona endpoint at any tag — no
write verb exists) already matched this repo's hand-authored `Persona`
field-for-field, so `Persona` is now a real type alias
(`components["schemas"]["Persona"]`), not a hand-authored guess — the
one correction being `accent`, which the real schema marks optional
(may be omitted), not just nullable; no read site needed a change since
every one already treated a falsy `accent` as "no accent."
What's genuinely new, ahead of the contract:
- `PersonaTraits`/`getPersonaTraits()` (`src/lib/personas.ts`) — typed,
  defensive accessors over the real `Persona.traits` bag
  (`{[key: string]: unknown}` on the wire) for the specific keys this
  ticket's cards/editor display: `languageNote`, `emotion`,
  `speakingRate`, `interruptions`, `environment`, `conditionChips`. A
  real API persona's `traits` can hold anything or nothing; every read
  is type-checked and falls back to "absent," never a cast.
- `PersonaWithUsage` (`.scenarioCount`) — computed live in
  `getPersonasWithUsage()` off `Scenario.personaId` (itself a `#25`
  addition real scenarios don't carry yet), never cached, so a real-mode
  persona's count is honestly 0 rather than fabricated.
- `db.personas` — the first mutable persona store (`repository.ts`);
  `getPersonas()` now reads it instead of returning the seed array
  directly, so duplicate/delete/upsert are visible on the next read
  within the same mock session.
- `duplicatePersona`/`deletePersona`/`upsertPersona` + their Server
  Action wrappers (`duplicatePersonaAction`/`deletePersonaAction`/
  `upsertPersonaAction`, `lib/actions.ts`) — real, session-durable mock-
  mode writes, following `startSuiteRun`'s precedent (`#22`), **not**
  `upsertAgent`'s (`#23`): real mode throws an honest "no write endpoint
  exists yet" `Error` the UI surfaces inline, rather than attempting a
  call against a verb/shape that was never going to work.
  `deletePersona` blocks (with the in-use count in the message) when
  `scenarioCount > 0` — the ticket's own done-when. `upsertPersona`
  refuses to overwrite a builtin (defense-in-depth; the UI never opens
  the editor on one — builtins only ever offer "Duplicate to project").
- Mock data: `personasSeed` grown from 4 to 14 (the ticket's own "6
  built-in, 8 custom," matching the redesign's literal subtitle) by
  **appending** p5–p14 after the untouched p1–p4 — several existing unit
  fixtures across the codebase hardcode those four names/ids.
  `buildS3Scenarios`'s persona cycle was re-pinned to a local
  `s3PersonaCycle = personasSeed.slice(0, 4)` rather than `personasSeed`
  itself, so growing the library doesn't silently reassign which persona
  lands on which `s3` scenario. None of p5–p14 are referenced by any
  seeded scenario — deliberate, so the "0 scenarios → delete allowed"
  and "N scenarios → delete blocked" cases both have real examples to
  demo against without new scenario data.

**Not built, deliberately:** a controlled vocabulary/select for
Voice/Language (free-text inputs, matching the seed data's own
free-text shape — nothing in the redesign or ticket specifies an
enum). Condition-profile fields in the editor render read-only with a
permanent "Roadmap" chip, per the ticket's own "disabled ... rather
than hidden, until the engine wires them" — not editable in any mode,
including mock.

**Fix:** once `B2.7-02` tags `contract-v1.3.0` and a persona write
endpoint exists: reconcile `duplicatePersona`/`deletePersona`/
`upsertPersona`'s real branches from "honest throw" to real
`pythonJson` calls, same mechanical pass `#25`'s fix section describes.
**Repo:** frontend (this doc) now; brain for the schema/write endpoint.
**Status:** open, by design.

### 27. `W2.7-04` (Metrics library screen) built ahead of `contract-v1.3.0`/`B2.7-04`/`B2.7-05`
**Deviates:** originally implemented backlog ticket `W2.7-04` against
`Cadence.redesign.html`'s `#v-metrics` section, ahead of
`contract-v1.3.0` tagging (`schema.d.ts` had zero metrics-library
surface at `contract-v1.2.0`) — same "build ahead, reconcile later"
precedent as `#14`/`#18`/`#25`/`#26`. `contract-v1.3.0` has since tagged
and been synced; real `/v1/metrics` (list/create/get/update/delete),
`/v1/metrics/resolved?scenarioId=`, and `/v1/metrics/{id}/backtest` all
exist now. This entry tracks reconciliation against that, done across
`W2.7-04` (mechanical alignment + CRUD), `W2.7-05` (resolved-view
redesign), and `W2.7-06` (backtest).

**Reconciled this pass:**
- **Enum/field renames, mechanical:** `Metric.kind`:
  `"built-in"|"llm-judge"|"python"` → real `"builtin"|"llm_judge"|"python"`
  across every seed/component/test. `Metric.detail` → `description`
  (matches the real field name). `Metric.outputType` → real closed enum
  `"boolean"|"numeric"|"enum"|"tri_state"`. `Metric.status` gained
  `"archived"`. **Caught while renaming:** `deleteMetric`'s built-in
  guard (`target.kind === "built-in"`) would have silently gone
  always-false after the rename if missed — fixed to `"builtin"` in the
  same commit, the exact class of bug `#23` warns about (a stale string
  literal surviving a schema rename).
- **`scope`/`scopeAgentName` → derived, not stored.** The real field is
  `agentId?: string | null`; `metricScope()`/`metricAgentName()`
  (`src/lib/metrics.ts`) derive the old display concepts from it at the
  **component** boundary (`metrics-screen.tsx`/`metric-editor-modal.tsx`,
  which already hold a `ConnectedAgent[]` prop) — not inside
  `repository.ts`, to avoid an extra fetch per `getMetrics()` call and an
  id→name→id round trip through the editor. Same pattern as
  `Suite.agent`'s `resolveAgentName()` in `repository.ts` (`#25`).
- **Real CRUD.** `getMetrics()` upgraded from an unconditional `[]`
  short-circuit to a genuine `pythonJson` call (standard
  try/catch-degrade-to-`[]`, no longer "nothing to attempt"). `upsertMetric`/
  `deleteMetric` do the real POST-create/PATCH-update/DELETE split (the
  `#23` lesson, applied again). `deleteMetric` still refuses any
  `kind: "builtin"` unconditionally, checked before the in-use check, so
  a used built-in reports the built-in reason not the usage one.
  `upsertMetric` still does **not** hard-reject built-in edits — the UI
  restricts a built-in's form to Threshold only, not the repository.
- **Backtest flow is real** (`W2.7-06`): `backtestMetric(id, sampleSize)`
  calls the real `POST /v1/metrics/{id}/backtest` in real mode; mock mode
  fabricates a plausible `BacktestResult` from `db.runs`-derived data,
  same "session-durable, not persisted forever" spirit as other mock
  writes. `backtest-flow-modal.tsx`: sample-size → progress → per-call
  verdict table → agreement/cost → Activate (a plain `upsertMetric`
  `{status: "active"}` PATCH — `MetricUpdate` confirms `status` is a
  normal field, no separate activate endpoint) or Keep editing.
- **`getResolvedMetrics(agentName)` redesigned, `W2.7-05`.** Mock mode's
  agent-scoped precedence engine is unchanged (still the one place that
  computes precedence — component layer still never recomputes it,
  matching this ticket's own "never recompute it client-side"), but now
  resolves via the real `agentId` instead of the old hand-authored
  `scope`/`scopeAgentName`. **New `getResolvedMetricsForScenario(scenarioId)`**
  is the real-mode counterpart — real branch calls
  `GET /v1/metrics/resolved?scenarioId=` directly (`sourceLevel:
  "builtin"|"project"|"agent"`, no `overrides` id); mock branch looks the
  scenario up for its `agentName` and **delegates to `getResolvedMetrics`,
  reshaped** — explicitly not a second precedence implementation, per
  this entry's own earlier warning. `metrics/page.tsx` now branches on
  `API_MODE`: mock precomputes `resolvedByAgent` server-side as before;
  real mode passes `suites` instead, and `ScenarioResolvedMetricsView`
  (new component) lets the viewer pick a suite then a scenario and fetches
  that scenario's real resolved set on demand — a genuinely different UX
  for a genuinely scenario-scoped endpoint, not one component forcing two
  data shapes into the same view.

**Plan-vs-implementation divergence, recorded on purpose:** the approved
plan's own text for this ticket said `backtestAgreement` "**is removed as
a stored field**... replaced by a real `BacktestResult` fetched live."
What actually shipped keeps `backtestAgreement` on `Metric`, hand-authored,
as a **mock-only, session-durable convenience** —
`backtestMetric`'s mock branch writes it onto the metric after a run,
purely so a re-opened editor shows "last backtest: N%" without a second
fetch; `repository.metrics.test.ts` pins this behavior explicitly. Real
mode never populates it (a real `BacktestResult` is always fetched live,
matching the original plan). Flagging this here specifically because an
unrecorded plan/implementation gap is the kind of thing that reads as an
oversight later, not a decision.

- `Metric.mustPass` stays hand-authored — real gating lives on
  `ScenarioMetricAttachment.gating`, a per-scenario-attachment property,
  not a metric-level one; reshaping this needs the `#25` Backend-gap/
  Attach-metric work, not this entry. `promptTemplate`/`pythonCode`/
  `threshold` also stay hand-authored — nothing writes to the real
  `spec: {[key:string]:unknown}` bag they're meant to eventually live in
  yet (see `#25`'s "App-invented conventions" note on `Metric.spec`).

**Not built, deliberately:** the Radix-based shared `ui/select.tsx` was
tried first for the editor's Kind/Scope/Output-type/Agent pickers and
the Resolved view's agent selector, then reverted to plain native
`<select>` elements (matching `scenario-drawer-tabs.tsx`'s own Persona/
Test profile tab convention) — this repo has never exercised
`ui/select.tsx` in a test, and Radix's `Select` needs jsdom pointer-
capture/`scrollIntoView` polyfills that don't exist here yet. Revisit
if a future ticket needs `ui/select.tsx` interactively tested; add the
polyfills then rather than working around them per-component again.

**Fix (residual):** `mustPass` and the `spec`-bag fields above, both
blocked on the same `#25` Backend-gap resolution (a real scenario read
shape / `ScenarioMetricAttachment` read path) rather than anything
metrics-specific.
**Repo:** frontend (this doc) now; brain for the schema/endpoints.
**Status:** partially reconciled.

### 28. Phase W2.8 (Dashboard v2) — RECONCILED against `contract-v1.4.0`
**Status:** reconciled 2026-10-08; see the matching entry under
`## Resolved` below for the full writeup of what the hand-authored guess
got right/wrong against the real contract. Kept here only as the pointer
`#9`/`#20` already reference by number — those amendments stay accurate.

### 29. `/runs` Live tab + `/runs/[testRunId]` (W2.6-05/W2.6-02) finished — both were built incomplete against the redesign and their own ticket text
**Deviates:** `W2.6-02` built `RunHero`/`CallTable`/`LiveBatchView` (the
redesign's `runhero` + table, the closest analog this page has — the
redesign's own markup has no dedicated standalone Test Run detail page,
only `#v-runs`'s Live tab and the `#dr-call` drawer) with real polling
(`useRunQueuePoll`) already wired and unit-tested, but this page never
mounted any of it — it rendered a one-time static `BatchCallTableSection`
snapshot regardless of whether the batch was still in progress.
Concretely, the one real place a user watches a batch they just started
(`run-config-modal.tsx` pushes straight to `/runs/${parentRunId}` on
submit) showed every child frozen `idle` forever, since
`startSuiteRun`'s mock branch creates them that way and nothing else in
mock mode ever resolves them. Also missing, against the ticket's own
done-when: a per-scenario rollup, and `?call=` deep-linking to a drawer.
**Fix:** `page.tsx` now branches on whether the batch is live — mode-
dependent (mock: the parent's own `status === "idle"`; dev/demo: any
child non-terminal — see the page's own `isLive` comment for why these
have to differ, caught by an advisor review before this was called done:
mock's fake drain is destructive to an already-resolved batch, dev/demo's
real poll isn't). Live mounts `LiveBatchView`; otherwise the existing
static view, now paired with a new `ScenarioRollup`
(`scenario-rollup.tsx`, grouping the already-fetched rows — no new
endpoint) and a new `OpenDrawerFromQuery` component for `?call=`
(validated against the batch's real rows before opening anything).
Also fixed in the same pass: `isBatch` was `aggregate.callCount > 1`,
which silently excluded a *one-scenario* "Run test suite" submission
(`callCount === 1`) from ever reaching the batch branch at all — it fell
into the childless single-call cards, rendering a permanently-idle,
empty-transcript run forever, same root bug via a different path. Now
`isBatch = callRows.length > 0` (a genuinely childless run like
`run-4821` has none), with `callRows` always fetched rather than gated
behind the old check.
**Known limitation, not fixed — mock mode's live drain is display-only:**
`useQueueDrain` deliberately never writes back to `db.runs` (`queue-
drain.ts`'s own docstring) — a freshly-submitted batch's fabricated
pass/fail outcomes exist only in that one component's local state.
Concretely: reloading the page replays a *different* random outcome;
clicking a row the drain shows as "Passed · 92" still opens the drawer on
the *persisted* child, which is still `idle` with an empty transcript;
and the batch shows as `idle` forever in History and the dashboard's
recent-runs widget. Genuinely better than the old permanently-frozen
table (you now see realistic queued → running → completed progress,
which is W2.6-02's whole point), but it doesn't make the mock submission
actually *complete* anywhere else in the app. Giving mock mode a real,
persisted resolution path is a separate, bigger piece of work (the mock
layer would need its own async resolution timer against `db.runs`
itself, not a display-only drain) — not attempted here. Same root cause
shows up on the Live tab too: `liveCount`'s badge only ever grows (every
mock submission stays `idle` in `db.runs` forever), while that same run's
own `LiveBatchView` hero can say "COMPLETE" within seconds once its local
drain finishes — the badge and the hero are reading two different sources
of truth for the same run, by construction, not a bug introduced here.
**Amendment — `/runs`'s Live tab, originally flagged out of scope, fixed
in a follow-up pass the same day:** the first version of this entry left
`runs-screen.tsx`'s Live tab rendering the pre-W2.6-02 `SimulationsScreen`
(a different screen's component — the old `/simulations` single-scenario
player, carried over unchanged), on the reasoning that replacing it was a
materially bigger change than finishing the one permalink page. The user
caught this directly against the redesign (`Cadence.redesign.html`'s own
`#v-runs` note: *"Simulations stops being a nav item and becomes the 'Run
test suite' action"* — i.e., it's supposed to be gone, not reachable as a
tab). Fixed: `/runs/page.tsx` resolves whichever Test Run is currently
live (`status === "idle"` among `getTestRunSummaries()`) and passes it to
`runs-screen.tsx`, which mounts the same `LiveBatchView` the permalink
page uses — never a second implementation. `status === "idle"` is
confirmed correct in *every* mode, not an assumption carried over from
mock-only reasoning: the real backend's `app/verdict.py` (`verdict_for_run`)
folds every in-progress raw status (`queued`/`claimed`/`running`) to
`"idle"` on the wire unconditionally — there's no other value an in-flight
run could report. Which end of the list to pick *is* mode-dependent
(mock's `db.runs` is insertion-ordered oldest-first; the real backend's
`list_runs` is `ORDER BY created_at DESC`, confirmed against
`app/api/runs.py` — newest first), and a childless idle candidate is
guarded out the same way `/runs/[testRunId]`'s own `isBatch` is (not
possible in today's seed data, but not guaranteed by any type). A
tab-trigger count badge (the redesign's own "3") reuses the same filtered
count. `SimulationsScreen`/`CallPanel`/`LiveAssertions`/
`useSimulationStore` are deleted outright (confirmed via grep: zero other
consumers) rather than left orphaned. `startRunAction`/`createRunAction`
(`actions.ts`) and `startRun`/`createRun`/the `"simulations"` `RunKind`
branch (`repository.ts`) are **not** deleted — they're a different layer
(real/mock data writes, not UI), lower-risk to leave dead than to remove
speculatively; a genuine candidate for `W5-02`'s "dead mock-only branches
removed where safe" sweep.
**Capability removed, not just UI:** `SimulationsScreen` was the only
place in the app that ran a *single* scenario against a *chosen persona*
— "Run test suite" (the replacement, and the only way to start a run now)
lets you pick scenarios but not a persona; that field is a disabled
placeholder (`#21` — the real contract 422s on `personaIds`). The redesign
intends this (persona selection isn't in its run-config modal either),
but it's a real, user-facing capability gap versus what existed before,
not a pure refactor.
**Known simplification, not fixed:** picking "the" live run when more
than one exists — first/last match by insertion order, no picker UI; the
redesign doesn't model watching several at once either.
**Repo:** frontend (this doc) now.
**Status:** resolved — both halves of this entry (the permalink page and
the Live tab) are now fixed. The mock-drain-is-display-only limitation
above remains open, by design.

### 30. Switcher's "+ New project" wired for real — `POST /v1/projects` was already live, never called
**Deviates:** `DECISIONS.md #21` (now amended) recorded both switcher
footer buttons as inert for the same reason — "no backend exists yet."
True for org-management (`/v1/organizations` is still `GET`-only,
confirmed against the real contract — no change there). False for project
creation: `POST /v1/projects` (`create_project`, `B2.5-01`) was already
part of `contract-v1.4.0`'s generated schema — the frontend simply never
called it, same "backend shipped it, nobody came back to wire it" pattern
as `#29`'s `LiveBatchView`.
**The real shape forces a UI decision, not a guess:** `ProjectCreate` is
`{name: string}` — no `orgId` field at all, and the real `Project`
response (`{id, name, createdAt}`) doesn't carry one either. Cross-checked
against `B2.5-02`'s migration note (`projects.org_id FK NOT NULL`,
`org_memberships(org_id, user_id, role)`): the backend must infer which
org a new project belongs to from the caller's own membership, not from
anything the client sends. Consequences, not assumptions:
- The "+ New project" form is **not** nested inside the popover's
  "Projects in {viewedOrg}" column the way picking an *existing* project
  is — anchoring it there would imply control over placement the real
  endpoint doesn't take as input.
- `createProjectAction` (`actions.ts`) re-lists organizations after
  creating and searches for which org now contains the new project's id,
  rather than assuming — correct in both modes without the real endpoint
  needing to echo back something it was never designed to return.
- **Mock-only simplification, not a guess at the real inference rule:**
  mock mode has no `org_memberships` table to infer from, so `createProject`
  (`repository.ts`) attaches the new project to whichever org the scope
  cookie currently selects. This is only unambiguous because this
  session's mock user happens to belong to more than one org
  (`organizationsSeed`) — a real single-membership user wouldn't have this
  ambiguity at all. Flagged as a mock/real UX gap, not papered over.
**Fix:** none needed on the frontend — already matches the real contract.
If the real backend ever supports true multi-org membership with a way to
pick which org to create into, this mock simplification (and the "can't
anchor the form to a column" UI consequence) should be revisited together.
**Repo:** frontend (this doc) now.
**Status:** resolved.

### 31. `W3-01` — "Connect a voice agent" wired for real, resolving `#23`

**Deviates:** `#23` flagged `upsertAgent`'s real branch as wrong on every
axis (verb, endpoint, body shape) but left it unfixed. Re-checked the
currently-synced `contract-v1.4.0` schema directly (not the backlog's own
phase ordering, which this session had — wrongly — told the user meant
W3-01 was still blocked on an untagged `B3-01`; it isn't: `create_agent`,
`update_agent`, `test_connection_pre_save` and `test_connection_post_save`
are all already live) and confirmed `app/api/agents.py`'s own source for
the test-connection split.
- `upsertAgent` deleted outright. Replaced with `createAgent(input:
  AgentCreate): Promise<AgentDetail>` → real `POST /v1/agents`. No
  `updateAgent`/`AgentUpdate` built at the time this entry was written —
  closed by `#33` (`W3-01c`).
- **Two test-connection endpoints exist, not one** — easy to miss by only
  grepping for `test_connection` and finding the `_post_save` (existing
  agent, `POST /v1/agents/{id}/test-connection`) variant first. A
  `_pre_save` sibling (`POST /v1/agents/test-connection`, body is a bare
  `WebConfig|SipConfig|PhoneConfig`, no agent id) also exists — confirmed
  via the generated schema, not assumed from naming alone. This is the one
  `connect-screen.tsx` actually needs: B3-01's own ticket says "pre-save
  handshake" literally, and this form tests a connection *before* an agent
  row exists to post-save test against. The original test-then-save UX in
  `connect-screen.tsx` was therefore already correct — no flow redesign
  needed, just wiring `testConnectionPreSave` to the real endpoint.
- **`web` gets a canned success, `sip`/`phone` always 501.** Read directly
  from `app/api/agents.py`: `web`'s handshake is itself a stub
  (`{success:true, rttMs:42, detail:"stub result -- real in B3-01"}` —
  the real live handshake is still unbuilt), and `sip`/`phone` return
  `501 not_supported` unconditionally (matching this ticket's own
  done-when: "SIP/Phone tabs show 'coming soon' on 501"). Mock mode
  reproduces this exact split, including throwing the same
  `PythonApiError` shape for sip/phone, so `connect-screen.tsx` needs no
  `API_MODE` branch of its own to show the right state.
- **A new agent reads "Offline," not "Healthy."** `ConnectedAgent.status`
  is derived from call history (`derive_agent_liveness`), never written by
  `create_agent` itself — a brand-new agent has zero calls, which the real
  derivation reads as offline. `upsertAgent` used to hardcode `"Healthy"`;
  `createAgent`'s mock branch now matches the real derivation instead of
  inventing a status that a real save could never actually produce.
- **`connect-screen.tsx` rewritten**: every field (name, WebRTC
  URL/token, SIP URI/username/password, phone number/provider, language,
  concurrency limit) converted from an uncontrolled ref/`defaultValue` to
  controlled state. This wasn't cosmetic — `TabsContent` unmounts inactive
  tabs (Radix), so every ref except the one on the always-mounted "Agent
  name" field pointed at a detached node the moment a user switched tabs,
  silently discarding anything typed into WebRTC/SIP/Phone fields before
  save. The old `defaultValue`s were also fake production-looking strings
  (`"Clinic Concierge"`, `"wss://agents.acme.health/rtc"`,
  `"sk_live_••••••••••••4f2a"`) that would have been submitted verbatim as
  real connection config for anyone who didn't overtype every field — moved
  to `placeholder`s, empty initial state. Auth token field is now
  `type="password"`. SIP's Port/Transport select and Phone's Region select
  were removed (no field on the real `SipConfig`/`PhoneConfig`); SIP
  gained `username`/`password`, Phone's Region became a free-text
  `Provider` field — matching the real schema exactly, not the redesign's
  mockup fields. Concurrency limit is now read and sent as
  `AgentCreate.maxConcurrency` (previously silently dropped, deliberately,
  per `#23`'s own writeup, because nothing read the field at all).
- `testConnectionPreSaveAction` (`actions.ts`) returns `{ok, data} |
  {ok:false, status, message}`, same shape as `startSuiteRun`'s own
  `StartSuiteRunResult` — a thrown `PythonApiError` loses its `.status`/
  `.code` crossing the Server Action boundary (only `.message` survives),
  so the real 501 needs to come back as data for the UI to render a
  "coming soon" state instead of a generic error string.
**Fix:** done — `types/index.ts` (`AgentCreate`/`AgentDetail`/`AgentConfig`/
`WebConfig`/`SipConfig`/`PhoneConfig`/`TestConnectionResult` aliases),
`endpoints.ts` (`agentTestConnectionPreSave`), `repository.ts`
(`createAgent`/`testConnectionPreSave`, mock parity with the real
derivation/501 logic), `actions.ts` (`createAgentAction`/
`testConnectionPreSaveAction`), `connect-screen.tsx` (full controlled
rewrite), plus unit tests (`repository.mock-mode.test.ts`,
`connect-screen.test.tsx`) and a `repository.test.ts` dev-mode write-failure
regression updated off the old `upsertAgent` call shape.
**Repo:** frontend (this doc) now.
**Status:** resolved — `pnpm typecheck`/`lint`/`test`/`build` all clean,
plus a real mock-mode click-through in-browser (already-signed-in dev
session, no credentials entered): typed every Web field, switched to SIP
and back and confirmed nothing was lost, clicked Test connection and saw
the real canned `web` success banner, saved and confirmed the new agent
appeared in "Connected agents" reading **Offline** (not "Healthy") with
the form reset, then switched to SIP and confirmed Test connection
renders "Testing SIP connections is coming soon." — the real 501, not a
generic error. Not live-tested against the real production backend (no
live write made without the user's explicit go-ahead, same standing rule
`#23` itself stated).

### 32. `W3-01b` — bring-your-own LiveKit agent form — RESOLVED, `contract-v1.5.0` tagged

**Deviates (original, now resolved):** backend `B3-01b` replaces the
pasted `token` with LiveKit credentials: `WebConfig` is now `{roomUrl,
apiKey, apiSecret?, agentName?}` and `TestConnectionResult` gains
`errorCode`. At the time this was built the contract change wasn't tagged
yet, so `src/lib/api/generated/schema.d.ts` was regenerated from the
backend repo's local `contract/openapi.json` instead of via `pnpm
sync:contract`.
**Resolved:** `QA_test_backend` tagged and pushed `contract-v1.5.0`
(`B3-01`/`B3-01b`, PR #1). `contract.lock` bumped from `contract-v1.4.0` to
`contract-v1.5.0`, `pnpm sync:contract` re-run against the real tag. The
regenerated `schema.d.ts` is **byte-identical** to the hand-regenerated
one this entry originally described, save for the generator's own header
comment (which tag it was pulled from) — confirmed by diffing the two
files directly, not assumed. No further reconciliation needed: the
contract the frontend was built against and the contract that actually
shipped are the same commit.
**Fix:** `connect-screen.tsx`'s Web tab now collects LiveKit URL / API key /
API secret / optional agent name instead of a single token field. The API
secret is write-only end to end: it's sent once through the server action,
the real API never returns it on reads (`hasSecret` instead), and
`createAgent`'s mock branch mirrors that by stripping `apiSecret` and
substituting `hasSecret`. A failed test (`success:false`) now renders a
distinct state per `errorCode` (`bad_config`/`auth_failed`/
`agent_unreachable`) instead of being indistinguishable from a successful
one. Mock mode keeps web's canned success for anything it can't fake
(`testConnectionPreSave`/`testConnectionPostSave` can't actually join
LiveKit) but mirrors every check that's pure business logic, not network:
no `apiSecret` -> `bad_config` (matches `test_connection_pre_save`'s own
check verbatim), plus two deliberately-documented mock-only triggers
(`apiKey: "bad"` -> `auth_failed`, `roomUrl` containing "unreachable" ->
`agent_unreachable`) so every state in `TestResultBanner` stays reachable
without a real backend.
**Repo:** both. **Status:** resolved — `pnpm typecheck`/`lint`/`test`/
`build` all clean against the real tagged schema, and live-verified for
real with the user's go-ahead: on the deployed `w3-01-agent-connect`
preview (`https://cadence-web-git-w3-01-agen-785225-ashishdalmia34-4516s-projects.vercel.app`,
real `API_MODE`, real backend, "Default Organization / Test Project"),
created a web agent with intentionally-fake LiveKit credentials. "Test
connection" hit the real `POST /v1/agents/test-connection` and got a
genuine LiveKit rejection back — `auth_failed`, detail `"engine: signal
failure: client error: 401 Unauthorized - invalid API key"` — not a mock
or a guess, the real probe actually ran. Saved the agent (`POST
/v1/agents`, real write, confirmed via the sidebar's agent count and the
list reading "Offline" — the real `derive_agent_liveness`, not a fake
"Healthy"), then deleted it afterward so no test data was left behind.

### 33. `W3-01c` — manage saved agents (edit, re-test, delete), built against the then-untagged `contract-v1.5.0`

**Deviates:** net-new — nothing on the Agents screen could touch a saved
agent before this; `ConnectedAgentsList` was read-only. Built against the
real `app/api/agents.py` (read directly, not guessed): `GET/PATCH/DELETE
/v1/agents/{id}` and `POST /v1/agents/{id}/test-connection` (post-save,
distinct from W3-01's pre-save handshake — no body, the backend reads the
agent's own stored config) are all already live.
- **Edit** fetches the full `AgentDetail` on click (the list's own `GET
  /v1/agents` only returns the slim `ConnectedAgent` row — no config,
  language or concurrency), then `PATCH`es it. Transport is locked to
  whatever the agent was created with — the real endpoint would honor a
  transport change (`_agent_updates` just reassigns the `transport` column
  whenever `config` is sent), but no ticket or create-flow UX asks for
  transport migration, so this app doesn't offer it.
- **Secret handling matches `_stored_config` exactly**: leaving the API
  secret field blank on an agent that already has one (`hasSecret: true`)
  keeps the stored secret (the field is simply omitted from the PATCH
  body); a legacy agent (`hasSecret: false` — every a1-a4 seed, all
  predating `B3-01b`) must supply a key and secret to save at all, same
  422 message the real backend throws (`"apiSecret is required for a web
  agent"`), reproduced verbatim in mock mode's `updateAgent`.
- **Delete 409s** when the agent is referenced by any suite or run,
  matching `delete_agent`'s own check (no `ON DELETE` policy on
  `agents.id`, deliberately — cascading through an agent's suites would be
  more destructive than the deletor likely intends). Mock has no
  `agent_id` foreign keys to check against, so it matches by *name* — the
  same association `agentIdForName`/`RunDetail.agentName` already use
  everywhere else in this mock. Confirmed via the fixture data: "Clinic
  Concierge"/"Support Line" (a1/a2) are referenced by seeded suites and
  runs and 409 on delete; "After-hours IVR"/"Billing Bot (staging)" (a3/
  a4) aren't, and delete cleanly — a real in-use/not-in-use contrast for
  manual verification, not a coincidence.
- **Re-test's "no stored credentials" prompt is the real backend's own
  detail string** (`"this agent has no stored LiveKit credentials -- add
  an API key and secret"`), not a client-invented one — mock mode returns
  it verbatim for a1/a4, the two legacy web seeds. The row renders an "Add
  credentials" action on exactly that `bad_config` state, opening Edit.
- Delete confirmation uses an in-app `Modal`, not `window.confirm` — a
  native confirm dialog blocks further automation mid-click-through
  (irrelevant to a real user, but it's the same reason this app already
  avoids triggering browser dialogs during its own manual verification
  passes).
- **A thrown Server Action error's `.message` is redacted in a production
  build, not just its `.status`/`.code`** — confirmed with `pnpm build &&
  pnpm start`, not assumed: deleting an in-use agent rendered the generic
  "An error occurred in the Server Components render..." instead of the
  real 409 text. `updateAgentAction`/`deleteAgentAction` now return
  `{ok,status,message}` the same way `testConnectionPreSaveAction` always
  has (for a different original reason), catching server-side so nothing
  throws across the boundary at all; re-verified against a production
  build afterward and the real message renders correctly.
  `createAgentAction`'s `saveError`, and the persona/suite delete actions
  elsewhere in this app, have this exact same latent bug and have not
  been touched here — worth its own pass, not folded into this ticket.
- **Renaming an agent now rewrites `db.suites[].agent` and every
  `RunDetail.agentName`** that pointed at the old name — the mock's 409
  check matches by name (no `agent_id` foreign key in this fixture data),
  so without this a rename would silently break the in-use check and let
  a still-referenced agent delete cleanly. Pinned with a regression test
  (rename a seed agent, then delete it, still expect 409).
- **Known gap, not fixed here:** `_public_config` only redacts the web
  transport's secret — `GET/PATCH /v1/agents/{id}` returns a SIP agent's
  `password` in plaintext (`_public_config` passes `sip`/`phone` config
  through unchanged). The done-when's "the secret never appears in any
  response" holds for the LiveKit secret this ticket actually built
  around; the SIP password was already like this before `W3-01c` and is a
  backend gap, not something this edit form introduced.
**Fix:** done — `endpoints.ts` (`agentTestConnectionPostSave`),
`repository.ts` (`getAgent`/`updateAgent`/`deleteAgent`/
`testConnectionPostSave`, a new `db.agentDetails` mock store seeded via
`agentDetailsSeed`, rename propagation), `actions.ts` (`getAgentAction`
throw-based like every other read; `updateAgentAction`/`deleteAgentAction`/
`testConnectionPostSaveAction` share one `toAgentActionResult` helper,
`{ok,status,message}` on failure), `agent-edit-modal.tsx` (new),
`connected-agents-list.tsx` (rewritten from a dumb list into its own
CRUD-owning component, same shift `PersonasScreen` made over
`PersonaCard`; clears a row's stale re-test banner on a successful edit or
delete), `test-result.tsx` (new — `TestState`/`TestResultBanner` extracted
out of `connect-screen.tsx` so pre-save and post-save don't render two
copies of the same banner), plus unit tests (`repository.mock-mode.test.ts`,
dev-mode method/URL/body pins in `repository.test.ts`,
`connected-agents-list.test.tsx`, including the apiSecret-omitted-vs-typed
PATCH body and the rename-then-409 regression).
**Repo:** frontend (this doc) now.
**Status:** resolved on the frontend/contract side — `contract-v1.5.0` is
now tagged and `contract.lock`/`schema.d.ts` are synced against it for
real (`pnpm sync:contract`, confirmed byte-identical to what this ticket
was already built against, see `#32`). `pnpm typecheck`/`lint`/`test`/
`build` all clean (463 tests), plus two real click-throughs in-browser
(already-signed-in dev session, no credentials entered): one in `pnpm dev`
covering the full flow — re-tested "After-hours IVR" (phone) and got the
real "coming soon"; re-tested "Clinic Concierge" (legacy, no stored
credentials) and got the real `bad_config` prompt verbatim, clicked "Add
credentials", filled a key and secret, saved, re-tested again and got the
real canned success; attempted to delete "Support Line" (referenced by
seeded suites/runs) and got the real 409 message inline, not a crash;
deleted "Billing Bot (staging)" (unreferenced) and it was removed cleanly
— and a second, narrower one against `pnpm build && pnpm start`
specifically to confirm the 409 fix above survives a production build,
which it does. Live-verified for real afterward, same pass as `#32`'s own
writeup (same deployed preview, same test agent): Edit opened prefilled
from a real `GET /v1/agents/{id}` (name/LiveKit URL/API key correct, the
secret field reading "SAVED, LEAVE BLANK TO KEEP" — real `hasSecret`, not
mock); renamed and saved via a real `PATCH`, confirmed in the list; "Re-
test" hit the real post-save endpoint and got the same genuine LiveKit
401 rejection as the pre-save test, proving the encrypted secret actually
round-trips through real storage and decryption, not just the pre-save
path; Delete removed it via a real `DELETE`, confirmed by the list
reading "No agents connected yet." again and the sidebar count dropping
back to 0. No gaps left open from this ticket's own done-when.

### 34. Sidebar "Test Suites"/"Agents" badge counts were hardcoded literals, not real reads — RESOLVED

**Deviates:** `sidebar.tsx`'s `navGroups` had `count: 6` on Test Suites and
`count: 4` on Agents as plain literals, present since the sidebar was
first built — not derived from any data source, not a regression from any
later ticket, just never wired up. Visible the moment the real count
diverges: creating or deleting a suite/agent left the badge showing the
original seed count forever, same number whether 0 or 50 existed.
**Fix:** `(shell)/layout.tsx` now reads `getSuites()`/`getConnectedAgents()`
alongside `auth()`/`resolveScope()` (one extra read per navigation under
the shell, same tradeoff `resolveScope()` already makes for the project
switcher) and threads `suiteCount`/`agentCount` through `AppShell` ->
`Sidebar`. `sidebar.tsx`'s `navGroups` moved from a module-level literal
array to `buildNavGroups(suiteCount, agentCount)`, called with the real
props instead of a hardcoded number. Both reads already degrade to `[]` on
a real-mode failure (pre-existing `try/catch` in each), so this can't
newly crash the shell.
**Repo:** frontend. **Status:** resolved — `pnpm typecheck`/`lint`/`test`/
`build` all clean; verified live in `pnpm dev` mock mode: sidebar read "7"
after creating a suite (6 seeded + 1 new) and "4" -> "3" immediately after
deleting an agent, both via `router.refresh()`, no manual reload needed.

### 35. `/suites` 404'd on a genuinely empty real project — RESOLVED, root cause confirmed live

**Deviates:** two user reports turned out to be the same bug: "creating a
test suite shows a 404" and, later, "Run test suite doesn't show a suite
option, Generate scenarios is failing." The original hypothesis here
(serverless cold-start isolation of mock mode's `db`, one level up from
`#22`) was never confirmed and turned out to be the wrong theory —
reproduced live instead, on the deployed `w3-01-agent-connect` preview's
own empty "Default Organization / Test Project" (0 suites, by then 1 real
agent): navigating to `/suites` redirected to `/suites/s1` and 404'd.
**Root cause**, in `(shell)/suites/page.tsx`:
```ts
const landing = suites.find((s) => s.id === "s3") ?? suites[0];
redirect(`/suites/${landing?.id ?? "s1"}`);
```
`"s1"` was a mock-only fallback — safe as long as mock mode's seed
guarantees at least one suite, which is every environment this was ever
tested in before now. In a genuinely empty real project, `suites` is `[]`,
`suites[0]` is `undefined`, and the redirect target becomes the literal
string `"s1"` — an id that exists nowhere in the real database. The real
`getSuite("s1")` correctly returns nothing, `[suiteId]/page.tsx` calls
`notFound()`, and the one screen a brand-new project needs most — create
your first suite — was unreachable through normal navigation. This also
explains both symptoms as one: "Run suite" showed no suite option because
zero suites exist, and "Generate scenarios" (suite-scoped, only ever
rendered from inside a suite's own detail page) could never even be
opened, because that detail page could never be reached in the first
place — not a separate failure in either feature.
**Fix:** `(shell)/suites/page.tsx` no longer redirects when `suites` is
empty — it renders a new `EmptySuitesState` component directly ("No test
suites yet." + a "New suite" button, reusing the same `SuiteEditorModal`
`suite-screen.tsx` already uses, routing to the new suite's own page on
save). The non-empty path (`s3`-or-first-suite redirect) is unchanged,
confirmed live in `pnpm dev` mock mode after the fix.
**Repo:** frontend. **Status:** resolved — `pnpm typecheck`/`lint`/`test`/
`build` all clean (466 tests; new `(shell)/suites/page.test.tsx` pins both
the redirect and the empty-state paths). The follow-up question this entry
left open — whether "Generate scenarios" has a further genuine issue once
a suite can actually exist — is answered in `#36` below.

### 36. "Generate scenarios" 422s on every agent this app can create — no UI ever wrote `prompt`/`description`, and the real error was hidden by production redaction

**Deviates:** live-verified the follow-up from `#35`: created a real suite
on the deployed preview (now reachable), opened "Generate scenarios,"
clicked Generate — got the exact `#32`/`#33`-class redaction bug again
("An error occurred in the Server Components render..."), because
`generateScenariosAction` was still a throw-based wrapper. Pulled the real
error from Vercel's server logs (`vercel logs`, not guessed): `Error
[PythonApiError]: this agent has no prompt authored yet -- add one before
generating from it`, `status: 422`. Read `generate_scenarios`'s real
implementation (`app/api/suites.py`'s `_generation_context`, confirmed
directly): the `agent_prompt` source requires `agents.prompt` non-empty,
`agent_description` requires `agents.description` non-empty — both 422
with a clear message otherwise. Neither field was ever collectable
anywhere in this app: `AgentCreate`/`AgentUpdate` have always had
`prompt?`/`description?` on the wire, but `connect-screen.tsx`'s create
form and `agent-edit-modal.tsx`'s edit form never rendered a field for
either — every agent this app can create is structurally unable to use
either "Generate scenarios" source. Not a mock-mode gap either: mock's own
`generateScenarios` never checked these fields at all (an un-asserted gap
of its own, now moot since the real check is what matters and the fields
exist to satisfy it).
**Fix:** two independent problems, both fixed —
1. `generateScenariosAction` now returns `ActionResult<ScenarioGenerateResponse>`
   (the same `toActionResult` helper `#32`/`#33` introduced, generalized —
   renamed from `AgentActionResult`/`toAgentActionResult` since this is no
   longer agent-only) instead of throwing, so the real 422 message reaches
   the UI in production, not a generic one. `generate-scenarios-modal.tsx`
   reads `.ok`/`.message` instead of try/catch.
2. Added "System prompt" and "Description" fields (both optional
   `<textarea>`s, a new shared `textareaClass` in `agent-form-constants.ts`)
   to both the create form and the edit modal, wired into
   `AgentCreate.prompt`/`.description` and `AgentUpdate.prompt`/
   `.description`. This is what actually makes "Generate scenarios"
   reachable for an agent connected through this app at all — the
   redaction fix alone would only have made the 422 readable, not
   resolvable.
**Repo:** frontend. **Status:** resolved — `pnpm typecheck`/`lint`/`test`/
`build` all clean (469 tests; new coverage in `connect-screen.test.tsx`,
`connected-agents-list.test.tsx`, `generate-scenarios-modal.test.tsx`), and
live-verified end to end against the real deployed backend after
redeploy: opened Edit on the real test agent, filled in "System prompt,"
saved (real `PATCH`); went back to the suite, clicked "Generate
scenarios," and this time it actually generated — two real LLM-authored
drafts ("Account Balance Inquiry," "Unauthorized Account Access Attempt"),
not an error. Closed without adding them to the suite (no need for that
write just to confirm the fix). The real suite created for this
investigation (`Claude verification suite`, project "Default
Organization / Test Project") was left in place — this app's suite
editor has no delete action in its UI to clean it up with, a separate,
pre-existing gap from this ticket.

### 37. Backend-side audit (user-requested) — a batch parent's own missing `scenario_id` silently breaks two separate real-mode reads; dashboard 500 was a missed migration, now RESOLVED

Triggered by the user hitting a real suite-batch run for the first time
against the real deployed backend and reporting three symptoms in a row
(dashboard not populating, History showing only one legible run, a
live/history tab flicker). Dug into the real backend source directly
(`QA_test_backend`, read-only — no changes made there) rather than
guessing from symptoms alone.

**Confirmed — one root cause, two separate symptoms:** a suite batch's
*parent* run row never gets its own `scenario_id` (only its children do —
`app/api/suites.py`'s `_INSERT_SUITE_PARENT_SQL` vs
`_INSERT_CHILD_RUN_SQL`). Two different real-mode queries both resolve a
run's suite identity by joining `scenario_id -> suite_id`, and both break
on a batch parent the same way, for different visible reasons:
- **History table's "Suite" column is blank for every batch parent.**
  `app/api/runs.py`'s `_LIST_RUNS_SQL` uses a `LEFT JOIN`, so the row still
  appears but with an empty suite name — confirmed live (`/runs?tab=
  history` showed a long list of real runs, almost all blank-named,
  against the real deployed backend). This is very likely what read as
  "history only shows about one run" — the one row with a real name (a
  childless single-scenario run, which does carry its own `scenario_id`)
  stood out; every actual multi-call batch was present but indistinguishable from
  each other.
- **"Pass rate by suite" silently drops batch parents from the dashboard
  trend chart entirely.** `app/api/dashboard.py`'s `_PASS_RATE_BY_SUITE_SQL`
  (lines ~397-404) uses an `INNER JOIN` on the same `scenario_id ->
  suite_id` chain — a batch parent doesn't just render blank here, it's
  excluded from the query's result set outright. No crash, just quietly
  missing data, consistent with the dashboard's own "No suite activity
  yet." empty state potentially under-reporting real activity rather than
  genuinely having none.
**Fix (backend):** both queries need a real way to resolve a batch
parent's own suite identity that doesn't depend on the parent's own
(always-null) `scenario_id` — e.g. join through any one of its children's
`scenario_id` instead, the same pattern `app/workers/rollup.py`'s
`_BATCH_RUBRIC_SQL` already uses for resolving a batch's rubric. Not a
frontend fix — `src/components/runs/history-table.tsx` and the dashboard
suite-trend chart already render whatever `r.suite`/`suiteName` they're
given correctly; the gap is in what the backend sends.
**Repo:** brain. **Status:** History half resolved (2026-10-14, branch
`fix/deviations-19-20-21-37`, commit `5f3610a`) — pending merge; the
dashboard half was never a bug (see correction below).

**CORRECTION (2026-10-14, checked against the code):** the dashboard half
above is wrong. `_PASS_RATE_BY_SUITE_SQL` already restricts to leaf calls
via `_LEAF_CALLS_PREDICATE` (`app/api/dashboard.py`), deliberately, so a
batch parent is not counted alongside its own children. The children do
carry `scenario_id`, so the suite trend loses no real activity; the INNER
JOIN excluding the parent is intended. No change was made there, and it
should not be "fixed" to join through a child (that would double-count).

**Resolution (History half):** `_LIST_RUNS_SQL` and `_RUN_DETAIL_SQL` now
resolve the suite via `COALESCE(sc.suite_id, <any child's scenario's
suite>)` (`_SUITE_ID_EXPR`, `app/api/runs.py`). This fixes the blank
History suite name, `RunDetail.suiteName` for a batch parent, and the
`?suiteId=` filter (which previously missed batch parents). Scenario and
persona are deliberately left as the row's own (a parent has none).

**RESOLVED — the dashboard 500.** Confirmed: the deployed Railway database
was behind the migration chain. `budget_usd_monthly` (added on `projects`
by the newest migration, `4dee343ffc00_018_dashboard_v2.py`, per its own
"purely additive -- safe to run while old code is live" docstring) didn't
exist on the live DB yet, even though the app code querying it
(`_fetch_spend`'s `_PROJECT_BUDGET_SQL`) had already been deployed —
exactly the gap `CONTRIBUTING.md` warns about: migrations are never run
automatically on deploy (`railway.json`'s `startCommand` is plain
`uvicorn`, no `alembic upgrade head` — removed deliberately after an
earlier attempt caused a silent healthcheck hang) and have to be run
manually against the real `DATABASE_URL`. User ran `uv run alembic
upgrade head` against the production database directly. Re-verified live
immediately after: `/dashboard` now renders fully — "5 agents · 15 calls
in the last 7 days," real pass rate/latency/outcome-mix/spend, no more
`getDashboardV2 failed against Python backend` warning in Vercel's logs
(confirmed via `vercel logs`, not just visually). **Status:** resolved.

**Checked and still accurate (stale-check only, no new finding):** `#19`
(no per-call identity on the `fields=light` polling endpoint — re-read
`_run_delta_row`/`RunDeltaRow` directly, still true), `app/workers/
rollup.py`/`reaper.py`/`claim.py` (stale-run recovery and parent-closing
logic look deliberately defensive — `FOR UPDATE SKIP LOCKED`, a 60s stale
threshold — nothing found suggesting a run could get stuck live forever
or a parent could fail to close). `app/api/dashboard.py`'s own
`_fetch_recent_runs` does NOT share the #37 join gap — it already
LEFT JOINs with an explicit fallback, unlike `_LIST_RUNS_SQL`/
`_PASS_RATE_BY_SUITE_SQL`.

- **#28 — Phase W2.8 (Dashboard v2) built ahead of `contract-v1.4.0`.**
  Resolved 2026-10-08: `QA_test_backend` tagged and pushed
  `contract-v1.4.0` (`B2.8-01..04`). Reconciled `contract.lock` →
  `pnpm sync:contract` → diffed every hand-authored shape against the real
  `contract/openapi.json`. **What the guess got wrong, concretely:**
  - **The real envelope kept the old v1 `DashboardMetrics` name and
    *extended* it in place** (`avgLatency`/`trendBars`/`scenarioCoverage`
    etc. still required, right alongside the new `posture`/`latency`/
    `passRateBySuite`/`spend`/`recentRuns`/`agentHealth` fields) — this app
    had assumed a clean replacement and invented a `DashboardV2` name for
    what's really still `DashboardMetrics` on the wire. `DashboardV2`
    stays as this app's own name, now explicitly documented as a
    *projection* of the real `DashboardMetricsReal` (`= components["schemas"]
    ["DashboardMetrics"]`) down to only the v2 fields this screen reads.
  - **`agentHealth` is bundled into the same response**, not a second call
    — the original build kept calling `getAgentHealth()` separately from
    `page.tsx` out of habit (carried over from the v1 page). Removed; the
    screen now gets it off `dashboard.agentHealth` and refetches it for
    free on every window switch. **Degraded-mode behavior change, worth
    recording:** a `getDashboardV2` failure now blanks the Agent health
    panel too (both come from one call), where the old two-call design let
    Agent health keep working independently of a dashboard-endpoint
    failure — defensible, since the contract genuinely bundles them now,
    but a real behavior change, not just a refactor.
  - **`SuitePassRateSeries` is a flat `dailyPassRate: (number|null)[]`,
    always the trailing 14 calendar days, explicitly *not* re-scoped by
    `window`** — not `{suiteId, suiteName, color, points: {day,
    passRatePct}[]}` with a different point-count per window. Killed the
    original per-window suite-trend seed (2/14/30 points) and
    `SPARSE_POINT_THRESHOLD`-on-array-length sparse signal; `line-chart.tsx`
    now reads sparseness off *non-null count within* the fixed 14-length
    array, and renders interior `null` gaps as actual line breaks
    (segments), not a short array. `color` is genuinely absent from the
    wire (confirmed, not merely unreconciled) — assigned client-side by
    series position from a fixed palette.
  - **`PostureCards.passRatePct` is a number, not a formatted `passRate`
    string** — there is no `"—"` sentinel on the real wire at all. The
    posture strip's empty-state signal moved to `posture.callsRun === 0`,
    and `openCriticalFindingsBreakdown` (a hand-authored string) became
    `openCriticalFindingsByCategory: {[category]: count}`, humanized
    client-side (`formatFindingsBreakdown`, `dashboard-screen.tsx`).
    `latencyP95Ms`/`p50Ms`/`p90Ms`/`p95Ms` are all nullable on the real
    wire (the guess had them as plain numbers) — `percentile-bars.tsx` now
    drops a null percentile rather than rendering a fake 0ms bar.
  - **`Outcome.review` doesn't exist — it's `warnings`**, and the real
    schema also sends a pre-computed `passPct` the UI now uses directly
    instead of recomputing it client-side (`warnPct` for the donut's
    second ring is still client-computed; there's no real field for that).
  - **The dashboard's own recent-runs row is a dedicated `RecentRun`
    schema** (`{id, name, agentName, trigger, callCount, passRate, status}`),
    not the shared `DashboardRunRow` the original build reused out of
    convenience. It genuinely carries `callCount` — which `DashboardRunRow`
    still lacks (`#20`, amended: that gap now stands for the History table
    specifically, not the dashboard). It has `id` but no separate `runId`
    — inferred, not yet empirically confirmed against a populated real
    account (the deployed backend's own account had zero seeded projects
    at reconciliation time, discovered via a live `curl` against it), that
    `.id` is the openable run id, mirroring the backend's own documented
    `_dashboard_run_row` convention (`id === runId` always) for the
    sibling type. **Re-verify the first time this screen renders against a
    real, populated account.**
  - **`SpendSummary` field names are `costPerCall`/`windowSpend`** (no
    `Usd` suffix), and `costPerCall` is independently nullable from
    `windowSpend` — the guess had `costPerCallUsd`/`windowSpendUsd`, both
    non-null numbers.
  - **`FailingCheck.kind` is only `"assertion" | "metric"`** — the guess's
    4-way `assertion`/`llm_judge`/`builtin`/`python` split didn't survive.
    Real field names are `checkId` (new), `scopeLabel` (a single display
    string, not a `suiteNames` array to join), `lastSeenAt` (not
    `lastSeenLabel` — confirmed to still be a pre-formatted display string
    like `RunDetail.createdAt`, by comparing both fields' identical
    schema-level shape: bare `type: string`, no `format: date-time`).
  **What's permanently client-side, not a future reconciliation target:**
  `DashboardV2.callCountLabel` (formatted from the real `posture.callsRun`
  + `window`, no such field on the wire) and `DashboardV2.suiteTrendGatePct`
  (derived from the shown suites' own `SuiteRubric.minPassRate` via one
  extra `getSuites()` call — confirmed absent from the wire entirely, not
  merely unreconciled). There is also no real `suiteCount` — dropped from
  the header subtitle rather than approximated from `passRateBySuite.length`
  (which would undercount: that array only includes suites with calls in
  the trailing 14 days).
  **Repo:** both.
- **#14 — Phase W2.5 built ahead of `contract-v1.1.0`.** Resolved 2026-08-11:
  `QA_test_backend` tagged and pushed `contract-v1.1.0` (B2.5-01/02/03/04).
  Reconciled `contract.lock` → `pnpm sync:contract` → diffed every
  hand-authored shape against the real `contract/openapi.json` (fetched the
  tag directly and read `app/schemas/runs.py`/`app/api/runs.py` at that
  commit, not just the generated types, to resolve ambiguities the schema
  alone couldn't answer). **What the guess got wrong, concretely:**
  - **No `TestRun`/`Call` type split at the wire level.** The backend uses
    one unified `RunDetail`/`DashboardRunRow` shape for both, distinguished
    by `isParent: boolean` + nullable `parentRunId`/`aggregate` — not two
    disjoint schemas. `src/types/index.ts` collapsed `TestRun`+`Call` into a
    single `RunDetail` alias to match; `repository.ts`'s `getTestRun`+`getCall`
    collapsed into one `getRun`.
  - **`RunAggregate`'s real fields** are `callCount`, `statusCounts` (a
    `{[status]: number}` dict, not five named counters), `passRate`,
    `latencyP50Ms`/`P90Ms`/`P95Ms` (three percentiles, not one
    `avgLatencyMs`), `totalCostUsd`, `distinctScenarioCount`,
    `distinctPersonaCount` — none of which matched the guessed
    `CallAggregate` (`total`/`passed`/`failed`/`running`/`queued`/`cancelled`/
    `avgLatencyMs`/`costUsd`).
  - **`DashboardRunRow` kept its original name** (not renamed to
    `TestRunSummary`) and gained `isParent`/`parentRunId`/required
    (non-optional) `projectId` — no invented `testRunId` field; for a parent
    row, `id` already is the Test Run's id.
  - **`getCallsForTestRun` (`?parentRunId=`) returns row-shaped
    `DashboardRunRow[]`, not full `RunDetail[]`** — confirmed against
    `app/api/runs.py`'s `list_runs`. The `/runs/[testRunId]` page's original
    two-step fetch (list children, then fetch one's detail) was also
    unnecessary for this phase: a childless Test Run's own `GET /v1/runs/{id}`
    response already carries both the `aggregate` and the call-level
    transcript/score fields on the same object (confirmed against
    `_compute_aggregate`, which reduces from the run's own row when it has
    no real children) — simplified to one fetch.
  - **`Organization`/`OrganizationProject`** (renamed from the guessed
    `OrgProjectSummary`) matched the hand-authored guess exactly,
    field-for-field — the one shape that needed no changes.
  - **`Project` gained a required `createdAt`** the pre-B2.5 stub didn't have.
  - **`X-Project-Id` is not a declared OpenAPI parameter** (same pattern as
    the pre-existing `X-User-Id`/`X-User-Email`) — confirms the header-based
    approach in `python-client.ts` was directionally correct without changes.
  See `DECISIONS.md` #18 for the one place this repo still knowingly falls
  short of B2.5-02's literal ask (per-function type enforcement vs. ambient
  cookie enforcement) — that gap is unchanged by this reconciliation.
- **#15 — `generated/schema.d.ts` had zero real consumers.** Resolved
  2026-08-11, alongside #14: `src/types/index.ts` now aliases
  `components["schemas"][...]` directly for every type this reconciliation
  touched (`RunDetail`, `RunAggregate`, `DashboardRunRow`, `Organization`,
  `OrganizationProject`, `Project`, `RunCost`, `TranscriptTurn`,
  `ResultAssertion`) — `pnpm typecheck` now actually fails if a future
  contract bump changes one of these shapes and the consuming code doesn't
  follow. Types outside this reconciliation's scope are tracked as a new
  open item, #16.
