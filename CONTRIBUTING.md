# Contributing

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
