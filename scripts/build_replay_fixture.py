#!/usr/bin/env python3
"""Build the golden replay fixture for A2.5 (Tech Spec §10): 40 requests through `ask()`.

    uv run python scripts/build_replay_fixture.py          # write the packaged fixture
    uv run python scripts/build_replay_fixture.py --check  # exit 1 if it would change

Questions and earlier turns come from the public golden split only (never golden/heldout),
routes from the A2.1 route fixtures, the corpus structure from splitting the sample course
(unit text replaced by a placeholder) and the briefs from sample_course/assessments.yaml.
Model outputs are scripted as OpenAI SDK objects and pass through N1.2's real `respond`; each
call is recorded, given a fixed request id and latency, and the request is then replayed to
store the Answer that `ask()` returns. No network, no API key.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from tests.answer import scripted as s  # noqa: E402

from course_assistant.answer import golden, responder  # noqa: E402
from course_assistant.answer.answer import AnswerStep  # noqa: E402
from course_assistant.answer.replay import Recorder, Recording  # noqa: E402
from course_assistant.ingest import corpus_version, units_from_manifest  # noqa: E402
from course_assistant.ingest.corpus import Corpus  # noqa: E402
from course_assistant.llm import load_prices  # noqa: E402
from course_assistant.schemas.config import AppConfig, load_assessments, load_manifest  # noqa: E402
from course_assistant.schemas.corpus import CorpusUnit  # noqa: E402

PUBLIC = ROOT / "golden" / "public" / "cases.jsonl"
ROUTES = ROOT / "tests" / "answer" / "fixtures" / "routes.jsonl"
NOW = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)
Expect = Literal["answered", "not_covered", "hint_only", "handed_over", "error"]
LATENCY = {"route": 850, "answer": 3400, "support": 900, "integrity": 750, "hint": 2600}
CONFIG = AppConfig(schema_version="1.0", cohort="sample", vector_store_id="vs_sample",
                   answer_model=s.SOL, check_model=s.LUNA,
                   reasoning_effort={s.SOL: "medium", s.LUNA: "low"}, grounding_bar=0.5,
                   monthly_budget_usd=150.0)  # fmt: skip


def sample_corpus() -> Corpus:
    split = units_from_manifest(load_manifest(ROOT / "sample_course" / "manifest.yaml"), ROOT)
    cv = corpus_version(split.units)
    units = tuple(
        CorpusUnit(
            unit_index=i,
            file_id=f"file-{u.session}-{u.kind}-{u.number}",
            vs_file_id=f"vsf-{i:03d}",
            manifest_id=u.manifest_id,
            session=u.session,
            kind=u.kind,
            locator=u.locator,
            label=u.label,
            text=golden.PLACEHOLDER,
            version=u.version,
            corpus_version=cv,
        )
        for i, u in enumerate(split.units, start=1)
    )
    return Corpus(schema_version="1.0", cohort="sample", corpus_version=cv, units=units)


def _hex(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]


class Builder:
    def __init__(self) -> None:
        self.corpus = sample_corpus()
        self.assessments = load_assessments(ROOT / "sample_course" / "assessments.yaml")
        self.cases = {c["id"]: c for c in map(json.loads, _lines(PUBLIC))}
        self.routes = {r["id"]: r for r in map(json.loads, _lines(ROUTES))}
        self.used: set[str] = set()

    # ---------- picking public cases ----------

    def pick(self, rule: str, *, category: str | None = None, multi: bool | None = None,
             status: str | None = None) -> dict[str, Any]:  # fmt: skip
        for cid, c in self.cases.items():
            if cid in self.used or c["rule"] != rule:
                continue
            if status is not None and c["expected_status"] != status:
                continue
            if multi is not None and bool(c.get("prior_turns")) != multi:
                continue
            if category is not None and self.routes.get(cid, {}).get("category") != category:
                continue
            self.used.add(cid)
            return c
        raise SystemExit(f"no public {rule} case left for {category or status or multi}")

    def file_for(self, case: dict[str, Any]) -> tuple[str, str]:
        gold = case.get("gold_units") or ["s01:slides:3"]
        unit = self.corpus.resolve(gold[0])
        return unit.file_id, unit.label

    def brief_file(self, assessment_id: str) -> str:
        session = next(a for a in self.assessments.assessments
                       if a.assessment_id == assessment_id).sessions[0]  # fmt: skip
        return next(u.file_id for u in self.corpus.units if u.session == session)

    # ---------- scripted model outputs ----------

    def answered(self, case: dict[str, Any], text: str | None = None,
                 n: int = 1) -> dict[str, list[Any]]:  # fmt: skip
        file_id, label = self.file_for(case)
        return {
            "route": [s.route("course", case["question"])],
            "answer": [s.cited(text or f"This is covered in {label}.", file_id)],
            "support": [s.support(*([True] * n))],
            "integrity": [s.integrity("clear")],
        }

    def grounded_hint(self, aid: str) -> list[Any]:
        return [s.cited("Revisit the slides on this idea and how they frame it.",
                        self.brief_file(aid)), s.integrity("clear")]  # fmt: skip

    def scenarios(self) -> list[tuple[dict[str, Any], str, Expect, dict[str, list[Any]], bool]]:
        out: list[tuple[dict[str, Any], str, Expect, dict[str, list[Any]], bool]] = []

        def add(case: dict[str, Any], name: str, status: Expect, queues: dict[str, list[Any]],
                paused: bool = False) -> None:  # fmt: skip
            out.append((case, name, status, queues, paused))

        for _ in range(4):
            c = self.pick("B1", multi=False)
            add(c, "answered", "answered", self.answered(c))
        for _ in range(3):
            c = self.pick("B1", multi=True)
            add(c, "answered-follow-up", "answered", self.answered(c))
        for _ in range(2):
            c = self.pick("B10", status="answered")
            add(c, "answered-injection-carrier", "answered", self.answered(c))
        for _ in range(2):
            c = self.pick("B2")
            add(c, "not-covered-no-citation", "not_covered", {
                "route": [s.route("course")],
                "answer": [s.sol("The materials do not cover this.", (), (("file-s01-slides-3",
                                                                          0.4),))]})  # fmt: skip
        c = self.pick("B2")
        file_id, label = self.file_for(c)
        add(c, "not-covered-low-score", "not_covered", {
            "route": [s.route("course")],
            "answer": [s.cited(f"This is in {label}.", file_id, score=0.2)]})  # fmt: skip
        for _ in range(3):
            c = self.pick("B7")
            add(c, "not-covered-unsupported", "not_covered", {
                "route": [s.route("course")],
                "answer": [s.cited("Here the answer comes from general knowledge.",
                                   "file-s01-slides-3")],
                "support": [s.support(False)], "integrity": [s.integrity("clear")]})  # fmt: skip
        for _ in range(2):
            c = self.pick("B3", multi=True)
            q = self.answered(c)
            q["integrity"] = [s.integrity("completes", c["assessment_id"])]
            q["hint"] = self.grounded_hint(c["assessment_id"])[:1]
            q["integrity"].append(s.integrity("clear"))
            add(c, "hint-decomposition-caught", "hint_only", q)
        for i in range(3):
            c = self.pick("B3", multi=False, category="graded_work")
            aid = c["assessment_id"]
            hint = (
                self.grounded_hint(aid) if i < 2 else [s.sol("Just write it like this.", (), ())]
            )  # ungrounded: the fixed hint
            add(c, "hint-graded" if i < 2 else "hint-graded-fixed", "hint_only", {
                "route": [s.route("graded_work", c["question"], aid)],
                "answer": [s.cited("Discarded draft.", "file-s01-slides-3")],
                "hint": hint[:1], "integrity": hint[1:]})  # fmt: skip
        c = self.pick("B3", multi=False, category="graded_work")
        add(c, "hint-graded-unknown-brief", "hint_only", {
            "route": [s.route("graded_work", c["question"], None)],
            "answer": [s.cited("Discarded draft.", "file-s01-slides-3")]})  # fmt: skip
        c = self.pick("B10", status="hint_only")
        aid = c.get("assessment_id") or self.routes[c["id"]].get("assessment_id") or "s01-a1"
        hint = self.grounded_hint(aid)
        add(c, "hint-injection-carrier", "hint_only", {
            "route": [s.route("graded_work", "A request for graded work.", aid)],
            "answer": [s.cited("Discarded draft.", "file-s01-slides-3")],
            "hint": hint[:1], "integrity": hint[1:]})  # fmt: skip
        for category, k in (("admin", 2), ("personal", 1)):
            for _ in range(k):
                c = self.pick("B4", category=category)
                add(c, f"handed-over-{category}", "handed_over", {
                    "route": [s.route(category)],
                    "answer": [s.cited("Discarded draft.", "file-s01-slides-3")]})  # fmt: skip
        for _ in range(2):
            c = self.pick("B9")
            add(c, "off-topic", "not_covered", {
                "route": [s.route("off_topic")],
                "answer": [s.cited("Discarded draft.", "file-s01-slides-3")]})  # fmt: skip
        for _ in range(2):
            c = self.pick("B8")
            add(c, "injection", "not_covered", {
                "route": [s.route("injection", "Asks to reveal the rules.")],
                "answer": [s.cited("Discarded draft.", "file-s01-slides-3")]})  # fmt: skip
        c = self.pick("B1", multi=False)
        q = self.answered(c)
        q["route"] = [s.luna("not json"), s.luna('{"category": "course"}')]
        add(c, "route-parse-fallback", "answered", q)
        c = self.pick("B4", category="admin")
        add(c, "discarded-answer-error", "handed_over",
            {"route": [s.route("admin")], "answer": s.down()})  # fmt: skip
        c = self.pick("B1", multi=False)
        add(c, "error-route", "error", {"route": s.down(), "answer": self.answered(c)["answer"]})
        c = self.pick("B1", multi=False)
        add(c, "error-answer", "error", {"route": [s.route("course")], "answer": s.down()})
        c = self.pick("B1", multi=False)
        q = self.answered(c)
        q["support"] = s.down()
        add(c, "error-support", "error", q)
        c = self.pick("B1", multi=False)
        q = self.answered(c)
        q["integrity"] = s.down()
        add(c, "error-integrity", "error", q)
        c = self.pick("B3", multi=False, category="graded_work")
        add(c, "error-hint", "error", {
            "route": [s.route("graded_work", c["question"], c["assessment_id"])],
            "answer": [s.cited("Discarded draft.", "file-s01-slides-3")],
            "hint": s.down()})  # fmt: skip
        for name, text in (
            ("leak-canary", f"The marker is {AnswerStep.CANARY} in the materials."),
            ("leak-pid", "Your learner id is 0a1b2c3d4e5f6a7b on this course."),
            ("leak-rules", "Cite every sentence that states a fact from them, as told."),
        ):
            c = self.pick("B1", multi=False)
            add(c, name, "not_covered", self.answered(c, text))
        c = self.pick("B1", multi=False)
        add(c, "paused", "not_covered", {}, paused=True)
        return out

    # ---------- recording and replay ----------

    def build(self) -> golden.Fixture:
        cases: list[golden.Case] = []
        for i, (src, name, status, queues, paused) in enumerate(self.scenarios(), start=1):
            rid = f"r-{_hex('replay', src['id'], name)}"
            case = golden.Case(
                id=f"rp-{i:02d}", source=src["id"], scenario=name, request_id=rid,
                learner_pid=_hex("learner", src["id"]), session=src.get("session_scope"),
                question=src["question"],
                prior_turns=tuple(tuple(t) for t in src.get("prior_turns", [])),
                paused=paused, expected_status=status, recordings=(),
            )  # fmt: skip
            client = s.StepClient(**queues)
            rec = Recorder(responder(client=client, prices=load_prices(), sleep=lambda _: None,
                                     clock=lambda: 0.0))  # fmt: skip
            fx = self._fixture([])
            golden.run_case(fx, case, rec)
            if client.left():
                raise SystemExit(f"{case.id} {name}: scripted outcomes left {client.left()}")
            recs = tuple(_fixed(r) for r in rec.for_request(rid))
            cases.append(case.model_copy(update={"recordings": recs}))
        fx = self._fixture(cases)
        done = []
        for case in fx.cases:
            got = golden.replay_case(fx, case)
            if got.status != case.expected_status:
                raise SystemExit(f"{case.id} {case.scenario}: {got.status}, expected "
                                 f"{case.expected_status}")  # fmt: skip
            done.append(case.model_copy(update={"expected": got}))
        return self._fixture(done)

    def _fixture(self, cases: list[golden.Case]) -> golden.Fixture:
        return golden.Fixture(
            format=golden.FORMAT,
            note="Built by scripts/build_replay_fixture.py from the public golden split and "
            "the sample course structure (unit text not kept). Scripted model outputs, "
            "recorded through respond(). Do not edit by hand.",
            now=NOW, config=CONFIG, corpus=self.corpus, assessments=self.assessments,
            cases=tuple(cases),
        )  # fmt: skip


def _fixed(r: Recording) -> Recording:
    """A fixed provider request id and a per-step latency, so the fixture is reproducible."""
    if r.result is None:
        return r
    result = r.result.model_copy(update={"request_id": f"r-{_hex(r.request_id, r.step, str(r.n))}",
                                         "latency_ms": LATENCY[r.step]})  # fmt: skip
    return r.model_copy(update={"result": result})


def _lines(path: Path) -> list[str]:
    return [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def build_text() -> str:
    return golden.dumps(Builder().build())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 if the fixture would change")
    args = ap.parse_args(argv)
    text = build_text()
    current = golden.FIXTURE.read_text(encoding="utf-8") if golden.FIXTURE.is_file() else ""
    if args.check:
        print("fixture up to date" if text == current else "fixture is stale: rebuild it")
        return 0 if text == current else 1
    golden.FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    golden.FIXTURE.write_text(text, encoding="utf-8")
    print(f"wrote {golden.FIXTURE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
