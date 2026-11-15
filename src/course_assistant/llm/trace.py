"""Trace records with no learner or answer text (OAI-05, D-14, D-15).

`strip_text` is an allowlist: a key reaches a sink only if it is listed here, so a new field
holding text can never slip into a trace by being forgotten in a denylist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

TRACE_KEYS = frozenset({
    "request_id", "step", "version", "prompt_hash", "rules_hash", "purpose", "model",
    "model_snapshot", "learner_pid", "conversation_id", "question_id", "turn_index",
    "route", "status", "top_score", "n_citations", "n_hits", "grounding_passed",
    "integrity_passed", "tokens_in", "tokens_cached", "tokens_cache_write", "tokens_out",
    "tokens_reasoning",
    "file_search_calls", "cost_usd", "latency_ms", "attempts", "error", "refusal", "at",
})  # fmt: skip


def strip_text(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k in TRACE_KEYS}


class TraceSink(Protocol):
    def emit(self, record: dict[str, Any]) -> None: ...


class JsonlSink:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def emit(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(strip_text(record), sort_keys=True, default=str) + "\n")
