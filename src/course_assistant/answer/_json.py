"""Strict JSON for model replies: a duplicate key is an error, never "last one wins"
(verifier A2.4 pass 1: {"verdict":"completes","verdict":"clear"} passed as clear)."""

from __future__ import annotations

import json
from typing import Any


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def strict_loads(text: str) -> Any:
    """`json.loads` that refuses duplicate keys at any depth (raises ValueError)."""
    return json.loads(text, object_pairs_hook=_no_duplicates)
