"""Dump the OpenAPI schema for the frontend client (F1 commit 4).

The backend stays the single schema authority: this regenerates
`web/src/api/openapi.json` from the real app factory — no DB, no network
(`make gen-api` runs it, then `openapi-typescript` types the snapshot).
"""

from __future__ import annotations

import json
from pathlib import Path

from jarvis.api.app import create_app

TARGET = Path(__file__).resolve().parent.parent / "web" / "src" / "api" / "openapi.json"


def main() -> None:
    spec = create_app().openapi()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"wrote {TARGET} ({len(spec['paths'])} paths)")


if __name__ == "__main__":
    main()