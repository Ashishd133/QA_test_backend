#!/bin/sh
# Applies all pending Alembic migrations to the production DATABASE_URL
# (app.config.get_settings().database_url). Run this whenever a
# schema-changing migration lands on main, BEFORE pushing/deploying the
# code that depends on it -- Railway's auto-deploy of main will 500 every
# endpoint touching a new/changed column otherwise. Used for migrations
# 005-007, 008 (35efca88855b, run triggers), and 009 (bd2245aea43c,
# rerun_of_run_id) so far; not a one-off, keep it.
set -e
cd "$(dirname "$0")/.."
. .venv/bin/activate
export DATABASE_URL="$(python3 -c 'from app.config import get_settings; print(get_settings().database_url)')"
python3 -m alembic upgrade head
