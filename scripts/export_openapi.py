"""Exports the FastAPI OpenAPI schema to contract/openapi.json.

Deterministic by construction: sorted keys, fixed indent, trailing newline.
CI (see .github/workflows/ci.yml) runs this with --check to fail the build
on any undocumented contract drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract" / "openapi.json"


def render_schema() -> str:
    from app.main import app

    schema = app.openapi()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if contract/openapi.json would change, without writing it.",
    )
    args = parser.parse_args()

    rendered = render_schema()

    if args.check:
        current = CONTRACT_PATH.read_text() if CONTRACT_PATH.exists() else ""
        if current != rendered:
            print(f"Contract drift detected: {CONTRACT_PATH} is out of date.", file=sys.stderr)
            print(
                "Run `uv run python -m scripts.export_openapi` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("Contract is up to date.")
        return 0

    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(rendered)
    print(f"Wrote {CONTRACT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
