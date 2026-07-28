FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependencies installed from the lockfile in their own layer, before the
# rest of the source, so editing app code doesn't invalidate the dependency
# cache on rebuild.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

ENV PYTHONUNBUFFERED=1

# Railway overrides this per-service via railway.json (api) / railway.worker.json
# (worker, which points at app.workers.main instead) — this default only serves
# local `docker run` testing. Migrations are NOT chained into either start
# command: run `alembic upgrade head` from local/CI instead (see CONTRIBUTING.md)
# — chaining it into the healthcheck-gated boot path proved fragile in practice.
CMD uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
