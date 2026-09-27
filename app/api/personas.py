"""B2.7-02: /v1/personas CRUD + condition fields.

Supersedes app/api/dashboard.py's `GET /v1/personas` (B1-06, unscoped,
never consumed by any frontend type) with the real, project-scoped
version: a persona is visible if it's built-in (`project_id IS NULL`,
read-only project-wide) or belongs to the caller's current project.
"Duplicate to project" (`POST /v1/personas/{id}/duplicate`) is how a user
gets an editable copy of a built-in -- PATCH/DELETE on a built-in itself
409s rather than 403ing, matching the "refuse the write with a clear
reason" shape used elsewhere (agents.py's in-use delete) rather than a
bare permission error.

Voice/language validated against app.engine.caller.tts_voices' live
Chirp3-HD registry (only fields the engine can actually act on; `accent`
is a display-only label -- see that module's docstring for why) and 422s
with the supported set on a bad combination, per the ticket's own words:
"return 422 with the supported set rather than failing at call time."
"""

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import get_engine
from app.deps import ensure_project_match, require_project_id, require_user_id
from app.engine.caller.tts_voices import is_supported, supported_languages, supported_voices
from app.errors import APIError
from app.schemas.personas import PersonaCreate, PersonaDetail, PersonaUpdate

router = APIRouter(tags=["personas"])

_PERSONA_COLUMNS = (
    "id, project_id, name, voice, language, accent, traits, builtin, "
    "emotion, speaking_rate, interruption_behavior, environment, code_switch"
)


def _persona_detail(row: RowMapping) -> PersonaDetail:
    return PersonaDetail(
        id=str(row["id"]),
        project_id=str(row["project_id"]) if row["project_id"] is not None else None,
        name=row["name"],
        voice=row["voice"],
        language=row["language"],
        accent=row["accent"],
        traits=row["traits"],
        builtin=row["builtin"],
        emotion=row["emotion"],
        speaking_rate=row["speaking_rate"],
        interruption_behavior=row["interruption_behavior"],
        environment=row["environment"],
        code_switch=row["code_switch"],
    )


async def _validate_voice(language: str, voice: str) -> None:
    if await is_supported(language=language, voice=voice):
        return
    raise APIError(
        "unsupported_voice",
        f"voice {voice!r} is not available for language {language!r}",
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        details={
            "language": language,
            "voice": voice,
            "supportedLanguages": await supported_languages(),
            "supportedVoicesForLanguage": await supported_voices(language),
        },
    )


@router.get("/v1/personas", response_model=list[PersonaDetail])
async def list_personas(
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> list[PersonaDetail]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        f"SELECT {_PERSONA_COLUMNS} FROM personas "
                        "WHERE project_id IS NULL OR project_id = :project_id "
                        "ORDER BY builtin DESC, name"
                    ),
                    {"project_id": project_id},
                )
            )
            .mappings()
            .all()
        )
    return [_persona_detail(row) for row in rows]


@router.post("/v1/personas", response_model=PersonaDetail, status_code=status.HTTP_201_CREATED)
async def create_persona(
    body: PersonaCreate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> PersonaDetail:
    await _validate_voice(body.language, body.voice)
    async with engine.connect() as conn, conn.begin():
        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO personas "
                        "(id, project_id, name, voice, language, accent, traits, "
                        " emotion, speaking_rate, interruption_behavior, environment, code_switch) "
                        "VALUES (:id, :project_id, :name, :voice, :language, :accent, "
                        " CAST(:traits AS jsonb), :emotion, :speaking_rate, "
                        " :interruption_behavior, :environment, CAST(:code_switch AS jsonb)) "
                        f"RETURNING {_PERSONA_COLUMNS}"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_id,
                        "name": body.name,
                        "voice": body.voice,
                        "language": body.language,
                        "accent": body.accent,
                        "traits": _dump_json(body.traits),
                        "emotion": body.emotion,
                        "speaking_rate": body.speaking_rate,
                        "interruption_behavior": body.interruption_behavior,
                        "environment": body.environment,
                        "code_switch": _dump_json(body.code_switch) if body.code_switch else None,
                    },
                )
            )
            .mappings()
            .one()
        )
    return _persona_detail(row)


def _dump_json(value: object) -> str:
    return json.dumps(value)


async def _fetch_persona_or_404(
    conn: AsyncConnection, persona_id: uuid.UUID, project_id: uuid.UUID
) -> RowMapping:
    row = (
        (
            await conn.execute(
                text(f"SELECT {_PERSONA_COLUMNS} FROM personas WHERE id = :id"), {"id": persona_id}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise APIError("not_found", "persona not found", status.HTTP_404_NOT_FOUND)
    # A built-in (project_id NULL) is visible from every project; anything
    # else must belong to the caller's current project -- same cross-tenant
    # 404, not 403, rule as every other project-scoped resource.
    if row["project_id"] is not None:
        ensure_project_match(row["project_id"], project_id)
    return row


@router.get("/v1/personas/{persona_id}", response_model=PersonaDetail)
async def get_persona(
    persona_id: uuid.UUID,
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> PersonaDetail:
    async with engine.connect() as conn:
        row = await _fetch_persona_or_404(conn, persona_id, project_id)
    return _persona_detail(row)


@router.patch("/v1/personas/{persona_id}", response_model=PersonaDetail)
async def update_persona(
    persona_id: uuid.UUID,
    body: PersonaUpdate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> PersonaDetail:
    async with engine.connect() as conn, conn.begin():
        existing = await _fetch_persona_or_404(conn, persona_id, project_id)
        if existing["builtin"]:
            raise APIError(
                "builtin_read_only",
                "built-in personas cannot be edited -- duplicate it to your project first",
                status.HTTP_409_CONFLICT,
            )

        language = body.language if body.language is not None else existing["language"]
        voice = body.voice if body.voice is not None else existing["voice"]
        if body.language is not None or body.voice is not None:
            await _validate_voice(language, voice)

        updates: dict[str, Any] = {}
        for field in (
            "name",
            "voice",
            "language",
            "accent",
            "emotion",
            "speaking_rate",
            "interruption_behavior",
            "environment",
        ):
            value = getattr(body, field)
            if value is not None:
                updates[field] = value
        if body.traits is not None:
            updates["traits"] = body.traits
        if body.code_switch is not None:
            updates["code_switch"] = body.code_switch

        if updates:
            assignments = []
            params: dict[str, Any] = {"id": persona_id}
            jsonb_fields = {"traits", "code_switch"}
            for col, val in updates.items():
                clause = (
                    f"{col} = CAST(:{col} AS jsonb)" if col in jsonb_fields else f"{col} = :{col}"
                )
                assignments.append(clause)
                params[col] = _dump_json(val) if col in jsonb_fields else val
            await conn.execute(
                text(f"UPDATE personas SET {', '.join(assignments)} WHERE id = :id"), params
            )
        row = (
            (
                await conn.execute(
                    text(f"SELECT {_PERSONA_COLUMNS} FROM personas WHERE id = :id"),
                    {"id": persona_id},
                )
            )
            .mappings()
            .one()
        )
    return _persona_detail(row)


@router.delete("/v1/personas/{persona_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_persona(
    persona_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> None:
    async with engine.connect() as conn, conn.begin():
        existing = await _fetch_persona_or_404(conn, persona_id, project_id)
        if existing["builtin"]:
            raise APIError(
                "builtin_read_only", "built-in personas cannot be deleted", status.HTTP_409_CONFLICT
            )
        in_use = (
            await conn.execute(
                text("SELECT count(*) FROM scenarios WHERE persona_id = :id"), {"id": persona_id}
            )
        ).scalar_one()
        if in_use:
            raise APIError(
                "conflict",
                "persona is referenced by one or more scenarios and cannot be deleted",
                status.HTTP_409_CONFLICT,
            )
        await conn.execute(text("DELETE FROM personas WHERE id = :id"), {"id": persona_id})


@router.post(
    "/v1/personas/{persona_id}/duplicate",
    response_model=PersonaDetail,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_persona(
    persona_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> PersonaDetail:
    """The only way to get an editable copy of a built-in -- also usable on
    a persona already in the caller's own project (e.g. to fork a variant),
    since there's no reason to special-case that away."""
    async with engine.connect() as conn, conn.begin():
        source = await _fetch_persona_or_404(conn, persona_id, project_id)
        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO personas "
                        "(id, project_id, name, voice, language, accent, traits, builtin, "
                        " emotion, speaking_rate, interruption_behavior, environment, code_switch) "
                        "VALUES (:id, :project_id, :name, :voice, :language, :accent, "
                        " CAST(:traits AS jsonb), false, :emotion, :speaking_rate, "
                        " :interruption_behavior, :environment, CAST(:code_switch AS jsonb)) "
                        f"RETURNING {_PERSONA_COLUMNS}"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_id,
                        "name": f"{source['name']} (copy)",
                        "voice": source["voice"],
                        "language": source["language"],
                        "accent": source["accent"],
                        "traits": _dump_json(source["traits"]),
                        "emotion": source["emotion"],
                        "speaking_rate": source["speaking_rate"],
                        "interruption_behavior": source["interruption_behavior"],
                        "environment": source["environment"],
                        "code_switch": (
                            _dump_json(source["code_switch"]) if source["code_switch"] else None
                        ),
                    },
                )
            )
            .mappings()
            .one()
        )
    return _persona_detail(row)
