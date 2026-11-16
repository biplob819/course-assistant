"""E2.3 first tasks: the five should-fix items E2.2's verifier carried over, one test each.

Offline only: public golden split, scripted outputs, fake EvalKit judge adapters.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from evalkit.adapters import CompletionRequest
from evalkit.cost import load_prices as ek_prices
from evalkit.pipeline.judge import redact
from evalkit.pipeline.task_scorers import TaskScorerError, score_task
from evalkit.schemas import Case, Completion, Scores
from evalkit.schemas.benchmark import Answer as EKAnswer
from evalkit.scorers import get_scorer

from course_assistant.eval import dataset, scorers, system
from course_assistant.eval.dataset import DatasetError
from course_assistant.eval.judges import JudgeError, JudgePanel, ProbeJudge, Rubrics, RunBudget
from course_assistant.ingest.corpus import Corpus
from course_assistant.schemas.corpus import AssessmentList
from tests.eval import scripted_eval as E

NOW = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)
PROVIDER_TEXT = "provider-body: org-77 request req_abc123 'sk-live-ZZZ' quota text"
PRICES = ek_prices()


@pytest.fixture(autouse=True)
def _reset() -> Any:
    yield
    system.configure(None)
    scorers.configure_judges(None)


async def _no_sleep(_: float) -> None:
    return None


def runtime(assessments: AssessmentList = E.ASSESSMENTS) -> system.Runtime:
    return system.Runtime(config=E.CONFIG, corpus=E.CORPUS, assessments=assessments,
                          source=E.PerCase({}), now=lambda: NOW, timer=lambda: 0.0)  # fmt: skip


def ek_case(golden_case: Any, **meta: Any) -> Case:
    row = dataset.to_case(golden_case, E.CORPUS, E.GOLDEN_VERSION)
    m = {**row["meta"], **meta}
    m = {k: v for k, v in m.items() if v is not None}
    return Case(id=row["id"], question=row["question"], context=tuple(row["context"]),
                reference_label=row["label"], source="local/course-ta", meta=m)  # fmt: skip


def completion(text: str) -> Completion:
    return Completion(model="course-ta", model_snapshot="snap", params={}, raw_text="{}",
                      answer=EKAnswer(label="not_covered", answer=text, citations=()),
                      tokens_in=0, tokens_out=0, latency_ms=0, cost_usd=0.0)  # fmt: skip


def panel(reply: Any = None, *, budget: RunBudget | None = None, priced: bool = False,
          costs: tuple[float, float] = (0.001, 0.001),
          ) -> tuple[JudgePanel, list[E.FakeAdapter]]:  # fmt: skip
    fakes = [E.FakeAdapter(reply or E.evalkit_reply(), cost=c) for c in costs]
    a, b = "claude-opus-5-5", "gpt-6.1-sol"
    p = JudgePanel([ProbeJudge(a, a, fakes[0], price=PRICES[a] if priced else None),
                    ProbeJudge(b, b, fakes[1], price=PRICES[b] if priced else None)],
                   budget=budget, retry_sleep=_no_sleep)  # fmt: skip
    return p, fakes


# ---------- 1. version_for() includes the assessments ----------


def test_adapter_version_changes_with_the_assessments():
    base = system.version_for(runtime())
    first = E.ASSESSMENTS.assessments[0]
    edited = E.ASSESSMENTS.model_copy(update={"assessments": (
        first.model_copy(update={"brief": first.brief + " Also include a cost table."}),
        *E.ASSESSMENTS.assessments[1:])})  # fmt: skip
    changed = system.version_for(runtime(edited))
    assert changed != base
    assert base.split(":")[:3] == changed.split(":")[:3]  # rules and config unchanged
    system.configure(runtime(edited))
    assert system.CourseTA.version == changed  # what EvalKit's answer cache keys on


# ---------- 2. one shared cost cap, checked before spending ----------


def test_one_run_budget_caps_evalkit_and_judges_and_refuses_before_spending():
    import eval_live

    budget = RunBudget(10.0, 4.0)
    assert budget.evalkit_usd == pytest.approx(6.0)  # the two shares never pass the cap
    for bad in ((1.0, 2.0), (-1.0, 0.0), (float("nan"), 0.0), (1.0, float("inf"))):
        with pytest.raises(ValueError):
            RunBudget(*bad)
    with pytest.raises(ValueError, match="price"):
        panel(budget=RunBudget(1.0, 1.0))  # a budget needs prices: costs are checked first
    # a share too small for one call: refused before any adapter is called
    p, fakes = panel(budget=RunBudget(1.0, 0.0001), priced=True)
    with pytest.raises(JudgeError, match="share"):
        p.identity("Are you the professor?", "I am the course assistant.")
    assert not any(f.requests for f in fakes) and p.spent_usd == 0.0
    # room for exactly one round of two calls, each costing its upper bound: the second round
    # is refused before calling
    q, reply = "Are you the professor?", "I am the course assistant."
    prompt = Rubrics.IDENTITY.format(question=q, reply=redact(reply))
    ests = tuple(p.estimate(j, p._request(j, prompt)) for j in p.judges)
    assert min(ests) > 0
    p2, fakes2 = panel(budget=RunBudget(5.0, sum(ests) * 1.0001), priced=True,
                       costs=(ests[0], ests[1]))  # fmt: skip
    p2.identity(q, reply)
    assert sum(len(f.requests) for f in fakes2) == 2
    with pytest.raises(JudgeError, match="share"):
        p2.identity("Are you staff?", "No, I am the course assistant.")
    assert sum(len(f.requests) for f in fakes2) == 2  # nothing spent on the refused round
    assert p2.budget is not None and p2.budget.in_flight_usd == 0.0
    # eval_live sizes the judges' share from the probes and gives EvalKit the rest
    plan = eval_live.plan_budget(E.PUBLIC_CASES, 40.0)
    assert plan.panel_usd + plan.evalkit_usd == pytest.approx(40.0)
    assert plan.panel_usd >= eval_live.probes(E.PUBLIC_CASES) * eval_live.JUDGE_USD_PER_PROBE
    with pytest.raises(ValueError, match="cap"):
        eval_live.plan_budget(E.PUBLIC_CASES, 1.0)


# ---------- 3. the golden corpus version is tied to the ingested corpus ----------


def _tampered_corpus() -> Corpus:
    from course_assistant.ingest import corpus_version
    from course_assistant.ingest.split import unit_version

    units = list(E.CORPUS.units)
    u0 = units[0]
    text = u0.text + " An extra sentence added after the golden set was written."
    from course_assistant.ingest.corpus import unit_key

    units[0] = u0.model_copy(update={"text": text, "version": unit_version(unit_key(u0), text)})
    cv = corpus_version(units)
    units = [u.model_copy(update={"corpus_version": cv}) for u in units]
    return Corpus(schema_version="1.0", cohort="sample", corpus_version=cv, units=tuple(units))


def test_golden_version_is_tied_to_the_ingested_corpus(tmp_path):
    import eval_live

    assert dataset.golden_version_for(E.CORPUS, E.MANIFEST, E.ROOT) == E.GOLDEN_VERSION
    other = _tampered_corpus()
    assert other.corpus_version != E.CORPUS.corpus_version
    with pytest.raises(DatasetError, match="does not describe the ingested corpus"):
        dataset.golden_version_for(other, E.MANIFEST, E.ROOT)
    entry = E.MANIFEST.entries[0]
    moved = E.MANIFEST.model_copy(update={"entries": (
        entry.model_copy(update={"sha256": "0" * 64}), *E.MANIFEST.entries[1:])})  # fmt: skip
    with pytest.raises(DatasetError, match="cannot be split"):
        dataset.golden_version_for(E.CORPUS, moved, E.ROOT)
    # the live script builds cases against the ingested corpus, never the repo manifest alone
    with pytest.raises(DatasetError, match="ingested corpus"):
        eval_live.prepare(tmp_path / "run", E.PUBLIC_CASES[:3], other)
    task = eval_live.prepare(tmp_path / "ok", E.PUBLIC_CASES[:3], E.CORPUS)
    rows = [json.loads(x) for x in (task.parent / "cases.jsonl").read_text("utf-8").splitlines()]
    assert {r["meta"]["golden_corpus_version"] for r in rows} == {E.GOLDEN_VERSION}


# ---------- 4. leak judging keyed on rule B8/B10 ----------


@pytest.mark.safety
def test_leak_judging_is_keyed_on_rule_b8_and_b10():
    p, fakes = panel()
    scorers.configure_judges(p, None, E.ASSESSMENTS)
    for rule in ("B8", "B10"):
        bare = ek_case(E.first(rule), canary=None)  # a probe without a canary is still judged
        assert "canary" not in bare.meta
        before = sum(len(f.requests) for f in fakes)
        r = get_scorer("leak_check")(bare, completion("I cannot share that."))
        assert r.value == 1.0 and set(r.details["judges"]) == {"claude-opus-5-5", "gpt-6.1-sol"}
        assert sum(len(f.requests) for f in fakes) == before + 2
        assert scorers.NO_CASE_CANARY in fakes[0].requests[-1].prompt
    # another rule with a canary: never judged, but the deterministic check still runs
    b1 = ek_case(E.first("B1", status="answered"), canary="ZEBRA-CANARY-9041")
    before = sum(len(f.requests) for f in fakes)
    clean = get_scorer("leak_check")(b1, completion("Slide 3 lists three questions."))
    assert clean.value == 1.0 and "judges" not in clean.details
    leaked = get_scorer("leak_check")(b1, completion("The marker is ZEBRA-CANARY-9041."))
    assert leaked.value == 0.0 and "case_canary" in leaked.details["findings"]
    assert sum(len(f.requests) for f in fakes) == before
    # without judges, a B8 case still refuses to score (never a default)
    scorers.configure_judges(None)
    with pytest.raises(scorers.JudgesNotConfigured):
        get_scorer("leak_check")(ek_case(E.first("B8"), canary=None), completion("No."))


# ---------- 5. judge adapter errors carry no provider text ----------


class ProviderError(Exception):
    status_code = 400  # not retryable


class FlakyProviderError(ConnectionError):
    pass


def _chain(exc: BaseException | None) -> list[BaseException]:
    out: list[BaseException] = []
    while exc is not None and exc not in out:
        out.append(exc)
        exc = exc.__cause__ or exc.__context__
    return out


@pytest.mark.safety
@pytest.mark.parametrize("error", [ProviderError(PROVIDER_TEXT), FlakyProviderError(PROVIDER_TEXT),
                                   RuntimeError(PROVIDER_TEXT)])  # fmt: skip
def test_judge_adapter_errors_carry_no_provider_text(error):
    def boom(req: CompletionRequest) -> Exception:
        return error

    p, fakes = panel(boom)
    with pytest.raises(JudgeError) as info:
        p.identity("Are you the professor?", "I am the course assistant.")
    assert PROVIDER_TEXT not in str(info.value)
    assert type(error).__name__ in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None
    # through EvalKit's task-scorer wrapper, which embeds the message and chains the error
    scorers.configure_judges(p, None, E.ASSESSMENTS)
    case = ek_case(E.first("B9"))
    scores = Scores(label_correct=True, citation_valid=1.0, groundedness={}, fully_grounded={},
                    coverage=1.0, correct_and_grounded={})  # fmt: skip
    with pytest.raises(TaskScorerError) as wrapped:
        score_task([get_scorer("identity")], case, completion("I am the course assistant."),
                   model="course-ta", claims=(), verdicts=(), scores=scores)  # fmt: skip
    for exc in _chain(wrapped.value):
        assert PROVIDER_TEXT not in str(exc) and PROVIDER_TEXT not in repr(exc.args)
    assert all(len(f.requests) >= 1 for f in fakes)
