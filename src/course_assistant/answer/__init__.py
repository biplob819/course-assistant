"""The answer path: route, answer, grounding, integrity and hints (wave 2)."""

from __future__ import annotations

import functools
from typing import Any

from course_assistant.answer.hint import Hint, HintStep
from course_assistant.answer.integrity import IntegrityResult, IntegrityStep
from course_assistant.answer.route import CATEGORIES, Responder, RouteParseError, RouteStep
from course_assistant.llm import respond


def responder(**kw: Any) -> Responder:
    """`respond` with its client, prices, ledger and sink bound, for the steps to call."""
    return functools.partial(respond, **kw)


__all__ = [
    "CATEGORIES",
    "Hint",
    "HintStep",
    "IntegrityResult",
    "IntegrityStep",
    "Responder",
    "RouteParseError",
    "RouteStep",
    "responder",
]
