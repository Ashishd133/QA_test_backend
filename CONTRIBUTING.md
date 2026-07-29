# Contributing

## Test database

DB-touching tests need `TEST_DATABASE_URL` set (a separate Neon branch, not
`DATABASE_URL`) — never point tests at the production DB. The deployed
`worker` service continuously polls that DB for `status='queued'` runs
(spine §5); it will claim and execute a test-inserted queued row before the
test's own assertions run (discovered during B1-01, when this broke most of
`test_workers.py`). `tests/conftest.py`'s `_test_engine()`/`requires_test_db`
are what every DB test builds its engine from and gates its collection on —
new DB tests must use them, not `app.db.build_engine()`/`get_settings().database_url`
directly. Bring a fresh branch to head with:
```
DATABASE_URL=<test branch URL> uv run alembic upgrade head
```

## Contract sync (cadence-brain ↔ frontend)

This repo is the source of truth for the API contract (spine §2). Every route
is a FastAPI endpoint; `contract/openapi.json` is generated from the running
app, committed, and CI fails if it drifts from what the code actually
produces.

**When you change a route, model, or event schema:**

1. Regenerate the contract:
   ```
   uv run python -m scripts.export_openapi
   ```
2. Commit `contract/openapi.json` alongside your code change.
3. If the change is contract-visible (new/changed endpoint, request/response
   shape, or SSE event payload), tag the commit once it's on `main`:
   ```
   git tag contract-vX.Y.Z
   git push origin contract-vX.Y.Z
   ```
   - **patch** (`v0.1.0` → `v0.1.1`): additive, backward-compatible (new
     optional field, new endpoint).
   - **minor** (`v0.1.0` → `v0.2.0`): meaningful new capability that the
     frontend needs to opt into (new required field, new event type).
   - **major** (`v0.Y.0` → `v1.0.0`): breaking change to an existing shape.
     Coordinate with the frontend before merging.

The frontend's `pnpm sync:contract` fetches a pinned tag and regenerates its
TypeScript types — it never reads off `main` directly. Don't tag until the
change is merged and CI is green.

**Local checks before pushing** (same as CI):
```
uv run ruff check .
uv run mypy app scripts
uv run pytest -q
uv run python -m scripts.export_openapi --check
```

## Deploying (Railway)

`api` and `worker` are two Railway services in the same project, both built
from the repo's `Dockerfile`. `worker`'s service settings point its Config
File Path at `railway.worker.json` instead of the default `railway.json`,
since Railway applies the root `railway.json` to every service on the repo
by default — without that override, `worker` would run `api`'s uvicorn
command instead of its own claim/reaper loop.

**Migrations are not run on boot.** An earlier attempt chained
`alembic upgrade head` into the container start command; combined with a
Railway command-parsing quirk (its startCommand doesn't do shell `$VAR`
expansion unless explicitly wrapped in `sh -c '...'`), this produced a
deploy that hung silently for the entire healthcheck window. Rather than
depend on a migration finishing inside that window on every boot, run
migrations from local (or CI, once that exists) before deploying:
```
uv run alembic upgrade head
```
