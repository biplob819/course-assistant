"""Replay injection (A2.5, Tech Spec §2.1 and §10): every outside call of a request is keyed by
the request id, the step (the request's purpose) and the call number within that step, so a
recorded request can run through the whole `ask()` graph with no API call.

`Recorder` wraps a live (or fake) responder and keeps what each call returned or raised;
`Replay` gives those back. A replayed request must be the same request that was recorded (its
sha256 is kept), and a missing, changed or unused recording raises `ReplayError`, which is not
an `LLMError`, so a broken replay can never pass for the status `error`.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable
from typing import Literal, Protocol

from pydantic import model_validator

from course_assistant.llm import LLMError, ResponsesRequest, ResponsesResult
from course_assistant.schemas._base import Model, NonNegInt, PosInt, Sha256

Responder = Callable[[ResponsesRequest], ResponsesResult]
Step = Literal["answer", "route", "support", "integrity", "hint"]
ErrorKind = Literal["transport", "parse", "config"]
STEPS: tuple[str, ...] = ("answer", "route", "support", "integrity", "hint")


class ReplayError(Exception):
    """A replay could not be served exactly as recorded (missing, changed or unused)."""


class Source(Protocol):
    """Where a request's outside calls go: live, recorded or replayed."""

    def call(self, request_id: str, request: ResponsesRequest) -> ResponsesResult: ...


def request_hash(request: ResponsesRequest) -> str:
    """sha256 of the whole request (instructions, input, tools, filters, schema)."""
    return hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()


class Recording(Model):
    """One call: what it returned, or the kind of error it raised (never the error text)."""

    request_id: str
    step: Step
    n: NonNegInt
    request_hash: Sha256
    result: ResponsesResult | None = None
    error_kind: ErrorKind | None = None
    error_attempts: PosInt | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> Recording:
        if (self.result is None) == (self.error_kind is None):
            raise ValueError("a recording holds a result or an error kind, not both")
        if (self.error_kind is None) != (self.error_attempts is None):
            raise ValueError("an error recording names its attempts")
        return self

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.request_id, self.step, self.n)


class _Counter:
    """Call numbers per (request id, step), safe across the request's threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[tuple[str, str], int] = {}

    def take(self, request_id: str, step: str) -> int:
        with self._lock:
            n = self._next.get((request_id, step), 0)
            self._next[(request_id, step)] = n + 1
            return n


class Live:
    """A plain responder as a source."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder

    def call(self, request_id: str, request: ResponsesRequest) -> ResponsesResult:
        return self.responder(request)


class Recorder:
    """Pass every call to `responder` and keep a `Recording` of each."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self._counter = _Counter()
        self._lock = threading.Lock()
        self.recordings: list[Recording] = []

    def call(self, request_id: str, request: ResponsesRequest) -> ResponsesResult:
        n = self._counter.take(request_id, request.purpose)
        digest = request_hash(request)
        try:
            result = self.responder(request)
        except LLMError as exc:
            self._keep(Recording(request_id=request_id, step=_step(request), n=n,
                                 request_hash=digest, error_kind=exc.kind,
                                 error_attempts=max(exc.attempts, 1)))  # fmt: skip
            raise
        self._keep(Recording(request_id=request_id, step=_step(request), n=n,
                             request_hash=digest, result=result))  # fmt: skip
        return result

    def _keep(self, recording: Recording) -> None:
        with self._lock:
            self.recordings.append(recording)

    def for_request(self, request_id: str) -> list[Recording]:
        return sorted((r for r in self.recordings if r.request_id == request_id),
                      key=lambda r: (r.step, r.n))  # fmt: skip


class Replay:
    """Serve recorded calls by (request id, step, call number)."""

    def __init__(self, recordings: Iterable[Recording]) -> None:
        self._by_key: dict[tuple[str, str, int], Recording] = {}
        for r in recordings:
            if r.key in self._by_key:
                raise ReplayError(f"two recordings for {r.key}")
            self._by_key[r.key] = r
        self._counter = _Counter()
        self._lock = threading.Lock()
        self._used: set[tuple[str, str, int]] = set()

    def call(self, request_id: str, request: ResponsesRequest) -> ResponsesResult:
        step = _step(request)
        key = (request_id, step, self._counter.take(request_id, step))
        recording = self._by_key.get(key)
        if recording is None:
            raise ReplayError(f"no recording for {key}")
        if recording.request_hash != request_hash(request):
            raise ReplayError(f"request {key} differs from the one recorded")
        with self._lock:
            self._used.add(key)
        if recording.error_kind is not None:
            raise LLMError(recording.error_kind, "replayed error", recording.error_attempts or 1)
        assert recording.result is not None
        return recording.result

    def unused(self, request_id: str | None = None) -> list[tuple[str, str, int]]:
        with self._lock:
            return sorted(
                k
                for k in self._by_key
                if k not in self._used and (request_id is None or k[0] == request_id)
            )

    def check_used(self, request_id: str | None = None) -> None:
        left = self.unused(request_id)
        if left:
            raise ReplayError(f"{len(left)} recording(s) never used, first {left[0]}")


def _step(request: ResponsesRequest) -> Step:
    if request.purpose not in STEPS:
        raise ReplayError(f"purpose {request.purpose} is not a step of ask()")
    return request.purpose  # type: ignore[return-value]
