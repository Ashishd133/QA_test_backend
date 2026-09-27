"""Generic app-layer encryption at rest (B2.7-03), reused by B4-05 for
`findings.evidence` per that ticket's own text ("reuses B2.7-03's
encryption path rather than inventing a second one, if B2.7-03 landed
first"). Anything storing sensitive jsonb should go through this module,
not invent a second scheme.

`MultiFernet` over `FIELD_ENCRYPTION_KEYS` (comma-separated Fernet keys):
the first key encrypts new data, every key can decrypt -- so rotation is
"prepend a new key, keep the old one around until nothing needs it",
available from day one rather than retrofitted once it's actually needed.

Read through `app.config.Settings` (not `os.environ` directly): local dev's
`.env` is loaded by `BaseSettings` into `Settings` but never copied into
`os.environ`, while Railway sets a real platform env var and no `.env`
file at all -- going through `get_settings()` resolves both the same way,
the same reason `database_url` lives there instead of being read raw.

Fails closed, not lazily-plaintext: a missing/empty key raises the moment
encrypt/decrypt is actually called (not at import time, so merely
importing this module never requires the key to exist), and there is no
code path that silently writes plaintext instead.

Storage format: `{"v": 1, "ct": "<token>"}`, not a bare token string -- the
version field means a future scheme change is a matter of branching on
`v`, not guessing which rows predate it.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TypedDict

from cryptography.fernet import Fernet, MultiFernet

from app.config import get_settings

_FORMAT_VERSION = 1


class EncryptedEnvelope(TypedDict):
    v: int
    ct: str


@lru_cache(maxsize=1)
def _multi_fernet() -> MultiFernet:
    raw = get_settings().field_encryption_keys
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise RuntimeError(
            "FIELD_ENCRYPTION_KEYS is not set -- refusing to encrypt/decrypt rather than "
            "silently falling back to plaintext. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt_json(value: dict[str, object]) -> EncryptedEnvelope:
    token = _multi_fernet().encrypt(json.dumps(value).encode()).decode()
    return {"v": _FORMAT_VERSION, "ct": token}


def decrypt_json(envelope: dict[str, object]) -> dict[str, object]:
    if envelope.get("v") != _FORMAT_VERSION:
        raise ValueError(f"unsupported encryption envelope version: {envelope.get('v')!r}")
    token = envelope["ct"]
    if not isinstance(token, str):
        raise ValueError("encryption envelope's 'ct' must be a string")
    plaintext = _multi_fernet().decrypt(token.encode())
    result = json.loads(plaintext)
    if not isinstance(result, dict):
        raise ValueError("decrypted payload is not a JSON object")
    return result
