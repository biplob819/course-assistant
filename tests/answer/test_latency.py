"""E2.5 (D-44) latency candidates behind config, offline with fakes.

(a) `reasoning_effort` and (c) `answer_model` were config already; this file covers what E2.5
adds: `early_finalize` (a non-course turn does not wait for the discarded answer call) and
`hint_model` (hints on another model than answers). Neither may change a status or let a
discarded draft reach the Answer.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from pydantic import ValidationError

from course_assistant.answer import responder
from course_assistant.answer.answer import AnswerStep
from course_assistant.answer.ask import CATEGORIES, Assistant
from course_assistant.llm import ResponsesRequest, ResponsesResult
from course_assistant.schemas.config import AppConfig
from course_assistant.store.db import Store
from tests.answer import scripted as s
from tests.answer.test_ask import (
    ASSESSMENTS,
    CONFIG,
    CORPUS,
    PID,
    PRICES,
    QUESTION,
    S01,
    Clock,
    happy,
    ids,
)

EARLY = CONFIG.model_copy(update={"early_finalize": True})


def build(config: AppConfig, client: s.StepClient, source: Any = None) -> Assistant:
    rsp = responder(client=client, prices=PRICES, sleep=lambda _: None)
    return Assistant(config, CORPUS, ASSESSMENTS, Store(":memory:"), source or rsp,
                     now=Clock(), timer=lambda: 0.0, new_id=ids())  # fmt: skip


class Blocking(s.StepClient):
    """The answer call waits until released, so a turn that waits for it cannot finish."""

    def __init__(self, **queues: list[Any]) -> None:
        super().__init__(**queues)
        self.release = threading.Event()
        real = self.responses.create

        def create(**kw: Any) -> Any:
            if kw["instructions"] == AnswerStep.ANSWER_RULES:
                assert self.release.wait(10), "the answer call was never released"
            return real(**kw)

        self.responses.create = create  # type: ignore[method-assign]


class Latency:
    """A Source that sets each step's latency, so the critical path can be checked exactly."""

    def __init__(self, inner: Any, ms: dict[str, int]) -> None:
        self.inner = inner
        self.ms = ms

    def call(self, request_id: str, request: ResponsesRequest) -> ResponsesResult:
        result = self.inner(request)
        return result.model_copy(update={"latency_ms": self.ms[request.purpose]})


def run_in_thread(fn: Any) -> tuple[threading.Thread, list[Any]]:
    """Run fn on its own thread; fn builds the Assistant there (sqlite stays on one thread)."""
    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    t.start()
    return t, out


def test_early_finalize_is_off_by_default():
    assert CONFIG.early_finalize is False and CONFIG.hint_model is None


def test_with_early_finalize_a_non_course_turn_does_not_wait_for_the_discarded_answer():
    client = Blocking(route=[s.route("admin")], answer=[s.cited("SECRET DRAFT here.", S01)])
    t, out = run_in_thread(
        lambda: build(EARLY, client).turn("When is the deadline for Assignment 2?", PID)
    )
    t.join(5)
    try:
        assert not t.is_alive(), "the turn waited for the discarded answer call"
        turn = out[0]
        assert turn.answer.status == "handed_over" and turn.discarded
        assert "SECRET DRAFT" not in turn.answer.model_dump_json()
        # the discarded call is left out of the turn's cost and steps, whatever its timing
        assert [x.step for x in turn.steps] == ["route"]
        assert turn.answer.cost_usd == pytest.approx(turn.steps[0].cost_usd)
    finally:
        client.release.set()


def test_without_early_finalize_the_turn_waits_for_the_discarded_answer():
    client = Blocking(route=[s.route("admin")], answer=[s.cited("d", S01)])
    t, _ = run_in_thread(
        lambda: build(CONFIG, client).turn("When is the deadline for Assignment 2?", PID)
    )
    t.join(0.5)
    assert t.is_alive()  # still waiting on the answer call
    client.release.set()
    t.join(5)
    assert not t.is_alive()


def _queues(category: str) -> dict[str, list[Any]]:
    q: dict[str, list[Any]] = {"route": [s.route(category)],
                               "answer": [s.cited("SECRET DRAFT TEXT here.", S01)]}  # fmt: skip
    if category == "graded_work":
        q["route"] = [s.route("graded_work", "x", "s01-a1")]
        q["hint"] = [s.sol("Write it like this.", (), ())]
    if category == "course":
        q = happy()
    return q


@pytest.mark.parametrize("category", CATEGORIES)
def test_early_finalize_gives_the_same_status_and_text_for_every_route(category: str):
    plain = build(CONFIG, s.StepClient(**_queues(category))).turn(QUESTION, PID)
    early = build(EARLY, s.StepClient(**_queues(category))).turn(QUESTION, PID)
    assert early.answer.status == plain.answer.status
    assert early.answer.text == plain.answer.text
    assert early.answer.citations == plain.answer.citations
    if category != "course":
        assert "SECRET DRAFT" not in early.answer.model_dump_json()
        assert early.draft is None and early.discarded


def test_early_finalize_still_waits_for_and_checks_a_course_answer():
    client = s.StepClient(**happy())
    turn = build(EARLY, client).turn(QUESTION, PID)
    assert turn.answer.status == "answered"
    assert {"route", "answer", "support", "integrity"} <= set(client.steps())
    assert {x.step for x in turn.steps} >= {"route", "answer", "ground", "integrity"}


def test_early_finalize_on_a_failed_route_gives_the_error_answer_without_waiting():
    client = Blocking(route=s.down(), answer=[s.cited("d", S01)])
    t, out = run_in_thread(lambda: build(EARLY, client).turn(QUESTION, PID))
    t.join(5)
    try:
        assert not t.is_alive()
        assert out[0].answer.status == "error" and out[0].answer.text == Assistant.ERROR_MESSAGE
    finally:
        client.release.set()


def test_early_finalize_latency_is_route_then_hint_and_plain_is_the_slower_of_route_and_answer():
    ms = {"route": 1500, "answer": 7000, "hint": 3000, "integrity": 1000, "support": 1000}

    def turn(config: AppConfig) -> int:
        client = s.StepClient(route=[s.route("graded_work", "x", "s01-a1")],
                              answer=[s.cited("d", S01)],
                              hint=[s.sol("Revisit session one.", ((S01, 20),), ((S01, 0.9),))],
                              integrity=[s.integrity("clear")])  # fmt: skip
        rsp = responder(client=client, prices=PRICES, sleep=lambda _: None)
        return build(config, client, Latency(rsp, ms)).turn(QUESTION, PID).answer.latency_ms

    hint_path = ms["hint"] + ms["integrity"]  # the hint step checks its own text
    assert turn(CONFIG) == ms["answer"] + hint_path
    assert turn(EARLY) == ms["route"] + hint_path


def test_hint_model_writes_hints_and_answers_stay_on_the_answer_model():
    config = CONFIG.model_copy(update={"hint_model": s.LUNA})
    client = s.StepClient(route=[s.route("graded_work", "x", "s01-a1")],
                          answer=[s.cited("d", S01)],
                          hint=[s.sol("Write it like this.", (), ())])  # fmt: skip
    build(config, client).turn("Write my product brief for Assignment 1.", PID)
    models = {step: kw["model"] for step, kw in client.calls}
    assert models["hint"] == s.LUNA and models["answer"] == s.SOL


def test_hint_model_needs_its_effort_named_like_every_model():
    with pytest.raises(ValidationError, match="reasoning_effort"):
        AppConfig(schema_version="1.0", cohort="c", vector_store_id="v", answer_model="m1",
                  check_model="m2", hint_model="m3", reasoning_effort={"m1": "low", "m2": "low"},
                  grounding_bar=0.5, monthly_budget_usd=150.0)  # fmt: skip
    ok = AppConfig(schema_version="1.0", cohort="c", vector_store_id="v", answer_model="m1",
                   check_model="m2", hint_model="m3",
                   reasoning_effort={"m1": "low", "m2": "low", "m3": "minimal"},
                   grounding_bar=0.5, monthly_budget_usd=150.0)  # fmt: skip
    assert ok.hint_model == "m3"


def test_a_config_without_the_e25_settings_dumps_and_hashes_as_before():
    dumped = CONFIG.model_dump(mode="json")
    assert "hint_model" not in dumped and "early_finalize" not in dumped
    both = CONFIG.model_copy(update={"hint_model": s.LUNA, "early_finalize": True})
    again = AppConfig.model_validate_json(both.model_dump_json())
    assert again.hint_model == s.LUNA and again.early_finalize is True


def test_latency_smoke_runs_only_the_thirty_questions_and_stops_over_its_cap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import smoke_live

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-key")  # never used: the cap stops it first
    args = ["--config", "nope.yaml", "--yes", "--latency-only", "--label", "sol-low",
            "--max-cost", "0.01"]  # fmt: skip
    assert smoke_live.main(args) == 3
    assert "estimate: 30 questions" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        smoke_live.main(["--config", "nope.yaml", "--label", "Bad Label!"])


def test_public_all_runs_every_public_case_and_counts_statuses_by_rule(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import smoke_live

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-key")  # never used: the cap stops it first
    n = len(smoke_live.public_cases())
    assert smoke_live.main(["--config", "nope.yaml", "--yes", "--public-all",
                            "--max-cost", "0.01"]) == 3  # fmt: skip
    assert f"estimate: {n} questions" in capsys.readouterr().out
    rows = [{"rule": "B1", "status": "answered", "expected": "answered"},
            {"rule": "B1", "status": "error", "expected": "answered"},
            {"rule": "B3", "status": "hint_only", "expected": "hint_only"}]  # fmt: skip
    assert smoke_live.by_rule(rows) == {"B1": {"n": 2, "as_expected": 1, "errors": 1},
                                        "B3": {"n": 1, "as_expected": 1, "errors": 0}}  # fmt: skip
