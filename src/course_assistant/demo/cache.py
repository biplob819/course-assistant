"""The demo cache (D3.1, RPL-01; FR12, Tech Spec §8).

About 20 curated public questions, at least two per status a learner can see, answered
through the real `ask()` path and stored in A2.5's replay format (`golden.Fixture`): the
config, the corpus, the briefs and, per question, the recorded calls and the Answer their
replay gives. The demo page shows those Answers with no API call; `verify_cache` proves each
still replays to exactly what is stored.

`error` is not curated: a live model cannot be made to fail on purpose, and the demo shows
failures as guard refusals instead. Questions come from the public golden split only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from course_assistant.answer import golden
from course_assistant.answer.replay import Recorder, ReplayError, Source
from course_assistant.demo._errors import DemoCacheError
from course_assistant.ingest.corpus import Corpus
from course_assistant.schemas._base import Model
from course_assistant.schemas.config import AppConfig
from course_assistant.schemas.corpus import AssessmentList
from course_assistant.schemas.request import Answer

Shown = Literal["answered", "not_covered", "hint_only", "handed_over"]
SHOWN: tuple[Shown, ...] = ("answered", "not_covered", "hint_only", "handed_over")
MIN_PER_STATUS = 2
PUBLIC_SPLIT = "tuning"
CACHE_FORMAT_NOTE = (
    "Demo cache: curated public golden questions answered through ask() on the sample course "
    "(CC BY 4.0), built by scripts/demo_cache.py. Do not edit by hand."
)

# (public golden case id, the status it is curated to show)
CURATED: tuple[tuple[str, Shown], ...] = (
    ("ta-b1-001", "answered"),
    ("ta-b1-002", "answered"),
    ("ta-b1-003", "answered"),
    ("ta-b1-041", "answered"),  # a follow-up turn
    ("ta-b1-043", "answered"),  # a follow-up turn
    ("ta-b10-004", "answered"),  # carries an injection, still answered from the course
    ("ta-b2-003", "not_covered"),
    ("ta-b2-004", "not_covered"),
    ("ta-b2-005", "not_covered"),
    ("ta-b7-004", "not_covered"),  # the draft is not supported by the cited slide
    ("ta-b9-002", "not_covered"),  # off topic
    ("ta-b9-006", "not_covered"),  # off topic
    ("ta-b8-003", "not_covered"),  # asks for the rules
    ("ta-b3-001", "hint_only"),
    ("ta-b3-002", "hint_only"),
    ("ta-b3-021", "hint_only"),  # graded work drafted step by step over turns
    ("ta-b10-020", "hint_only"),
    ("ta-b4-002", "handed_over"),  # admin
    ("ta-b4-004", "handed_over"),  # admin
    ("ta-b4-010", "handed_over"),  # personal
)


def _hex(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]


def request_id_for(case_id: str) -> str:
    return f"r-{_hex('demo', case_id)}"


def pid_for(case_id: str) -> str:
    return _hex("demo-learner", case_id)


def curated_cases(
    public: Iterable[Mapping[str, Any]], curated: tuple[tuple[str, Shown], ...] = CURATED
) -> list[golden.Case]:
    """The curated questions as replay cases, read from public golden rows. An id that is
    missing, or not in the public split, is refused."""
    rows = {str(r.get("id")): r for r in public}
    out = []
    for i, (cid, status) in enumerate(curated, start=1):
        row = rows.get(cid)
        if row is None:
            raise DemoCacheError(f"curated case {cid} is not in the public golden split")
        if row.get("split") != PUBLIC_SPLIT:
            raise DemoCacheError(f"curated case {cid} is not public")
        if status not in SHOWN:
            raise DemoCacheError(f"curated case {cid}: {status} is not a shown status")
        out.append(golden.Case(
            id=f"demo-{i:02d}", source=cid, scenario=f"demo-{status}",
            request_id=request_id_for(cid), learner_pid=pid_for(cid),
            session=row.get("session_scope"), question=row["question"],
            prior_turns=tuple(tuple(t) for t in row.get("prior_turns") or ()),
            expected_status=status, recordings=(),
        ))  # fmt: skip
    return out


class Skipped(Model):
    case_id: str
    reason: Literal["status", "unused", "replay"]
    got: Literal["answered", "not_covered", "hint_only", "handed_over", "error"] | None = None


class BuildReport(Model):
    """What a build kept and left out: ids, statuses and counts only, never text."""

    kept: tuple[str, ...]
    skipped: tuple[Skipped, ...]
    per_status: dict[str, int]

    def ok(self) -> bool:
        return all(self.per_status.get(s, 0) >= MIN_PER_STATUS for s in SHOWN)

    def render(self) -> str:
        counts = ", ".join(f"{s} {self.per_status.get(s, 0)}" for s in SHOWN)
        lines = [f"demo cache: {len(self.kept)} kept ({counts})"]
        lines += [f"  left out {s.case_id}: {s.reason}" + (f" (got {s.got})" if s.got else "")
                  for s in self.skipped]  # fmt: skip
        return "\n".join(lines)


def build_cache(cases: list[golden.Case], *, config: AppConfig, corpus: Corpus,
                assessments: AssessmentList, source_for: Callable[[golden.Case], Source],
                now: dt.datetime, note: str = CACHE_FORMAT_NOTE,
                ) -> tuple[golden.Fixture, BuildReport]:  # fmt: skip
    """Run each case through the real `ask()` with its calls recorded, then replay the
    recordings to get the Answer that is stored. A case whose status is not the curated one,
    or whose recordings do not replay exactly, is left out. Raises `DemoCacheError` (with
    `.report`) unless at least two cases remain per shown status."""
    base = golden.Fixture(format=golden.FORMAT, note=note, now=now, config=config,
                          corpus=corpus, assessments=assessments, cases=())  # fmt: skip
    kept: list[golden.Case] = []
    skipped: list[Skipped] = []
    for case in cases:
        source = source_for(case)
        rid = case.request_id
        rec = Recorder(lambda request, _s=source, _r=rid: _s.call(_r, request))
        try:
            golden.run_case(base, case, rec)
        except ReplayError:
            skipped.append(Skipped(case_id=case.id, reason="replay"))
            continue
        recorded = case.model_copy(update={"recordings": tuple(rec.for_request(rid))})
        try:
            got = golden.replay_case(base, recorded)
        except ReplayError:
            skipped.append(Skipped(case_id=case.id, reason="unused"))
            continue
        if got.status != case.expected_status:
            skipped.append(Skipped(case_id=case.id, reason="status", got=got.status))
            continue
        kept.append(recorded.model_copy(update={"expected": got}))
    per = Counter(c.expected_status for c in kept)
    report = BuildReport(kept=tuple(c.id for c in kept), skipped=tuple(skipped),
                         per_status={s: per.get(s, 0) for s in SHOWN})  # fmt: skip
    if not report.ok():
        err = DemoCacheError("demo cache needs at least two cases per status: "
                             + ", ".join(f"{s} {report.per_status[s]}" for s in SHOWN))  # fmt: skip
        err.report = report  # type: ignore[attr-defined]
        raise err
    return base.model_copy(update={"cases": tuple(kept)}), report


def load_cache(path: Path) -> golden.Fixture:
    try:
        return golden.load(path)
    except (OSError, ValueError) as exc:
        raise DemoCacheError(f"cannot read demo cache ({type(exc).__name__})") from None


def verify_cache(fx: golden.Fixture) -> list[tuple[str, bool]]:
    """Replay every stored case through `ask()` with no API call: (case id, identical)."""
    out = []
    for case in fx.cases:
        try:
            got = golden.replay_case(fx, case)
        except ReplayError:
            out.append((case.id, False))
            continue
        out.append((case.id, case.expected is not None and got == case.expected))
    return out


def dumps(fx: golden.Fixture) -> str:
    return golden.dumps(fx)


def estimate_usd(n_cases: int, request_hold_usd: float) -> float:
    """The most a build can spend: each question at the guard's per-request hold."""
    return n_cases * request_hold_usd


class DemoCache:
    """The page's view of a demo cache: questions and stored Answers by case id."""

    def __init__(self, fx: golden.Fixture) -> None:
        self.fixture = fx
        self._by_id = {c.id: c for c in fx.cases}

    def ids(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    def question(self, case_id: str) -> str:
        return self._case(case_id).question

    def status(self, case_id: str) -> str:
        return self._case(case_id).expected_status

    def answer(self, case_id: str) -> Answer:
        case = self._case(case_id)
        if case.expected is None:
            raise DemoCacheError(f"{case_id} has no stored answer")
        return case.expected

    def _case(self, case_id: str) -> golden.Case:
        case = self._by_id.get(case_id)
        if case is None:
            raise KeyError("no such demo question")
        return case
