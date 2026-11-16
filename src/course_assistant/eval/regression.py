"""The eval regression gate (E2.3 CAL-04, Tech Spec §10, `pytest -m evalreg`).

A recorded course-ta run of public cases is kept as a fixture: EvalKit's `run.json`,
`records.jsonl` and `summary.json`, the ask() recordings, the probe verdict cache (verdict words
only), the support sentences' verdicts (ids and booleans, no text) and `fixture.json`.
`regression_problems` reruns everything that can run offline and reports every difference:

(a) the cases rebuilt from the public golden split have the fixture's ids;
(b) every case replays through the `course_ta` adapter to its recorded completion;
(c) every task scorer gives its recorded score, with probe verdicts served only from the cache;
(d) EvalKit re-aggregates `summary.json` byte for byte;
(e) the runtime support check's kappa against the recorded judge verdicts has not dropped more
    than 0.05 below the baseline (EvalKit's `kappa_regressions` rule);
(f) a changed judge rubric, probe rubric, rules hash or ground version asks for a re-record.

No held-out case ever enters a fixture: `record_fixture` refuses records of other splits.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from evalkit import run as R
from evalkit.adapters import (
    BatchRequest,
    BatchResult,
    CompletionRequest,
    ModelAdapter,
    RawCompletion,
)
from evalkit.pipeline.aggregate import summary_json
from evalkit.pipeline.calibrate import KAPPA_DROP_TOLERANCE, kappa_regressions
from evalkit.pipeline.judge import JUDGE_PROMPT_HASH
from evalkit.pipeline.task_scorers import TaskScorerError, score_task
from evalkit.schemas import Case, Record
from evalkit.scorers import get_scorer

from course_assistant.answer.ask import Assistant
from course_assistant.answer.ground import GroundStep
from course_assistant.answer.replay import Recording, Replay
from course_assistant.eval import scorers, system
from course_assistant.eval.calibrate import SupportTap, agreement_from_rows
from course_assistant.eval.judges import (
    RUBRIC_HASH,
    JudgePanel,
    ProbeJudge,
    SolutionBook,
    solution_digest,
)
from course_assistant.ingest.corpus import Corpus
from course_assistant.schemas.config import AppConfig
from course_assistant.schemas.corpus import AssessmentList

FILES = ("run.json", "records.jsonl", "summary.json", "recordings.jsonl", "probe-verdicts.jsonl",
         "support.jsonl", "fixture.json")  # fmt: skip
PROBE_JUDGES = ("claude-opus-5-5", "gpt-6.1-sol")
EVALKIT_PARAMS = frozenset({"structured_output"})  # added to params by EvalKit, not the adapter


class PlaceholderSolutions:
    """A fixed stand-in for the private solutions: fixtures are public and are replayed, so the
    integrity judge's prompt (and its cache key) must not depend on private text."""

    def solution(self, assessment_id: str) -> str:
        return f"(placeholder model solution for {assessment_id}; the real one is private)"


class CacheOnly(ModelAdapter):
    """A judge that may never be called: every verdict must come from the fixture cache."""

    provider = "fixture"

    async def complete(self, req: CompletionRequest) -> RawCompletion:
        raise LookupError("a probe verdict was not in the fixture cache")

    async def submit_batch(self, reqs: list[BatchRequest]) -> str:  # pragma: no cover
        raise NotImplementedError

    async def collect_batch(self, batch_id: str, reqs: Mapping[str, CompletionRequest],
                            ) -> list[BatchResult] | None:  # pragma: no cover  # fmt: skip
        raise NotImplementedError


def _lines(path: Path) -> list[str]:
    return [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=lambda v: dict(v) if isinstance(v, Mapping)
                      else list(v))  # fmt: skip


def _ek_case(row: Mapping[str, Any]) -> Case:
    return Case(id=row["id"], question=row["question"], context=tuple(row["context"]),
                reference_label=row["label"], source="local/course-ta",
                meta=row["meta"])  # fmt: skip


def fixture_meta() -> dict[str, str]:
    return {"judge_prompt_hash": JUDGE_PROMPT_HASH, "rubric_hash": RUBRIC_HASH,
            "rules_hash": Assistant.rules_hash(),
            "ground_version": GroundStep.version()}  # fmt: skip


def baseline_kappa(support: Sequence[Mapping[str, Any]], seed: int = 7) -> dict[str, float]:
    a = agreement_from_rows(support, seed=seed, n_resamples=200)
    return {"dual": a.kappa.point, **{j: k.point for j, k in a.by_judge.items()}}


class FixtureSolutions:
    """Replays integrity from the cache: the digest of each solution the run's judges read is
    known (fixture.json); the text never is. The adapter is never called, so the prompt's
    placeholder text is never sent anywhere."""

    def __init__(self, digests: Mapping[str, str]) -> None:
        self.digests = dict(digests)

    def solution(self, assessment_id: str) -> str:
        if assessment_id not in self.digests:
            raise KeyError(f"no solution digest for assessment {assessment_id} in the fixture")
        return PlaceholderSolutions().solution(assessment_id)

    def digest(self, assessment_id: str) -> str:
        return self.digests[assessment_id]


def record_fixture(run_dir: Path, out_dir: Path, *, recordings: Sequence[Recording],
                   probe_cache: Path, support: Sequence[Mapping[str, Any]], now: dt.datetime,
                   solutions: SolutionBook,
                   source: Literal["scripted", "live"]) -> Path:  # fmt: skip
    """Write the fixture from a finished public-split run folder. `solutions` is the book the
    run's integrity judge read (only digests are kept); `source` says whether the run was
    scripted with fake judges or live."""
    records = R.read_records(run_dir)
    cases = R.read_cases(run_dir)
    held = [c.id for c in cases.values() if c.meta.get("split") != "tuning"]
    if held:
        raise ValueError(f"{len(held)} non-public case(s) in the run: a fixture is public only")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("records.jsonl", "summary.json"):
        shutil.copyfile(run_dir / name, out_dir / name)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run["task_ref"] = Path(str(run.get("task_ref") or "course-ta.yaml")).name  # no local path
    if source == "scripted":  # fake judges: nothing was live and nothing was spent
        run["live_judges"], run["spent_usd"] = False, 0.0
    (out_dir / "run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    aids = sorted({str(c.meta["assessment_id"]) for c in cases.values()
                   if isinstance(c.meta.get("assessment_id"), str)})  # fmt: skip
    digests = {a: solution_digest(solutions.solution(a)) for a in aids}
    (out_dir / "recordings.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in recordings), encoding="utf-8"
    )
    keep = _lines(probe_cache) if probe_cache.is_file() else []
    (out_dir / "probe-verdicts.jsonl").write_text("".join(x + "\n" for x in keep),
                                                  encoding="utf-8")  # fmt: skip
    (out_dir / "support.jsonl").write_text(
        "".join(json.dumps(dict(r), sort_keys=True) + "\n" for r in support), encoding="utf-8"
    )
    report = R.read_calibration(run_dir)
    calibration = ({"digest": report.heldout_digest, "rubric_hash": report.judge_prompt_hash}
                   if report is not None else None)  # fmt: skip
    meta = {**fixture_meta(), "case_ids": sorted(r.case_id for r in records),
            "calibration": calibration,
            "now": now.isoformat(), "baseline_kappa": baseline_kappa(support),
            "kappa_drop_tolerance": KAPPA_DROP_TOLERANCE, "source": source,
            "solution_digests": digests}  # fmt: skip
    (out_dir / "fixture.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n",
                                          encoding="utf-8")  # fmt: skip
    return out_dir


def regression_problems(fixture_dir: Path, rows: Sequence[Mapping[str, Any]], *, corpus: Corpus,
                        config: AppConfig, assessments: AssessmentList, log: Path,
                        tolerance: float = KAPPA_DROP_TOLERANCE) -> list[str]:  # fmt: skip
    """Every difference between the fixture and a rerun of it. `rows` are the EvalKit case rows
    rebuilt from the public golden split (`dataset.build_rows`)."""
    missing = [f for f in FILES if not (fixture_dir / f).is_file()]
    if missing:
        return [f"fixture misses {', '.join(missing)}"]
    meta = json.loads((fixture_dir / "fixture.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    for key, now_value in fixture_meta().items():
        if meta.get(key) != now_value:
            problems.append(f"{key} changed ({meta.get(key)} -> {now_value}): re-judge and "
                            "re-record the regression fixture")  # fmt: skip
    # (g) a calibrated summary counts only with its committed calibration_scored line
    problems += _calibration_problems(fixture_dir, meta, log)
    # (a) cases
    by_id = {r["id"]: r for r in rows}
    ids = list(meta.get("case_ids", []))
    absent = [i for i in ids if i not in by_id]
    if absent or not ids:
        problems.append(f"{len(absent)} fixture case(s) are not in the public golden split")
    cases = {i: _ek_case(by_id[i]) for i in ids if i in by_id}
    records = [Record.model_validate_json(x) for x in _lines(fixture_dir / "records.jsonl")]
    if sorted(r.case_id for r in records) != sorted(ids):
        problems.append("records.jsonl does not hold exactly the fixture cases")
    # (b) replay through the adapter, with the support check tapped
    recordings = [Recording.model_validate_json(x)
                  for x in _lines(fixture_dir / "recordings.jsonl")]  # fmt: skip
    tap = SupportTap(Replay(recordings))
    now = dt.datetime.fromisoformat(meta["now"])
    rt = system.Runtime(config=config, corpus=corpus, assessments=assessments, source=tap,
                        now=lambda: now, timer=lambda: 0.0)  # fmt: skip
    for rec in records:
        case = cases.get(rec.case_id)
        if case is not None:
            problems += _replay_problems(rt, case, rec)
    # (c) task scorers from the verdict cache only
    problems += _scorer_problems(fixture_dir, cases, records, assessments)
    # (d) summary, byte for byte
    problems += _summary_problems(fixture_dir, cases, records)
    # (e) support kappa against the recorded judge verdicts
    problems += _kappa_problems(fixture_dir, tap, meta, tolerance)
    return problems


def _calibration_problems(fixture_dir: Path, meta: Mapping[str, Any], log: Path) -> list[str]:
    from course_assistant.eval.tuning import read_log

    summary = json.loads((fixture_dir / "summary.json").read_text(encoding="utf-8"))
    if summary.get("calibration_status") != "calibrated":
        return []
    cal: Any = meta.get("calibration") or {}
    want = ("groundedness", cal.get("digest"), cal.get("rubric_hash"))
    ok = any(e["kind"] == "calibration_scored"
             and (e.get("calibration"), e.get("digest"), e.get("rubric_hash")) == want
             for e in read_log(log))  # fmt: skip
    if ok:
        return []
    return [
        "summary.json says calibrated but the tuning log has no matching calibration_scored "
        "line (certify with scripts/calibrate.py score, never evalkit calibrate score)"
    ]


def _replay_problems(rt: system.Runtime, case: Case, rec: Record) -> list[str]:
    request = CompletionRequest(model=rec.completion.model, model_id=rec.completion.model,
                                prompt="(replay)")  # fmt: skip
    try:
        answer, rid = system.run_case(rt, case)
        raw = system.completion_for(rt, answer, rid, request)
    except ConnectionError:
        if rec.completion.error == "transport":
            return []
        return [f"{case.id}: replay failed where the recorded call succeeded"]
    except Exception as exc:  # a broken replay is a finding, never a crash of the gate
        return [f"{case.id}: replay raised {type(exc).__name__}"]
    if rec.completion.error is not None:
        return [f"{case.id}: replay answered where the recorded call failed"]
    recorded = {k: v for k, v in rec.completion.params.items() if k not in EVALKIT_PARAMS}
    got = (raw.raw_text, raw.model_snapshot, _canon(raw.params), raw.latency_ms, raw.cost_usd)
    want = (rec.completion.raw_text, rec.completion.model_snapshot, _canon(recorded),
            rec.completion.latency_ms, rec.completion.cost_usd)  # fmt: skip
    return [] if got == want else [f"{case.id}: replayed completion differs from the recording"]


def _scorer_problems(fixture_dir: Path, cases: Mapping[str, Case], records: Sequence[Record],
                     assessments: AssessmentList) -> list[str]:  # fmt: skip
    cache_copy = Path(tempfile.mkdtemp(prefix="evalreg-")) / "probe-verdicts.jsonl"
    shutil.copyfile(fixture_dir / "probe-verdicts.jsonl", cache_copy)  # the fixture stays as is
    panel = JudgePanel([ProbeJudge(PROBE_JUDGES[0], PROBE_JUDGES[0], CacheOnly()),
                        ProbeJudge(PROBE_JUDGES[1], PROBE_JUDGES[1], CacheOnly())],
                       cache=cache_copy, retry_sleep=_no_sleep)  # fmt: skip
    before = (scorers._Judges.panel, scorers._Judges.solutions, scorers._Judges.briefs)  # pyright: ignore[reportPrivateUsage]
    meta = json.loads((fixture_dir / "fixture.json").read_text(encoding="utf-8"))
    scorers.configure_judges(panel, FixtureSolutions(meta.get("solution_digests", {})),
                             assessments)  # fmt: skip
    out: list[str] = []
    try:
        for rec in records:
            case = cases.get(rec.case_id)
            if case is None or rec.scores is None:
                continue
            names = sorted(rec.task_scores)
            try:
                got = score_task([get_scorer(n) for n in names], case, rec.completion,
                                 model=rec.completion.model, claims=rec.claims,
                                 verdicts=rec.verdicts, scores=rec.scores)  # fmt: skip
            except TaskScorerError:
                out.append(f"{rec.case_id}: a task scorer could not rerun from the cache")
                continue
            for n in names:
                a, b = got[n], rec.task_scores[n]
                if (a.value, a.version, _canon(a.details)) != (b.value, b.version,
                                                                _canon(b.details)):  # fmt: skip
                    out.append(f"{rec.case_id}: task score {n} differs from the recording")
    finally:
        scorers._Judges.panel, scorers._Judges.solutions, scorers._Judges.briefs = before  # pyright: ignore[reportPrivateUsage]
        shutil.rmtree(cache_copy.parent, ignore_errors=True)
    return out


async def _no_sleep(_: float) -> None:
    return None


def _summary_problems(fixture_dir: Path, cases: Mapping[str, Case],
                      records: Sequence[Record]) -> list[str]:  # fmt: skip
    work = Path(tempfile.mkdtemp(prefix="evalreg-run-"))
    try:
        shutil.copyfile(fixture_dir / "run.json", work / "run.json")
        (work / "cases.jsonl").write_text(
            "".join(cases[i].model_dump_json() + "\n" for i in sorted(cases)), encoding="utf-8"
        )
        manifest = R.read_manifest(work)
        summary = R.summarise(work, manifest, records, "complete")
        text = summary_json(summary)
    except Exception as exc:
        return [f"summary could not be re-aggregated ({type(exc).__name__})"]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    want = (fixture_dir / "summary.json").read_text(encoding="utf-8")
    return [] if text == want else ["summary.json differs from EvalKit's re-aggregation"]


def _kappa_problems(fixture_dir: Path, tap: SupportTap, meta: Mapping[str, Any],
                    tolerance: float) -> list[str]:  # fmt: skip
    rows = [json.loads(x) for x in _lines(fixture_dir / "support.jsonl")]
    seen = {(s.request_id, s.n): s.runtime_supported for s in tap.sentences}
    out: list[str] = []
    current_rows = []
    for r in rows:
        key = (r["request_id"], r["n"])
        if key not in seen:
            out.append(f"support sentence {r['request_id']}#{r['n']} was not judged on replay")
            continue
        current_rows.append({**r, "runtime": seen[key]})
    extra = set(seen) - {(r["request_id"], r["n"]) for r in rows}
    if extra:
        out.append(f"{len(extra)} support sentence(s) on replay have no recorded judge verdicts")
    if not current_rows:
        return [*out, "no support sentences to compare"]
    current = baseline_kappa(current_rows)
    baseline = {str(k): float(v) for k, v in dict(meta.get("baseline_kappa", {})).items()}
    if not baseline:
        return [*out, "fixture has no baseline_kappa"]
    # EvalKit's rule (an exact drop of the tolerance passes); the wording names what this is
    return out + [
        p.replace("held-out kappa", "public support kappa")
        for p in kappa_regressions(baseline, current, tolerance)
    ]
