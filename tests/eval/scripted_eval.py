"""Offline building blocks for the course-ta tests: the sample corpus with its text, scripted
model outputs per golden case (SDK-model fakes through N1.2's real `respond`, L-9), and fake
EvalKit model adapters for the extractor, the groundedness judges and the probe judges.

Public golden split only (never golden/heldout). No network, no key, no spend.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from evalkit.adapters import (
    BatchRequest,
    BatchResult,
    CompletionRequest,
    ModelAdapter,
    RawCompletion,
)

from course_assistant.answer import responder
from course_assistant.answer.replay import Responder
from course_assistant.eval.dataset import load_public, manifest_version
from course_assistant.eval.system import request_id_for
from course_assistant.ingest import corpus_version, units_from_manifest
from course_assistant.ingest.corpus import Corpus
from course_assistant.llm import ResponsesRequest, ResponsesResult, load_prices
from course_assistant.schemas.config import AppConfig, GoldenCase, load_assessments, load_manifest
from course_assistant.schemas.corpus import CorpusUnit
from tests.answer import scripted as s

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "golden" / "public" / "cases.jsonl"  # never golden/heldout (E2.1)
MANIFEST = load_manifest(ROOT / "sample_course" / "manifest.yaml")
GOLDEN_VERSION = manifest_version(MANIFEST)
ASSESSMENTS = load_assessments(ROOT / "sample_course" / "assessments.yaml")
PRICES = load_prices()
CONFIG = AppConfig(schema_version="1.0", cohort="sample", vector_store_id="vs_sample",
                   answer_model=s.SOL, check_model=s.LUNA,
                   reasoning_effort={s.SOL: "medium", s.LUNA: "low"}, grounding_bar=0.5,
                   monthly_budget_usd=150.0)  # fmt: skip


def sample_corpus() -> Corpus:
    """The sample course split into units, with its (public, licensed) text kept."""
    split = units_from_manifest(MANIFEST, ROOT)
    cv = corpus_version(split.units)
    units = tuple(
        CorpusUnit(unit_index=i, file_id=f"file-{u.session}-{u.kind}-{u.number}",
                   vs_file_id=f"vsf-{i:03d}", manifest_id=u.manifest_id, session=u.session,
                   kind=u.kind, locator=u.locator, label=u.label, text=u.text,
                   version=u.version, corpus_version=cv)
        for i, u in enumerate(split.units, start=1)
    )  # fmt: skip
    return Corpus(schema_version="1.0", cohort="sample", corpus_version=cv, units=units)


CORPUS = sample_corpus()
PUBLIC_CASES: list[GoldenCase] = load_public(PUBLIC)
BY_ID = {c.id: c for c in PUBLIC_CASES}


def first(rule: str, *, status: str | None = None, multi: bool | None = None,
          skip: int = 0) -> GoldenCase:  # fmt: skip
    pool = [c for c in PUBLIC_CASES if c.rule == rule
            and (status is None or c.expected_status == status)
            and (multi is None or bool(c.prior_turns) == multi)]  # fmt: skip
    return pool[skip]


def brief_file(assessment_id: str) -> str:
    session = next(a for a in ASSESSMENTS.assessments
                   if a.assessment_id == assessment_id).sessions[0]  # fmt: skip
    return next(u.file_id for u in CORPUS.units if u.session == session)


def gold_file(case: GoldenCase) -> str:
    return CORPUS.resolve(case.gold_units[0]).file_id if case.gold_units else "file-s01-slides-3"


def answered(case: GoldenCase, text: str | None = None) -> dict[str, list[Any]]:
    return {"route": [s.route("course", case.question)],
            "answer": [s.cited(text or "The course covers this on the cited slide.",
                               gold_file(case))],
            "support": [s.support(True)], "integrity": [s.integrity("clear")]}  # fmt: skip


def hint(case: GoldenCase, text: str = "Revisit the slides on this idea and how they frame it.",
         ) -> dict[str, list[Any]]:  # fmt: skip
    aid = case.assessment_id or "s01-a1"
    return {
        "route": [s.route("graded_work", case.question, aid)],
        "answer": [s.cited("Discarded draft.", "file-s01-slides-3")],
        "hint": [s.cited(text, brief_file(aid))],
        "integrity": [s.integrity("clear")],
    }


def routed(category: str) -> dict[str, list[Any]]:
    return {
        "route": [s.route(category)],
        "answer": [s.cited("Discarded draft.", "file-s01-slides-3")],
    }


def not_covered() -> dict[str, list[Any]]:
    return {"route": [s.route("course")],
            "answer": [s.sol("The materials do not cover this.", (),
                             (("file-s01-slides-3", 0.4),))]}  # fmt: skip


def error_route(case: GoldenCase) -> dict[str, list[Any]]:
    return {"route": s.down(), "answer": answered(case)["answer"]}


def script_for(case: GoldenCase) -> dict[str, list[Any]]:
    """A plausible scripted turn for a case, by rule and expected status."""
    if case.expected_status == "answered":
        return answered(case)
    if case.expected_status == "hint_only":
        return hint(case)
    if case.expected_status == "handed_over":
        return routed("admin")
    if case.rule == "B8":
        return routed("injection")
    if case.rule == "B9":
        return routed("off_topic")
    return not_covered()


class PerCase:
    """A `Source` that sends each request to its own case's scripted client, so cases running
    at the same time can never take each other's outcomes."""

    def __init__(self, scripts: Mapping[str, dict[str, list[Any]]]) -> None:
        self.clients = {request_id_for(cid): s.StepClient(**q) for cid, q in scripts.items()}
        self.responders: dict[str, Responder] = {
            rid: responder(client=c, prices=PRICES, sleep=lambda _: None, clock=lambda: 0.0)
            for rid, c in self.clients.items()
        }

    def call(self, request_id: str, request: ResponsesRequest) -> ResponsesResult:
        return self.responders[request_id](request)

    def left(self) -> dict[str, dict[str, int]]:
        return {rid: c.left() for rid, c in self.clients.items() if c.left()}


# ---------- fake EvalKit model adapters ----------

EXTRACT_RE = re.compile(r"Answer \(citations[^:]*: (.*)\nCited sentence numbers: \[(.*)\]")
PROBE_RATIONALE = "probe-rationale-not-for-records"
CLEAN = json.dumps({"verdict": "clean", "rationale": PROBE_RATIONALE})


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


def evalkit_reply(probe: Callable[[CompletionRequest], str] | None = None,
                  ) -> Callable[[CompletionRequest], str]:  # fmt: skip
    """Stand-ins for EvalKit's extractor and groundedness judges, and the probe judges."""

    def reply(req: CompletionRequest) -> str:
        if req.model == "extractor":
            m = EXTRACT_RE.search(req.prompt)
            assert m is not None
            cited = [int(x) for x in m.group(2).split(",") if x.strip()]
            claim = {"text": m.group(1) or "No claim.", "cited": cited, "addresses_question": True}
            return json.dumps({"claims": [claim]})
        if req.model == "judge":
            shown = req.prompt.split("Cited source sentences:")[1]
            nums = re.findall(r"\[(\d+)\]", shown)
            return json.dumps({"verdict": "supported" if nums else "unsupported",
                               "evidence": [int(nums[0])] if nums else [], "quote": None,
                               "rationale": "scripted"})  # fmt: skip
        return probe(req) if probe is not None else CLEAN

    return reply


class FakeAdapter(ModelAdapter):
    """A scripted EvalKit ModelAdapter: no network, a fixed cost per call."""

    provider = "fake"

    def __init__(self, reply: Callable[[CompletionRequest], str | Exception | RawCompletion],
                 cost: float = 0.001) -> None:  # fmt: skip
        self.reply = reply
        self.cost = cost
        self.requests: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> RawCompletion:
        await asyncio.sleep(0)
        self.requests.append(req)
        out = self.reply(req)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, RawCompletion):
            return out
        return RawCompletion(model=req.model, model_snapshot=req.model_id + "-snap",
                             params=dict(req.params), raw_text=out, tokens_in=500,
                             tokens_out=40, latency_ms=700, cost_usd=self.cost)  # fmt: skip

    async def submit_batch(self, reqs: list[BatchRequest]) -> str:  # pragma: no cover
        raise NotImplementedError

    async def collect_batch(self, batch_id: str, reqs: Mapping[str, CompletionRequest],
                            ) -> list[BatchResult] | None:  # pragma: no cover  # fmt: skip
        raise NotImplementedError


def prompts(adapters: Iterable[FakeAdapter]) -> list[str]:
    return [r.prompt for a in adapters for r in a.requests]
