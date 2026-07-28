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

# Overridden per-service on Railway (api chains `alembic upgrade head`; the
# worker service points this at `app.workers.main` instead) — this default
# only serves local `docker run` testing.
CMD uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
