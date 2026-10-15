"""B4-01: attack packs -- versioned YAML under `attacks/`, validated at load.

The files *are* the registry (versioned in git, shipped with the image), so
there is no `attack_packs` table: the spine sketched one, but nothing needs
rows -- a run records the pack `id` and `version` it used in its own config,
which is the audit trail, and a table would only add a migration plus a
second copy of content that already has an owner. Revisit if/when custom
(per-project) attack authoring (spine §10.7) needs a place to live.

A pack is a multi-turn attack ("single-shot attacks are toy attacks"): an
`opening` line, then `escalations` the attacker works through if the agent
holds the line. `success_signals` tell the judge what a leak/bypass looks
like for this attack; `fix_template` is the remediation the judge makes
concrete.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.schemas.runs import AttackCategoryKey

PACKS_DIR = Path(__file__).resolve().parents[3] / "attacks"

# Same vocabulary the dashboard's "open critical findings" card filters on
# (app.api.dashboard._OPEN_CRITICAL_FINDINGS_SQL: severity = 'critical').
Severity = Literal["critical", "high", "medium", "low"]

MAX_ESCALATIONS = 4


class PackError(ValueError):
    """A pack file failed validation. The message names the file and the
    offending field so a content author can fix it without a debugger."""


class AttackPack(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    version: int = Field(ge=1)
    name: str = Field(min_length=1)
    category: AttackCategoryKey
    # True if the attack only makes sense from inside an authenticated
    # session (e.g. the reference agent's planted leak sits behind its
    # identity gate). Such a pack needs a dummy identity on the run.
    requires_verified: bool = False
    opening: str = Field(min_length=1)
    escalations: list[str] = Field(default_factory=list, max_length=MAX_ESCALATIONS)
    success_signals: list[str] = Field(min_length=1)
    severity_if_success: Severity
    fix_template: str = Field(min_length=1)

    @field_validator("opening", "fix_template")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("escalations", "success_signals")
    @classmethod
    def _no_blank_items(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("must not contain blank entries")
        return value

    @property
    def turns(self) -> int:
        return 1 + len(self.escalations)


def _describe(path: Path, error: ValidationError) -> str:
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(file)'}: {e['msg']}" for e in error.errors()
    )
    return f"{path.name}: {problems}"


def load_pack_file(path: Path) -> AttackPack:
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PackError(f"{path.name}: not valid YAML ({exc})") from exc
    if not isinstance(raw, dict):
        raise PackError(f"{path.name}: expected a mapping at the top level")
    try:
        return AttackPack.model_validate(raw)
    except ValidationError as exc:
        raise PackError(_describe(path, exc)) from exc


def load_packs(directory: Path = PACKS_DIR) -> list[AttackPack]:
    """Every `*.yaml` in `directory`, validated, sorted by (category, id).
    Fails on the first malformed file and on duplicate ids -- a half-loaded
    registry would silently drop attacks from a run."""
    packs: list[AttackPack] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.yaml")):
        pack = load_pack_file(path)
        if pack.id in seen:
            raise PackError(f"{path.name}: duplicate pack id {pack.id!r} (also in {seen[pack.id]})")
        seen[pack.id] = path.name
        packs.append(pack)
    return sorted(packs, key=lambda p: (p.category, p.id))


@lru_cache(maxsize=1)
def registry() -> tuple[AttackPack, ...]:
    return tuple(load_packs())


def select_packs(
    categories: Iterable[str], *, packs: Iterable[AttackPack] | None = None
) -> list[AttackPack]:
    wanted = set(categories)
    return [p for p in (packs if packs is not None else registry()) if p.category in wanted]
