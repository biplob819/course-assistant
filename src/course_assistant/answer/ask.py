"""`ask()`: one learner turn through the answer graph (A2.5, Tech Spec §2.2).

Pause check, conversation window, route and answer in parallel (D-9), grounding and integrity,
Finalize, the post-answer leak scan and the hint step, with every outside call going through a
`Source` keyed by request id and step, so a recorded request replays exactly (§10).

Nothing fails silently. An `LLMError` on a call the outcome needs gives status `error` with the
NFR8 fixed message; an error on a discarded call is traced; any other exception propagates
after the turn's error answer is stored, so no question is left without an answer.
No question, window, draft or hint text ever reaches a trace (D-14): trace records are built
from ids, statuses, scores and numbers and pass through `strip_text` before the sink.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, ClassVar, Literal, TypeVar, get_args

from course_assistant.answer.answer import AnswerDraft, AnswerStep
from course_assistant.answer.ground import GroundReport, GroundStep
from course_assistant.answer.hint import Hint, HintStep
from course_assistant.answer.integrity import IntegrityResult, IntegrityStep
from course_assistant.answer.leak import Finding, scan
from course_assistant.answer.replay import Live, Responder, Source
from course_assistant.answer.route import RouteStep
from course_assistant.ingest.corpus import Corpus
from course_assistant.llm import (
    FileCitation,
    LLMError,
    ResponsesRequest,
    ResponsesResult,
    TraceSink,
    strip_text,
)
from course_assistant.schemas._base import Model, NonNegFloat, NonNegInt
from course_assistant.schemas.config import AppConfig
from course_assistant.schemas.corpus import Assessment, AssessmentList
from course_assistant.schemas.request import (
    Answer,
    Category,
    Citation,
    Conversation,
    GroundingResult,
    Question,
    SearchHit,
    Status,
)
from course_assistant.store.db import Store

CATEGORIES: tuple[str, ...] = get_args(Category)
STATUSES: tuple[str, ...] = get_args(Status)
Label = Literal["route", "answer", "ground", "integrity", "hint"]
ErrorKind = Literal["transport", "parse", "config"]
T = TypeVar("T")

_PID = re.compile(r"^[0-9a-f]{16}$")
_REQUEST_ID = re.compile(r"^r-[0-9a-f]{16}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
# citation markers a reply may carry in its text: ASCII [n] lists and 【…】 provider brackets
_MARKERS = re.compile(r"\s*(?:\[[0-9]+(?:\s*[,;]\s*[0-9]+)*\]|【[^】]*】)")


class StepTiming(Model):
    """One step of one request, for the trace and the smoke run's per-step percentiles."""

    step: Label
    calls: NonNegInt
    latency_ms: NonNegInt
    cost_usd: NonNegFloat
    error: ErrorKind | None = None


class Turn(Model):
    """What `ask()` decided, plus what the caller needs that the Answer does not carry."""

    answer: Answer
    request_id: str
    conversation_id: str | None  # None when paused: nothing was stored
    paused: bool
    discarded: bool  # the answer call's draft was thrown away (route not course, D-9)
    handover: bool  # admin or personal: B2.1 queues it
    injection: bool  # route injection: flagged for review, no handover
    leak: tuple[Finding, ...]  # what the leak scan found (empty when clean)
    draft: AnswerDraft | None  # a course draft not shown (not covered or leaked): for the queue
    steps: tuple[StepTiming, ...]


def decide(route: str | None, ground_passed: bool | None, integrity_passed: bool | None,
           api_error: bool, leaked: bool) -> Status:  # fmt: skip
    """The Finalize table (§2.2), total over every combination. `route` is None when the route
    call itself failed. `ground_passed` and `integrity_passed` are None when not run; they
    only matter for `course`. `api_error` means a call the outcome needs failed; `leaked`
    means the text to be shown failed the leak scan."""
    if api_error or route is None:
        return "error"
    if route not in CATEGORIES:
        raise ValueError(f"unknown route {route!r}")
    if route in ("admin", "personal"):
        return "handed_over"
    if route in ("off_topic", "injection"):
        return "not_covered"
    if route == "graded_work":
        return "not_covered" if leaked else "hint_only"
    # course
    if ground_passed is not True:
        return "not_covered"
    if integrity_passed is not True:
        return "not_covered" if leaked else "hint_only"
    return "not_covered" if leaked else "answered"


def strip_markers(text: str) -> str:
    """A reply as plain text for the window: citation markers removed (§2 window rules)."""
    return _MARKERS.sub("", text).strip()


def build_window(turns: Sequence[tuple[str, str]], max_turns: int,
                 max_tokens: int, chars_per_token: int = 4) -> list[tuple[str, str]]:  # fmt: skip
    """The last `max_turns` whole turns, then whole turns dropped from the oldest end until the
    window is at most `max_tokens` (characters / `chars_per_token`). A turn is never cut."""
    kept = list(turns[-max_turns:]) if max_turns > 0 else []
    budget = max_tokens * chars_per_token
    while kept and sum(len(q) + len(a) for q, a in kept) > budget:
        kept.pop(0)
    return kept


class _Calls:
    """Every call of one request, by step label, safe across the request's threads."""

    def __init__(self, source: Source, request_id: str) -> None:
        self.source = source
        self.request_id = request_id
        self._lock = threading.Lock()
        self.results: dict[str, list[ResponsesResult]] = {}
        self.errors: dict[str, ErrorKind] = {}

    def bind(self, label: Label) -> Responder:
        def call(request: ResponsesRequest) -> ResponsesResult:
            try:
                result = self.source.call(self.request_id, request)
            except LLMError as exc:
                with self._lock:
                    self.errors[label] = exc.kind
                raise
            _money(result.cost_usd)
            with self._lock:
                self.results.setdefault(label, []).append(result)
            return result

        return call

    def latency(self, label: str) -> int:
        return sum(r.latency_ms for r in self.results.get(label, ()))

    def cost(self, label: str | None = None) -> float:
        labels = [label] if label else list(self.results)
        # fsum: exactly rounded, so the total is the same on every Python (3.12 changed sum)
        return _money(math.fsum(r.cost_usd for x in labels for r in self.results.get(x, ())))

    def snapshot(self, label: str) -> str | None:
        rs = self.results.get(label)
        return rs[-1].model_snapshot if rs else None

    def timings(self) -> tuple[StepTiming, ...]:
        out = []
        for label in get_args(Label):
            rs = self.results.get(label, [])
            err = self.errors.get(label)
            if rs or err:
                out.append(StepTiming(step=label, calls=len(rs) + (err is not None),
                                      latency_ms=self.latency(label), cost_usd=self.cost(label),
                                      error=err))  # fmt: skip
        return tuple(out)


def _money(value: float) -> float:
    """Every money value is finite and non-negative before it is recorded (L-6)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("cost is not a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError("cost must be finite and non-negative")
    return float(value)


def _outcome(future: Future[T]) -> tuple[T | None, LLMError | None]:
    """A step's result, or its `LLMError`; any other exception propagates."""
    try:
        return future.result(), None
    except LLMError as exc:
        return None, exc


class Assistant:
    """The answer graph for one cohort. Fixed texts are class constants hashed into
    `rules_hash` with every step's version, so a change to any is a new release (§4)."""

    PAUSED_MESSAGE: ClassVar[str] = (
        "The course assistant is paused by the teaching team right now. Please try again "
        "later, or ask the teaching team directly."
    )
    ERROR_MESSAGE: ClassVar[str] = (
        "Sorry, the course assistant could not answer just now because of a technical "
        "problem. Please try again in a few minutes. If it keeps happening, contact "
        "programme support through the Help page of your learning platform."
    )
    NOT_COVERED: ClassVar[str] = (
        "I could not find this in the course materials, so I won't guess. You can ask the "
        "teaching team instead."
    )
    OFF_TOPIC: ClassVar[str] = "I can only help with questions about this course and its materials."
    INJECTION: ClassVar[str] = (
        "I can't help with that request. I can answer questions about the course materials."
    )
    HANDED_OVER: ClassVar[str] = "Sent to the teaching team. They will reply here, in your history."
    GENERIC_HINT: ClassVar[str] = (
        "I can't write graded work for you. Revisit the sessions this task draws on and work "
        "through the ideas there yourself."
    )
    IDLE: ClassVar[dt.timedelta] = dt.timedelta(hours=2)
    MAX_TURNS: ClassVar[int] = IntegrityStep.MAX_TURNS  # D-40: a conversation holds 20 turns

    @classmethod
    def fixed_texts(cls) -> dict[str, str]:
        return {"paused": cls.PAUSED_MESSAGE, "error": cls.ERROR_MESSAGE,
                "not_covered": cls.NOT_COVERED, "off_topic": cls.OFF_TOPIC,
                "injection": cls.INJECTION, "handed_over": cls.HANDED_OVER,
                "generic_hint": cls.GENERIC_HINT}  # fmt: skip

    @classmethod
    def rules_hash(cls) -> str:
        blob = json.dumps({
            "route": RouteStep.version(), "answer": AnswerStep.rules_version(),
            "ground": GroundStep.version(), "integrity": IntegrityStep.version(),
            "hint": HintStep.hint_version(), "fixed": cls.fixed_texts(),
            "idle_s": cls.IDLE.total_seconds(), "max_turns": cls.MAX_TURNS,
        }, sort_keys=True)  # fmt: skip
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def __init__(self, config: AppConfig, corpus: Corpus, assessments: AssessmentList,
                 store: Store, responder: Responder | Source, *, sink: TraceSink | None = None,
                 now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
                 timer: Callable[[], float] = time.monotonic,
                 new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:16]) -> None:  # fmt: skip
        if not isinstance(assessments, AssessmentList):
            raise TypeError("assessments must be an AssessmentList")
        self.config = config
        self.corpus = corpus
        self.assessments = assessments
        self.store = store
        self.source: Source = responder if hasattr(responder, "call") else Live(responder)  # type: ignore[arg-type]
        self.sink = sink
        self.now = now
        self.timer = timer
        self.new_id = new_id
        self.sessions = {u.session for u in corpus.units}
        self.briefs = {a.assessment_id: a for a in assessments.assessments}

    # ---------- public ----------

    def ask(self, question: str, learner_pid: str, session: str | None = None,
            conversation_id: str | None = None, *,
            request_id: str | None = None) -> Answer:  # fmt: skip
        return self.turn(question, learner_pid, session, conversation_id,
                         request_id=request_id).answer  # fmt: skip

    def new_conversation(self, learner_pid: str, session: str | None = None) -> Conversation:
        """Close the learner's open conversations and start a new one ("New conversation")."""
        self._check_pid(learner_pid)
        self._check_session(session)
        now = self._now()
        for c in self.store.conversations_for(learner_pid):
            if c.closed_at is None:
                self.store.put_conversation(
                    c.model_copy(update={"closed_at": max(now, c.started_at)})
                )
        return self._start(learner_pid, session, now)

    def turn(self, question: str, learner_pid: str, session: str | None = None,
             conversation_id: str | None = None, *,
             request_id: str | None = None) -> Turn:  # fmt: skip
        self._check_question(question)
        self._check_pid(learner_pid)
        self._check_session(session)
        rid = request_id if request_id is not None else f"r-{self._hex()}"
        if not isinstance(rid, str) or not _REQUEST_ID.fullmatch(rid):
            raise ValueError("request_id must be r- and 16 hex characters")
        qid = f"q-{rid[2:]}"
        start = self.timer()
        now = self._now()
        calls = _Calls(self.source, rid)

        # a delete this learner files while the turn runs must take every line the turn writes,
        # whenever it lands; the turn notices it by the count of their deletion requests (no
        # clock involved) and removes its own lines last (verifier B2.2 passes 12 and 13)
        before = self._deletions_of(learner_pid)
        touched: list[str | None] = [None]
        try:
            if self.store.is_paused(session):  # a read failure counts as paused (N1.5)
                answer = self._fixed(
                    qid, "not_covered", self.PAUSED_MESSAGE, None, now, calls, start
                )
                self._trace(rid, learner_pid, None, qid, None, "pause", now, error=None)
                self._trace_final(rid, learner_pid, None, qid, None, None, answer, now)
                return Turn(answer=answer, request_id=rid, conversation_id=None, paused=True,
                            discarded=False, handover=False, injection=False, leak=(), draft=None,
                            steps=())  # fmt: skip

            if conversation_id is not None:  # after the pause check: a paused app reads nothing
                self._owned(conversation_id, learner_pid, session)  # (verifier A2.5 pass 1)
            if self.store.get_question(qid) is not None:  # never replace an earlier turn
                raise ValueError("request_id already used")
            conv = self._conversation(learner_pid, session, conversation_id, now)
            touched[0] = conv.conversation_id
            earlier, count = self._earlier(learner_pid, conv.conversation_id)
            window = build_window(earlier, self.config.window_turns, self.config.window_tokens,
                                  RouteStep.CHARS_PER_TOKEN)  # fmt: skip
            replies = [a for _, a in earlier]
            turn_index = count + 1
            self.store.put_question(Question(question_id=qid, conversation_id=conv.conversation_id,
                                             turn_index=turn_index, learner_pid=learner_pid,
                                             session_scope=session, text=question,
                                             asked_at=now))  # fmt: skip
            self._trace(rid, learner_pid, conv.conversation_id, qid, turn_index, "window", now,
                        error=None)  # fmt: skip

            try:
                result = self._run(question, session, window, replies, qid, calls)
            except Exception:
                # an unexpected bug still propagates, but the stored question never stands without
                # an answer: it gets the NFR8 error answer first (verifier A2.5 pass 1)
                failed = self._fixed(qid, "error", self.ERROR_MESSAGE, None, now, calls, start)
                self._store_answer(failed, qid)
                raise
            answer = self._answer(qid, result, now, calls, start)
            if not self._store_answer(answer, qid):
                # the learner deleted this turn's question while it ran: nothing more is written
                return Turn(answer=answer, request_id=rid, conversation_id=conv.conversation_id,
                            paused=False, discarded=result["discarded"], handover=False,
                            injection=False, leak=result["leak"], draft=None,
                            steps=calls.timings())  # fmt: skip
            for t in calls.timings():
                self._trace(rid, learner_pid, conv.conversation_id, qid, turn_index, t.step, now,
                            error=t.error, cost=t.cost_usd, latency=t.latency_ms,
                            calls_made=t.calls)  # fmt: skip
            self._trace_final(rid, learner_pid, conv.conversation_id, qid, turn_index,
                              result.get("route"), answer, now)  # fmt: skip
            return Turn(answer=answer, request_id=rid, conversation_id=conv.conversation_id,
                        paused=False, discarded=result["discarded"],
                        handover=answer.status == "handed_over",
                        injection=result.get("route") == "injection" and answer.status != "error",
                        leak=result["leak"], draft=result["kept_draft"],
                        steps=calls.timings())  # fmt: skip
        finally:
            self._settle(learner_pid, touched[0], before)

    def _deletions_of(self, pid: str) -> frozenset[str] | None:
        try:
            return self.store.deletion_keys(pid)
        except Exception:
            return None  # unreadable: the turn cannot tell, so it removes nothing

    def _settle(self, pid: str, conversation_id: str | None, before: frozenset[str] | None) -> None:
        """If the learner filed a delete while this turn ran, what this turn wrote goes too:
        its trace lines (this conversation's; all the learner's for a paused turn, which has
        none), an open copy of each new request for purge's retry, and this conversation's app
        rows if the turn wrote them back. Only the turn's own conversation, never a newer
        one the learner started after the delete. New requests are found by key; each step has
        its own guard so one store error cannot skip the others; the turn never marks trace
        deletion complete (verifier B2.2 passes 12-17). If the store cannot be read at the
        start, nothing is removed."""
        if before is None:
            return
        try:
            new = self.store.deletions_by_key(self.store.deletion_keys(pid) - before)
        except Exception:
            return
        whole = any(d.conversation_id is None for d in new)
        scoped = conversation_id is not None and any(conversation_id in d.covers for d in new)
        if not (whole or scoped):
            return
        remove = getattr(self.sink, "delete_pid", None)
        if callable(remove):
            with contextlib.suppress(Exception):
                remove(pid, None if conversation_id is None else {conversation_id})
        now = self._now()
        for d in new:
            with contextlib.suppress(Exception):
                self.store.reopen_traces(d, at=max(now, d.requested_at))
        if conversation_id is not None:
            with contextlib.suppress(Exception):
                owned = {c.conversation_id for c in self.store.conversations_for(pid)}
                if conversation_id in owned:
                    self.store.delete_history(pid, conversation_id, at=now)

    def _store_answer(self, answer: Answer, qid: str) -> bool:
        """Store the answer; False (not an error) when the question was deleted meanwhile."""
        try:
            self.store.put_answer(answer)
        except KeyError:
            if self.store.get_question(qid) is None:
                return False
            raise
        return True

    # ---------- the graph ----------

    def _run(self, question: str, session: str | None, window: list[tuple[str, str]],
             replies: list[str], qid: str, calls: _Calls) -> dict[str, Any]:  # fmt: skip
        route_step = RouteStep(self.config, calls.bind("route"))
        answer_step = AnswerStep(self.config, self.corpus, calls.bind("answer"))
        out: dict[str, Any] = {"route": None, "route_obj": None, "discarded": False,
                               "leak": (), "kept_draft": None, "ground": None,
                               "integrity": None, "draft": None, "hint": None,
                               "status": "error", "api_error": False}  # fmt: skip

        def answer_call() -> tuple[AnswerDraft, tuple[FileCitation, ...]]:
            res = answer_step.responder(answer_step.request(question, window, session))
            return answer_step.draft(res), res.citations

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ask") as pool:
            f_route = pool.submit(route_step.run, question, window, self.assessments,
                                  question_id=qid)  # fmt: skip
            f_answer = pool.submit(answer_call)
            route, route_err = _outcome(f_route)
            drafted, answer_err = _outcome(f_answer)

        if route_err is not None or route is None:
            out["api_error"] = True
            out["discarded"] = True
            return self._finish(out)
        out["route"], out["route_obj"] = route.category, route
        if route.category != "course":
            out["discarded"] = True  # D-9: never grounded, checked, stored or shown
            if route.category == "graded_work":
                self._hint(out, self.briefs.get(route.assessment_id or ""), replies, calls)
            return self._finish(out)
        if answer_err is not None or drafted is None:
            out["api_error"] = True
            return self._finish(out)

        draft, marks = drafted
        out["draft"] = draft
        ground_step = GroundStep(self.config, self.corpus, calls.bind("ground"))
        integrity_step = IntegrityStep(self.config, calls.bind("integrity"))
        if not self._l1(ground_step, draft, marks, session):
            out["ground"] = ground_step.assess(draft, marks, session)  # L1 fails: no call
            out["kept_draft"] = draft
            return self._finish(out)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="check") as pool:
            f_ground = pool.submit(ground_step.assess, draft, marks, session)
            f_integ = pool.submit(integrity_step.run, draft.text, replies, self.assessments)
            report, ground_err = _outcome(f_ground)
            integ, integ_err = _outcome(f_integ)
        if ground_err is not None or integ_err is not None or report is None or integ is None:
            out["api_error"] = True
            return self._finish(out)
        out["ground"], out["integrity"] = report, integ
        if not report.result.passed:
            out["kept_draft"] = draft
            return self._finish(out)
        if not integ.passed:
            brief = self.briefs.get(integ.assessment_id or route.assessment_id or "")
            self._hint(out, brief, replies, calls)
            return self._finish(out)
        out["leak"] = scan(draft.text)
        if out["leak"]:
            out["kept_draft"] = draft
        return self._finish(out)

    def _hint(self, out: dict[str, Any], brief: Assessment | None, replies: list[str],
              calls: _Calls) -> None:  # fmt: skip
        if brief is None:
            return  # no known brief: the generic fixed hint, no call
        bound = calls.bind("hint")
        step = HintStep(self.config, self.corpus, bound, IntegrityStep(self.config, bound))
        try:
            hint = step.run(brief, replies, self.assessments)
        except LLMError:
            out["api_error"] = True
            return
        out["hint"] = hint
        if not hint.fixed:
            out["leak"] = scan(hint.text)

    def _finish(self, out: dict[str, Any]) -> dict[str, Any]:
        ground: GroundReport | None = out["ground"]
        integ: IntegrityResult | None = out["integrity"]
        out["status"] = decide(out["route"], None if ground is None else ground.result.passed,
                               None if integ is None else integ.passed, out["api_error"],
                               bool(out["leak"]))  # fmt: skip
        return out

    @staticmethod
    def _l1(ground: GroundStep, draft: AnswerDraft, marks: tuple[FileCitation, ...],
            session: str | None) -> bool:  # fmt: skip
        """A2.3's deterministic layer, from its public checks (§5 point 2)."""
        if draft.refusal or not draft.text.strip():
            return False
        if not ground.citations_resolve(draft, marks, session):
            return False
        return ground.top_score(draft, session) >= ground.config.grounding_bar

    # ---------- the Answer ----------

    def _answer(self, qid: str, r: dict[str, Any], now: dt.datetime, calls: _Calls,
                start: float) -> Answer:  # fmt: skip
        status: Status = r["status"]
        ground: GroundReport | None = r["ground"]
        integ: IntegrityResult | None = r["integrity"]
        grounding: GroundingResult | None = None if ground is None else ground.result
        integrity_passed = None if integ is None else integ.passed
        if status == "error":
            return self._fixed(qid, "error", self.ERROR_MESSAGE, None, now, calls, start)
        route = r["route"]
        if status == "answered":
            draft: AnswerDraft = r["draft"]
            return self._make(qid, status, draft.text, draft.citations, draft.hits, grounding,
                              integrity_passed, calls.snapshot("answer"),
                              grounding.top_score if grounding else 0.0, now, calls,
                              start)  # fmt: skip
        if status == "hint_only":
            hint: Hint | None = r["hint"]
            if hint is None:
                text, cites, hits, top = self.GENERIC_HINT, (), (), 0.0
            else:
                text, cites, hits = hint.text, hint.citations, hint.hits
                top = max((h.score for h in hits), default=0.0)
            snap = calls.snapshot("hint") or calls.snapshot("route")
            return self._make(qid, status, text, cites, hits, grounding, integrity_passed, snap,
                              top, now, calls, start)  # fmt: skip
        if status == "handed_over":
            text = self.HANDED_OVER
        elif route == "off_topic":
            text = self.OFF_TOPIC
        elif route == "injection":
            text = self.INJECTION
        else:
            text = self.NOT_COVERED  # course draft not grounded, or a leak
        if route == "course":
            snap = calls.snapshot("answer")
            top = grounding.top_score if grounding else 0.0
        else:
            snap = calls.snapshot("hint") if r["leak"] else calls.snapshot("route")
            top = 0.0
        return self._make(qid, status, text, (), (), grounding, integrity_passed, snap, top,
                          now, calls, start)  # fmt: skip

    def _fixed(self, qid: str, status: Status, text: str, snapshot: str | None,
               now: dt.datetime, calls: _Calls, start: float) -> Answer:  # fmt: skip
        return self._make(qid, status, text, (), (), None, None, snapshot, 0.0, now, calls, start)

    def _make(self, qid: str, status: Status, text: str, citations: tuple[Citation, ...],
              hits: tuple[SearchHit, ...], grounding: GroundingResult | None,
              integrity_passed: bool | None, snapshot: str | None, top: float,
              now: dt.datetime, calls: _Calls, start: float) -> Answer:  # fmt: skip
        return Answer(question_id=qid, status=status, text=text, citations=citations, hits=hits,
                      grounding=grounding, integrity_passed=integrity_passed,
                      model_snapshot=snapshot or self.config.answer_model,
                      rules_hash=self.rules_hash(), corpus_version=self.corpus.corpus_version,
                      top_search_score=top, cost_usd=calls.cost(),
                      latency_ms=self._latency(calls, start), created_at=now)  # fmt: skip

    def _latency(self, calls: _Calls, start: float) -> int:
        """The larger of the measured wall time and the critical path of the step latencies:
        route ‖ answer, then L2 ‖ integrity, then the hint (§2.6)."""
        wall = max(0.0, self.timer() - start) * 1000
        path = (max(calls.latency("route"), calls.latency("answer"))
                + max(calls.latency("ground"), calls.latency("integrity"))
                + calls.latency("hint"))  # fmt: skip
        return int(max(wall, path))

    # ---------- conversation and window ----------

    def _conversation(self, pid: str, session: str | None, conversation_id: str | None,
                      now: dt.datetime) -> Conversation:  # fmt: skip
        if conversation_id is None:
            return self._start(pid, session, now)
        conv = self._owned(conversation_id, pid, session)
        questions = self.store.questions_for(pid, conversation_id)
        last = max([conv.started_at, *(q.asked_at for q in questions)])
        if conv.closed_at is not None:
            return self._start(pid, session, now)
        if now - last >= self.IDLE:
            self.store.put_conversation(conv.model_copy(update={"closed_at": last + self.IDLE}))
            return self._start(pid, session, now)
        if len(questions) >= self.MAX_TURNS:
            self.store.put_conversation(conv.model_copy(update={"closed_at": max(now, last)}))
            return self._start(pid, session, now)
        return conv

    def _start(self, pid: str, session: str | None, now: dt.datetime) -> Conversation:
        conv = Conversation(conversation_id=f"c-{self._hex()}", learner_pid=pid,
                            session_scope=session, started_at=now)  # fmt: skip
        self.store.put_conversation(conv)
        return conv

    def _owned(self, conversation_id: str, pid: str, session: str | None) -> Conversation:
        conv = self.store.get_conversation(conversation_id)
        if conv is None or conv.learner_pid != pid:
            raise ValueError("no such conversation for this learner")  # never another's (B8)
        if conv.session_scope != session:
            raise ValueError("the conversation is scoped to another session")
        return conv

    def _earlier(self, pid: str, conversation_id: str) -> tuple[list[tuple[str, str]], int]:
        """Every earlier turn of this learner's conversation with a shown reply, oldest first,
        replies as plain text; and how many questions the conversation holds."""
        questions = sorted(self.store.questions_for(pid, conversation_id),
                           key=lambda q: q.turn_index)  # fmt: skip
        turns: list[tuple[str, str]] = []
        for q in questions:
            if q.learner_pid != pid or q.conversation_id != conversation_id:
                raise ValueError("stored question is not this learner's conversation")
            a = self.store.get_answer(q.question_id)
            if a is None or a.status == "error":
                continue
            reply = strip_markers(a.text)
            if reply:
                turns.append((q.text, reply))
        return turns, len(questions)

    # ---------- checks, ids, trace ----------

    def _check_question(self, question: object) -> None:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        if len(question) > RouteStep.MAX_QUESTION_CHARS:
            raise ValueError(f"question is longer than {RouteStep.MAX_QUESTION_CHARS} characters")

    @staticmethod
    def _check_pid(pid: object) -> None:
        if not isinstance(pid, str) or not _PID.fullmatch(pid):
            raise ValueError("learner_pid must be 16 hex characters (D-13)")

    def _check_session(self, session: object) -> None:
        if session is not None and session not in self.sessions:
            raise ValueError("session is not in the live corpus")

    def _hex(self) -> str:
        value = self.new_id()
        if not isinstance(value, str) or not _HEX16.fullmatch(value):
            raise ValueError("new_id must return 16 hex characters")
        return value

    def _now(self) -> dt.datetime:
        value = self.now()
        if value.tzinfo is None:
            raise ValueError("now() must be timezone-aware")
        return value.astimezone(dt.UTC)

    def _emit(self, record: dict[str, Any]) -> None:
        if self.sink is not None:
            self.sink.emit(strip_text(record))  # stripped here, whatever the sink (L-28)

    def _trace(self, rid: str, pid: str, cid: str | None, qid: str, turn_index: int | None,
               step: str, now: dt.datetime, *, error: str | None, cost: float = 0.0,
               latency: int = 0, calls_made: int = 0) -> None:  # fmt: skip
        self._emit({"request_id": rid, "step": step, "learner_pid": pid, "conversation_id": cid,
                    "question_id": qid, "turn_index": turn_index, "error": error,
                    "cost_usd": cost, "latency_ms": latency, "attempts": calls_made,
                    "at": now.isoformat()})  # fmt: skip

    def _trace_final(self, rid: str, pid: str, cid: str | None, qid: str,
                     turn_index: int | None, route: str | None, answer: Answer,
                     now: dt.datetime) -> None:  # fmt: skip
        g = answer.grounding
        self._emit({"request_id": rid, "step": "finalize", "learner_pid": pid,
                    "conversation_id": cid, "question_id": qid, "turn_index": turn_index,
                    "route": route, "status": answer.status, "rules_hash": answer.rules_hash,
                    "model_snapshot": answer.model_snapshot,
                    "top_score": answer.top_search_score, "n_citations": len(answer.citations),
                    "n_hits": len(answer.hits),
                    "grounding_passed": None if g is None else g.passed,
                    "integrity_passed": answer.integrity_passed, "cost_usd": answer.cost_usd,
                    "latency_ms": answer.latency_ms, "at": now.isoformat()})  # fmt: skip
