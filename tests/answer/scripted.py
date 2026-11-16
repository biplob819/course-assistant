"""A fake OpenAI client for ask(): one queue of SDK-model outcomes per step (L-9).

Route and answer run on two threads, and L2 and integrity too, so one shared queue would hand
outcomes to whichever call came first. Each call is matched to its step by the instructions it
carries (each step's rules are a class constant), and takes the next outcome from that queue.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from course_assistant.answer.answer import AnswerStep
from course_assistant.answer.ground import GroundStep
from course_assistant.answer.hint import HintStep
from course_assistant.answer.integrity import IntegrityStep
from course_assistant.answer.route import RouteStep
from tests.llm.fakes import make_response, status_error

SOL = "gpt-6.1-sol-2026-10-01"
LUNA = "gpt-6-luna-2026-10-01"
STEP_OF = {
    RouteStep.ROUTE_RULES: "route",
    AnswerStep.ANSWER_RULES: "answer",
    GroundStep.SUPPORT_RULES: "support",
    IntegrityStep.INTEGRITY_RULES: "integrity",
    HintStep.HINT_RULES: "hint",
}


class _Responses:
    def __init__(self, owner: StepClient) -> None:
        self.owner = owner

    def create(self, **kwargs: Any) -> Any:
        step = STEP_OF[kwargs["instructions"]]
        with self.owner.lock:
            self.owner.calls.append((step, kwargs))
            queue = self.owner.queues.get(step, [])
            if not queue:
                raise AssertionError(f"unexpected {step} call")
            out = queue.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


class StepClient:
    def __init__(self, **queues: list[Any]) -> None:
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.lock = threading.Lock()
        self.responses = _Responses(self)

    def with_options(self, **kw: Any) -> StepClient:
        return self

    def steps(self) -> list[str]:
        return [s for s, _ in self.calls]

    def left(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.queues.items() if v}


def luna(body: Any, refusal: str | None = None) -> Any:
    text = body if isinstance(body, str) else json.dumps(body)
    return make_response(text=text, citations=(), hits=(), file_search=False, model=LUNA,
                         tokens=(1500, 0, 60, 20), refusal=refusal)  # fmt: skip


def route(category: str, standalone: str = "A standalone question.",
          assessment_id: str | None = None, confidence: float = 0.9) -> Any:  # fmt: skip
    return luna({"category": category, "confidence": confidence,
                 "standalone_question": standalone, "assessment_id": assessment_id})  # fmt: skip


def support(*flags: bool) -> Any:
    return luna({"verdicts": [{"n": i, "supported": f} for i, f in enumerate(flags, start=1)]})


def integrity(verdict: str = "clear", assessment_id: str | None = None) -> Any:
    return luna({"verdict": verdict, "assessment_id": assessment_id})


def sol(text: str, cites: tuple[tuple[str, int], ...], hits: tuple[tuple[str, float], ...],
        refusal: str | None = None) -> Any:  # fmt: skip
    return make_response(text=text, citations=cites, hits=hits, model=SOL,
                         tokens=(4000, 1000, 400, 250), refusal=refusal)  # fmt: skip


def cited(text: str, file_id: str, score: float = 0.82) -> Any:
    """A one-sentence (or more) draft citing one file at its end, with one hit."""
    return sol(text, ((file_id, len(text)),), ((file_id, score),))


def down() -> list[Any]:
    """Three server errors: N1.2 retries twice, then a transport LLMError."""
    return [status_error(500), status_error(503), status_error(500)]
