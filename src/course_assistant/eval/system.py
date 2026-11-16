"""The course assistant as an EvalKit system under test (E2.2 EVK-02, D-10, D-11).

EvalKit builds a system adapter with no arguments, so what it answers with (config, corpus,
briefs, and where its model calls go) is installed once with `configure(Runtime(...))`. The
source is any A2.5 `Source`: a live responder, a `Recorder` around one (the live run keeps
its recordings), or a `Replay` of them (an offline rerun with no API call).

Each case runs through `Assistant.turn` on a fresh in-memory store, with the case's earlier
turns seeded as one conversation of the case's learner, and request id and learner pid derived
from the case id, so a recorded case replays by its id. The reply comes back as the task's
answer JSON: label = status, citations = 1-based unit indices (the case context is the corpus
units in order). Status `error` is not a label: it is raised as a `ConnectionError`, which
EvalKit records as a failed call (`error="transport"`), counted and never cached.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from evalkit.adapters import CompletionRequest, RawCompletion, SystemAdapter, register_system
from evalkit.schemas import Case

from course_assistant.answer import golden
from course_assistant.answer.ask import Assistant
from course_assistant.answer.replay import Source
from course_assistant.ingest.corpus import Corpus
from course_assistant.schemas.config import AppConfig
from course_assistant.schemas.corpus import AssessmentList
from course_assistant.schemas.request import Answer
from course_assistant.store.db import Store

SYSTEM_NAME = "course_ta"
BASE_VERSION = "2"
PER_CASE_USD = 0.05  # most one case can cost: answer, checks and a hint (Tech Spec §9, rounded up)


class SystemConfigError(RuntimeError):
    """The adapter cannot answer this case as configured. Not transient: EvalKit stops the run."""


@dataclass(frozen=True)
class Runtime:
    config: AppConfig
    corpus: Corpus
    assessments: AssessmentList
    source: Source
    now: Callable[[], dt.datetime] = field(default=lambda: dt.datetime.now(dt.UTC))
    timer: Callable[[], float] = time.monotonic


_RUNTIME: list[Runtime | None] = [None]


def _hex(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]


def request_id_for(case_id: str) -> str:
    return f"r-{_hex('course-ta', 'request', case_id)}"


def pid_for(case_id: str) -> str:
    return _hex("course-ta", "learner", case_id)


def version_for(runtime: Runtime) -> str:
    """Part of EvalKit's answer cache key: rules, config, assessment briefs (integrity and
    hints read them) and corpus all change answers."""
    config_hash = hashlib.sha256(runtime.config.model_dump_json().encode("utf-8")).hexdigest()
    briefs_hash = hashlib.sha256(runtime.assessments.model_dump_json().encode("utf-8")).hexdigest()
    return (f"{BASE_VERSION}:{Assistant.rules_hash()}:{config_hash[:12]}:{briefs_hash[:12]}:"
            f"{runtime.corpus.corpus_version[:12]}")  # fmt: skip


def configure(runtime: Runtime | None) -> None:
    """Install (or, with None, remove) what the adapter answers with."""
    _RUNTIME[0] = runtime
    CourseTA.version = version_for(runtime) if runtime is not None else BASE_VERSION


def current() -> Runtime | None:
    return _RUNTIME[0]


def unit_indices(answer: Answer, corpus: Corpus) -> list[int]:
    """The answer's citations as 1-based unit indices, in order, without repeats. A citation
    that does not name the unit at its index is a bug, never a quiet drop."""
    out: list[int] = []
    for c in answer.citations:
        if not 1 <= c.unit_index <= len(corpus.units):
            raise SystemConfigError(f"citation unit_index {c.unit_index} is not in the corpus")
        unit = corpus.units[c.unit_index - 1]
        if unit.file_id != c.file_id:
            raise SystemConfigError(f"citation {c.unit_index} names another file than the unit")
        if c.unit_index not in out:
            out.append(c.unit_index)
    return out


def _check_case(rt: Runtime, case: Case) -> None:
    if case.meta.get("corpus_version") != rt.corpus.corpus_version:
        raise SystemConfigError(f"case {case.id} was built for another corpus version")
    if tuple(case.context) != tuple(u.text for u in rt.corpus.units):
        raise SystemConfigError(f"case {case.id}: the context is not the corpus units in order")


def _prior_turns(case: Case) -> tuple[tuple[str, str], ...]:
    raw: Any = case.meta.get("prior_turns") or ()
    out = []
    for turn in raw:
        if not (isinstance(turn, list | tuple) and len(turn) == 2
                and all(isinstance(x, str) for x in turn)):  # fmt: skip
            raise SystemConfigError(f"case {case.id}: prior_turns must be [question, reply] pairs")
        out.append((turn[0], turn[1]))
    return tuple(out)


def run_case(rt: Runtime, case: Case) -> tuple[Answer, str]:
    """One case through `ask()`: (the Answer, its request id). Synchronous."""
    _check_case(rt, case)
    session = case.meta.get("session_scope")
    if session is not None and not isinstance(session, str):
        raise SystemConfigError(f"case {case.id}: session_scope must be a string")
    rid = request_id_for(case.id)
    pid = pid_for(case.id)
    now = rt.now()
    store = Store(":memory:")
    try:
        turns = _prior_turns(case)
        cid = None
        if turns:
            fx = golden.Fixture(format=golden.FORMAT, note="course-ta", now=now, config=rt.config,
                                corpus=rt.corpus, assessments=rt.assessments, cases=())  # fmt: skip
            seeded = golden.Case(id=case.id, source=case.id, scenario="course-ta",
                                 request_id=rid, learner_pid=pid, session=session,
                                 question=case.question, prior_turns=turns,
                                 expected_status="answered", recordings=())  # fmt: skip
            cid = golden.seed(store, fx, seeded)
        assistant = Assistant(rt.config, rt.corpus, rt.assessments, store, rt.source,
                              now=lambda: now, timer=rt.timer,
                              new_id=golden.ids_for(rid))  # fmt: skip
        turn = assistant.turn(case.question, pid, session, cid, request_id=rid)
        return turn.answer, rid
    finally:
        store.close()


def completion_for(rt: Runtime, answer: Answer, request_id: str,
                   request: CompletionRequest) -> RawCompletion:  # fmt: skip
    if answer.status == "error":
        raise ConnectionError("ask() returned status error (a model call failed)")
    body = {"label": answer.status, "answer": answer.text,
            "citations": unit_indices(answer, rt.corpus)}  # fmt: skip
    return RawCompletion(
        model=request.model,
        model_snapshot=answer.model_snapshot,
        params={
            "rules_hash": answer.rules_hash,
            "corpus_version": answer.corpus_version,
            "answer_model": rt.config.answer_model,
            "check_model": rt.config.check_model,
            "grounding_bar": repr(rt.config.grounding_bar),
        },
        raw_text=json.dumps(body, ensure_ascii=False),
        tokens_in=0,  # ask() reports cost and latency per request, not tokens
        tokens_out=0,
        latency_ms=answer.latency_ms,
        cost_usd=answer.cost_usd,
        request_id=request_id,
    )


@register_system(SYSTEM_NAME)
class CourseTA(SystemAdapter):
    """The whole `ask()` pipeline as the system under test (D-10)."""

    version: ClassVar[str] = BASE_VERSION

    async def answer(self, case: Case, request: CompletionRequest) -> RawCompletion:
        rt = current()
        if rt is None:
            raise SystemConfigError(
                "course_ta is not configured: call course_assistant.eval.system.configure()"
            )
        answer, rid = await asyncio.to_thread(run_case, rt, case)
        return completion_for(rt, answer, rid, request)

    def estimate_cost(self, case: Case, request: CompletionRequest) -> float:
        return PER_CASE_USD
