"""E2.5 (OWNER D-63): hint text cached per assessment item, offline with fakes.

The cache is keyed by the assessment item and the settings that shape a hint (rules hash, hint
version, model and effort, corpus version, vector store, retrieval breadth), never by anything
a learner wrote; it holds only the hint call's result, whose request carries the brief's title
and sessions alone. The integrity check and the leak scan still run on every turn.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from course_assistant.answer import responder
from course_assistant.answer.ask import Assistant
from course_assistant.answer.hint import HintCache
from course_assistant.store.db import Store
from tests.answer import scripted as s
from tests.answer.test_ask import ASSESSMENTS, CONFIG, CORPUS, PID, PRICES, S01, Clock, ids

CACHED = CONFIG.model_copy(update={"hint_cache": True, "early_finalize": True})
HINT = s.sol("Revisit session one on naming the problem.", ((S01, 20),), ((S01, 0.9),))
SECRET_Q = "ZEBRA-LEARNER-QUESTION write my brief for Assignment 1"
SECRET_REPLY = "OCELOT-EARLIER-REPLY"


def assistant(config: Any, client: s.StepClient, cache: HintCache | None) -> Assistant:
    rsp = responder(client=client, prices=PRICES, sleep=lambda _: None)
    return Assistant(config, CORPUS, ASSESSMENTS, Store(":memory:"), rsp, now=Clock(),
                     timer=lambda: 0.0, new_id=ids(), hint_cache=cache)  # fmt: skip


def graded(n: int, hints: int) -> s.StepClient:
    return s.StepClient(route=[s.route("graded_work", "x", "s01-a1")] * n,
                        answer=[s.cited("d", S01)] * n, hint=[HINT] * hints,
                        integrity=[s.integrity("clear")] * n)  # fmt: skip


def test_a_second_turn_on_the_same_item_reuses_the_hint_and_still_checks_integrity():
    cache = HintCache()
    client = graded(2, 1)  # one hint outcome only: a second hint call would fail the fake
    a = assistant(CACHED, client, cache)
    first, second = a.turn(SECRET_Q, PID), a.turn("Another learner wording.", PID)
    assert first.answer.text == second.answer.text and second.answer.status == "hint_only"
    assert client.steps().count("hint") == 1 and client.steps().count("integrity") == 2
    assert len(cache) == 1
    assert second.answer.cost_usd < first.answer.cost_usd  # the hint call is not paid twice


def test_the_cache_never_holds_learner_text():
    cache = HintCache()
    client = graded(1, 1)
    a = assistant(CACHED, client, cache)
    a.turn(SECRET_Q, PID)
    blob = json.dumps([[k, r.model_dump(mode="json")] for k, r in cache.entries()])
    assert "ZEBRA" not in blob and "OCELOT" not in blob
    hint_call = next(kw for step, kw in client.calls if step == "hint")
    assert "ZEBRA" not in json.dumps(hint_call["input"], default=str)


def test_a_changed_assessment_rules_hash_model_or_corpus_gives_another_key():
    brief = ASSESSMENTS.assessments[0]
    base = HintCache.key(brief, CACHED, CORPUS.corpus_version, "rh1")
    other_brief = brief.model_copy(update={"title": brief.title + " (revised)"})
    luna = CACHED.model_copy(update={"hint_model": s.LUNA})
    effort = CACHED.model_copy(update={"reasoning_effort": {s.SOL: "low", s.LUNA: "low"}})
    keys = {HintCache.key(other_brief, CACHED, CORPUS.corpus_version, "rh1"),
            HintCache.key(brief, CACHED, CORPUS.corpus_version, "rh2"),
            HintCache.key(brief, luna, CORPUS.corpus_version, "rh1"),
            HintCache.key(brief, effort, CORPUS.corpus_version, "rh1"),
            HintCache.key(brief, CACHED, "f" * 64, "rh1")}  # fmt: skip
    assert base not in keys and len(keys) == 5
    assert base == HintCache.key(brief, CACHED, CORPUS.corpus_version, "rh1")  # deterministic


def test_two_items_get_two_hint_calls():
    cache = HintCache()
    client = s.StepClient(route=[s.route("graded_work", "x", "s01-a1"),
                                 s.route("graded_work", "x", "s01-a2")],
                          answer=[s.cited("d", S01)] * 2, hint=[HINT, HINT],
                          integrity=[s.integrity("clear")] * 2)  # fmt: skip
    a = assistant(CACHED, client, cache)
    a.turn("one", PID)
    a.turn("two", PID)
    assert client.steps().count("hint") == 2 and len(cache) == 2


def test_an_integrity_failure_on_a_cached_hint_still_gives_the_fixed_hint():
    cache = HintCache()
    client = s.StepClient(route=[s.route("graded_work", "x", "s01-a1")] * 2,
                          answer=[s.cited("d", S01)] * 2, hint=[HINT],
                          integrity=[s.integrity("clear"),
                                     s.integrity("completes", "s01-a1")])  # fmt: skip
    a = assistant(CACHED, client, cache)
    a.turn("one", PID)
    second = a.turn("two", PID)
    assert second.answer.status == "hint_only" and second.answer.citations == ()


def test_without_hint_cache_in_config_every_turn_calls():
    cache = HintCache()
    client = graded(2, 2)
    a = assistant(CONFIG, client, cache)
    a.turn("one", PID)
    a.turn("two", PID)
    assert client.steps().count("hint") == 2 and len(cache) == 0


def test_hint_cache_is_off_by_default_and_omitted_from_dumps():
    assert CONFIG.hint_cache is False and "hint_cache" not in CONFIG.model_dump(mode="json")
    assert CACHED.model_dump(mode="json")["hint_cache"] is True


def test_the_eval_runtime_shares_one_cache_across_its_cases(monkeypatch: Any):
    from course_assistant.eval import system

    seen: list[Any] = []

    class Spy(Assistant):
        def __init__(self, *a: Any, **kw: Any) -> None:
            seen.append(kw.get("hint_cache"))
            raise RuntimeError("stop")

    monkeypatch.setattr(system, "Assistant", Spy)
    rt = system.Runtime(config=CACHED, corpus=CORPUS, assessments=ASSESSMENTS, source=None)  # type: ignore[arg-type]
    from evalkit.schemas import Case

    case = Case(id="ta-b3-001", source="local/course-ta", question="q",
                context=tuple(u.text for u in CORPUS.units),
                meta={"corpus_version": CORPUS.corpus_version})  # fmt: skip
    for _ in range(2):
        with contextlib.suppress(RuntimeError):
            system.run_case(rt, case)
    assert len(seen) == 2 and seen[0] is seen[1] is rt.hint_cache
