"""011_fix_builtin_persona_voices

Revision ID: 52bb225d68a4
Revises: 79c979f32a2d
Create Date: 2026-09-27 16:22:42.682089

Data-only, no schema change. B2.7-02 added real voice/language validation
against Google Cloud TTS's Chirp3-HD registry (app.engine.caller.
tts_voices) -- which the three seeded built-in personas immediately fail:
they carry OpenAI TTS voice names ("alloy"/"verse"/"shimmer") and a bare,
non-BCP-47 `language` ("en"), left over from before any engine actually
read these columns. Built-ins are read-only via the API (B2.7-02), so
there's no way to fix them except a migration.

Maps each persona's existing `accent` string to the matching Chirp3-HD
locale and picks one confirmed-real voice name per locale (fetched live
against the actual ListVoices API while writing this migration, not
guessed): "Indian English" -> en-IN, "American English" -> en-US (Charon,
already used elsewhere in this codebase -- app/engine/caller/
persona_call.py's own hardcoded default), "British English" -> en-GB.
`app/seed.py`'s source values are updated in the same commit so a fresh
database seeds correctly too; that alone would not have fixed these three
already-seeded rows (`_seed_personas`'s INSERT is `ON CONFLICT DO NOTHING`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "52bb225d68a4"
down_revision: str | Sequence[str] | None = "79c979f32a2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXES = {
    "Priya Sharma": {
        "old": ("alloy", "en"),
        "new": ("en-IN-Chirp3-HD-Achernar", "en-IN"),
    },
    "Frustrated Frank": {
        "old": ("verse", "en"),
        "new": ("en-US-Chirp3-HD-Charon", "en-US"),
    },
    "Elderly Elena": {
        "old": ("shimmer", "en"),
        "new": ("en-GB-Chirp3-HD-Achernar", "en-GB"),
    },
}


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    for name, fix in _FIXES.items():
        voice, language = fix["new"]
        conn.execute(
            sa.text("UPDATE personas SET voice = :voice, language = :language WHERE name = :name"),
            {"voice": voice, "language": language, "name": name},
        )


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    for name, fix in _FIXES.items():
        voice, language = fix["old"]
        conn.execute(
            sa.text("UPDATE personas SET voice = :voice, language = :language WHERE name = :name"),
            {"voice": voice, "language": language, "name": name},
        )
