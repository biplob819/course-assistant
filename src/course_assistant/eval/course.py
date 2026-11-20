"""Evaluate a course from the installed package (R3.1, `course-assistant eval`).

A course team keeps its own golden cases in its own files and passes them by path; nothing
here reads this repository's `golden/` folder. The pieces are the ones `scripts/eval_live.py`
and `scripts/release_gate.py` use for the sample course:

- `load_cases`: a golden JSONL file, every case of one split (`tuning` or `heldout`);
- `prepare`: drift first (cases whose gold units are gone are retired or converted), then the
  EvalKit task folder, `drift.json` (ids only) and `evalkit.yaml`;
- `estimate` and `plan_budget`: the cost estimate and one cap shared by EvalKit and the judges;
- `run`: the EvalKit run with the course-ta system and the probe judges installed;
- `course_gate`: the §6.4 thresholds on a finished EvalKit run folder.

`course_gate` is the thresholds only. The repository's full release gate adds what needs this
repository's records (judge calibration certified against the tuning log, the regression
fixture, the paired comparison with the last live run, the held-out MANIFEST check); a course
team that needs those works in a fork. Needs the `eval` extra. Nothing here prints case text.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from course_assistant.eval.panel import EXTRACTOR, JUDGES
from course_assistant.ingest.corpus import Corpus
from course_assistant.schemas.config import GoldenCase
from course_assistant.schemas.corpus import Manifest

SYSTEM_ALIAS = "course-ta"
# measured on the E2.5 public tuning run (course-ta-public-20261117T060529Z: 214 cases, 110
# probes, 98 answered; ledger): the figures below keep at least 1.5 times these
MEASURED_E25 = {"evalkit_usd_per_case": 0.0135, "judge_usd_per_probe": 0.0065,
                "support_usd_per_answer": 0.0101}  # fmt: skip
JUDGE_USD_PER_PROBE = 0.02  # two judges, one probe (was 0.04 planning; 3x measured)
EVALKIT_USD_PER_CASE = 0.025  # extractor plus two groundedness judges per answer (~1.9x measured)
SUPPORT_USD_PER_ANSWER = 0.02  # two judges on an answer's few L2 sentences (2x measured)
PANEL_HEADROOM = 2.0  # the judges' share covers a re-ask of every probe
# the two figures above that say "two judges" were measured on a dual panel; a panel of
# another size scales them (OWNER D-80 runs one judge, so its judging is half)
JUDGE_SHARE = len(JUDGES) / 2
PROBE_RULES = ("B3", "B8", "B9", "B10")


class CourseEvalError(ValueError):
    """The course's cases or files cannot be evaluated. Messages name ids and files only."""


def evalkit_config(seed: int = 7) -> str:
    return (f"models: [{SYSTEM_ALIAS}]\nsystems: {{{SYSTEM_ALIAS}: course_ta}}\n"
            f"judges: [{', '.join(JUDGES)}]\nextractor: {EXTRACTOR}\nseed: {seed}\n")  # fmt: skip


def probes(cases: Sequence[Any]) -> int:
    """Cases a probe judge reads: B3 (integrity), B9 (identity), B8 and B10 (leak)."""
    return sum(c.rule in PROBE_RULES for c in cases)


def evalkit_estimate(cases: Sequence[Any], *, replay: bool = False) -> float:
    """EvalKit's share: generating each case (nothing on a replay) plus extracting and
    judging its claims."""
    from course_assistant.eval.system import PER_CASE_USD

    return len(cases) * ((0.0 if replay else PER_CASE_USD) + EVALKIT_USD_PER_CASE)


def panel_estimate(cases: Sequence[Any]) -> float:
    from course_assistant.eval.judges import PROBE_SAMPLES  # D-85: each probe asked n times

    answered = sum(c.expected_status == "answered" for c in cases)
    return JUDGE_SHARE * (probes(cases) * JUDGE_USD_PER_PROBE * PANEL_HEADROOM * PROBE_SAMPLES
                          + answered * SUPPORT_USD_PER_ANSWER)  # fmt: skip


def estimate(cases: Sequence[Any], *, replay: bool = False) -> float:
    return evalkit_estimate(cases, replay=replay) + panel_estimate(cases)


def plan_budget(cases: Sequence[Any], max_cost: float, *, replay: bool = False) -> Any:
    """One cap for the run: the judges' share sized from the probes, EvalKit the rest. Refused
    when either share would be below its estimate."""
    from course_assistant.eval.judges import RunBudget

    panel = panel_estimate(cases)
    if panel + evalkit_estimate(cases, replay=replay) > max_cost:
        raise ValueError(f"the estimate ${estimate(cases, replay=replay):.2f} is over the cap "
                         f"${max_cost:.2f}")  # fmt: skip
    return RunBudget(max_cost, panel)


def evalkit_args(out_dir: Path, task: Path, n: int, usd: float, *,
                 live_judges: bool, fresh_store: bool = False) -> list[str]:  # fmt: skip
    """`evalkit run` arguments. `--live-judges` judges at full price now instead of waiting for
    provider batches (used to keep tuning rounds short; a pending batch is never paid twice)."""
    out = ["run", str(out_dir / "evalkit.yaml"), "--task", str(task), "--n", str(n),
           "--out", str(out_dir / "evalkit"), "--max-cost", f"{usd:.4f}"]  # fmt: skip
    if fresh_store:  # D-89: nothing from EvalKit's shared cache (answers, claims, verdicts)
        out += ["--store", str(out_dir / "evalkit-store")]
    return [*out, "--live-judges"] if live_judges else out


# ---------- the course's own cases ----------


def load_cases(path: Path) -> tuple[list[GoldenCase], str]:
    """Every case in a golden JSONL file and its split: `public` when every case is `tuning`,
    `heldout` when every case is `heldout`. A file that mixes the two is refused, so a tuning
    case can never be scored as held-out."""
    try:
        lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, UnicodeDecodeError):
        raise CourseEvalError(f"cannot read the cases file {path.name}") from None
    cases = []
    for i, line in enumerate(lines, start=1):
        try:
            cases.append(GoldenCase.model_validate_json(line))
        except ValueError:
            raise CourseEvalError(f"{path.name}: line {i} is not a valid golden case") from None
    if not cases:
        raise CourseEvalError(f"{path.name} holds no cases")
    splits = {c.split for c in cases}
    if splits == {"tuning"}:
        return cases, "public"
    if splits == {"heldout"}:
        return cases, "heldout"
    raise CourseEvalError(f"{path.name} mixes tuning and held-out cases: keep one split per "
                          "file and run each separately")  # fmt: skip


def load_case_files(paths: Sequence[Path]) -> tuple[list[GoldenCase], str, Path | None]:
    """(cases, split, the held-out file or None) for one file, or for a tuning and a held-out
    file together (split `both`: a calibration run, whose sheets take half from each)."""
    if len(paths) == 1:
        cases, split = load_cases(paths[0])
        return cases, split, paths[0] if split == "heldout" else None
    if len(paths) != 2:
        raise CourseEvalError("give one cases file, or a tuning file and a held-out file")
    loaded = [load_cases(p) for p in paths]
    splits = {split: (p, cases) for p, (cases, split) in zip(paths, loaded, strict=True)}
    if set(splits) != {"public", "heldout"}:
        raise CourseEvalError("two cases files must be one tuning file and one held-out file")
    held_path, held = splits["heldout"]
    _, public = splits["public"]
    shared = sorted({c.id for c in held} & {c.id for c in public})
    if shared:
        raise CourseEvalError(f"{len(shared)} case id(s) are in both files: {shared[:5]}")
    return [*public, *held], "both", held_path


def prepare(out_dir: Path, cases: Sequence[GoldenCase], corpus: Corpus, manifest: Manifest,
            root: Path) -> Path:  # fmt: skip
    """Write the task folder and evalkit.yaml; return the task YAML path. The golden version
    is accepted only for the corpus that was ingested from `manifest` (E2.3). Drift is applied
    first (E2.4 GAT-04), and `drift.json` (ids only) records it."""
    from course_assistant.eval import dataset
    from course_assistant.eval.release import drift

    version = dataset.golden_version_for(corpus, manifest, root)
    found = drift(list(cases), corpus, version)
    rows = dataset.build_rows(list(found.cases), corpus, version)
    task = dataset.write_task_dir(out_dir, rows)
    (out_dir / "drift.json").write_text(json.dumps(found.record(corpus.corpus_version), indent=1)
                                        + "\n", encoding="utf-8")  # fmt: skip
    (out_dir / "evalkit.yaml").write_text(evalkit_config(), encoding="utf-8")
    return task


def live_cases(task: Path) -> int:
    """Cases the run will answer: the task's rows (drift-retired cases are already out)."""
    from course_assistant.eval.dataset import DATASET_FILE

    rows = (task.parent / DATASET_FILE).read_text(encoding="utf-8").splitlines()
    return sum(1 for x in rows if x.strip())


def run(out_dir: Path, task: Path, *, n: int, budget: Any, runtime: Any, panel: Any,
        solutions: Any, assessments: Any, live_judges: bool = False) -> int:  # fmt: skip
    """`evalkit run` with the course-ta system (`runtime`) and the probe judges (`panel`, the
    private `solutions` for the integrity judge) installed, and removed again afterwards.
    Returns EvalKit's exit code."""
    from evalkit import cli as evalkit_cli

    from course_assistant.eval import scorers, system

    system.configure(runtime)
    scorers.configure_judges(panel, solutions, assessments)
    try:
        return evalkit_cli.main(evalkit_args(out_dir, task, n, budget.evalkit_usd,
                                             live_judges=live_judges))  # fmt: skip
    finally:
        system.configure(None)
        scorers.configure_judges(None)


class NoSolutions:
    """For a run with no graded-work (B3) cases: the integrity judge is never asked."""

    def solution(self, assessment_id: str) -> str:
        raise KeyError(f"no solution for assessment {assessment_id} (pass --solutions)")


# ---------- the thresholds ----------


@dataclass
class CourseVerdict:
    exit_code: int
    run: str
    split: str = ""
    gates: list[Any] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        from dataclasses import asdict

        return json.dumps(asdict(self), indent=1, sort_keys=True) + "\n"


def evalkit_run_dir(folder: Path) -> Path:
    """The EvalKit run folder: `folder` itself, or the one run under `folder/evalkit/`."""
    if (folder / "run.json").is_file():
        return folder
    found = sorted((folder / "evalkit").glob("*/run.json"))
    if len(found) != 1:
        raise CourseEvalError(f"{folder.name} holds {len(found)} EvalKit run folders (needs one)")
    return found[0].parent


def course_gate(run_dir: Path, *, seed: int = 7) -> CourseVerdict:
    """The §6.4 thresholds on a finished run: 0 every threshold passes, 1 one fails, 2 the
    folder cannot be read or a gate has no case, 3 the run stopped with cases unanswered (cost
    cap), 4 judging is pending. Ids, numbers and gate names only."""
    from evalkit import run as R

    from course_assistant.eval import release

    v = CourseVerdict(exit_code=0, run=run_dir.name)
    try:
        cases = R.read_cases(run_dir)
        records = R.read_records(run_dir) if (run_dir / "records.jsonl").is_file() else []
        summary_path = run_dir / "summary.json"
        summary = (json.loads(summary_path.read_text(encoding="utf-8"))
                   if summary_path.is_file() else None)  # fmt: skip
    except Exception as exc:
        v.exit_code = 2
        v.errors.append(f"not a readable run folder ({type(exc).__name__})")
        return v
    splits = {str(c.meta.get("split", "")) for c in cases.values()}
    v.split = "heldout" if splits == {"heldout"} else "public" if splits == {"tuning"} else "both"
    answered = {r.case_id for r in records}
    if summary is None or answered != set(cases):
        v.exit_code = 3
        v.notes.append(f"the run stopped with {len(set(cases) - answered)} case(s) unanswered "
                       "(cost cap): resume or rerun with a higher --max-cost")  # fmt: skip
        return v
    if not isinstance(summary, dict) or summary.get("judging_status") != "complete":
        v.exit_code = 4
        v.notes.append("judging is pending: collect the judges, then check again")
        return v
    try:
        values = release.case_values(records, cases)
    except release.ReleaseError as exc:
        v.exit_code = 2
        v.errors.append(str(exc))
        return v
    gates, errors = release.gates(values, [r.completion.latency_ms for r in records], seed)
    if errors:
        v.exit_code = 2
        v.errors += errors
        return v
    v.gates = gates
    v.failures = [f"gate {g.name}: {g.value:.3f} (target {g.target})" for g in gates
                  if not g.passed]  # fmt: skip
    v.exit_code = 1 if v.failures else 0
    if v.split != "heldout":
        v.notes.append("not a held-out run: these numbers come from cases you may have tuned "
                       "on, so they cannot clear a change for learners")  # fmt: skip
    if summary.get("judge_mode") == "single":
        v.notes.append("single judge: one vendor grades (OWNER D-80)")
    v.notes.append("thresholds only: judge calibration and the comparison with the last live "
                   "run are not checked here")  # fmt: skip
    return v


FIXTURE_NOTE = ("regression fixture: not shipped in the package; this project's CI checks it "
                "for every released version")  # fmt: skip


@dataclass(frozen=True)
class CourseFiles:
    """Where a course keeps what the release gate reads, beside its config by default."""

    cases: Path  # the held-out cases the run was built from
    public: Path | None  # the tuning split, for the coverage rule
    tuning_log: Path
    decisions: Path
    config_dir: Path

    @classmethod
    def beside(
        cls,
        config: Path,
        cases: Path,
        *,
        public: Path | None = None,
        tuning_log: Path | None = None,
        decisions: Path | None = None,
    ) -> CourseFiles:
        base = config.parent
        return cls(cases=cases, public=public,
                   tuning_log=tuning_log or base / "tuning-log.jsonl",
                   decisions=decisions or base / "decisions.md", config_dir=base)  # fmt: skip


def heldout_problem(files: CourseFiles, bar: float) -> str | None:
    """Why a held-out run must not start, as `scripts/eval_live.py` refuses: a bar or support
    rules change after the held-out split was scored needs a fresh held-out set (§5.4), and the
    bar and ground version in use must be the logged ones."""
    from course_assistant.answer.ground import GroundStep
    from course_assistant.eval import tuning

    log = tuning.read_log(files.tuning_log)
    if tuning.fresh_heldout_required(log):
        return ("the bar or the support rules changed after the held-out split was scored: "
                "write a fresh held-out set (and log heldout_refreshed) first")  # fmt: skip
    if tuning.unlogged(log, bar, GroundStep.version()):
        return (f"a held-out run needs the grounding bar and support rules logged in "
                f"{files.tuning_log.name}")  # fmt: skip
    return None


def course_check(run_dir: Path, *, config: Any, store: Any, assessments: Any,
                 files: CourseFiles, calibration_run: Path | None, baseline_path: Path | None,
                 env: Any) -> Any:  # fmt: skip
    """The release gate (`gate.check_run`, the same code `scripts/release_gate.py` runs) with a
    course's own inputs: its held-out file (every case `heldout`; the run must hold exactly
    these ids), its tuning split, its tuning log and decisions, and its git repository (for
    the append-only and exposure checks; both are skipped outside git, as for this
    repository's own runs without git)."""
    from course_assistant.eval import calibrate, gate

    held, split = load_cases(files.cases)
    if split != "heldout":
        raise CourseEvalError(f"{files.cases.name} is not a held-out file: only a held-out run "
                              "can pass the release gate")  # fmt: skip
    public: list[GoldenCase] = []
    if files.public is not None:
        public, public_split = load_cases(files.public)
        if public_split != "public":
            raise CourseEvalError(f"{files.public.name} is not a tuning-split file")
    decisions = (files.decisions.read_text(encoding="utf-8")
                 if files.decisions.is_file() else "")  # fmt: skip
    log_root = gate.git_root(files.tuning_log.parent.resolve())
    course_root = gate.git_root(files.config_dir.resolve())
    result = gate.check_run(
        run_dir, config=config, store=store, assessments=assessments,
        held=lambda: held, heldout_ids=lambda: frozenset(c.id for c in held),
        public=lambda: public,
        calibration=lambda: calibrate.certified_calibration(calibration_run or run_dir,
                                                            log=files.tuning_log),
        fixture=lambda: [],
        log=lambda bar: gate.log_problems(bar, env, files.tuning_log,
                                          decisions_text=decisions, root=log_root),
        exposure=lambda h: gate.exposure(h, course_root),
        baseline_path=baseline_path,
    )  # fmt: skip
    result.notes.append(FIXTURE_NOTE)
    return result


def render(v: Any) -> str:
    """A `CourseVerdict` (thresholds only) or a release `Verdict`, as fixed lines."""
    what = "course gate" if isinstance(v, CourseVerdict) else "release gate"
    split = v.split if isinstance(v, CourseVerdict) else "heldout"
    lines = [f"{what}: {v.run} ({split or 'unknown split'}) exit {v.exit_code}"]
    for g in v.gates:
        mark = "pass" if g.passed else "FAIL"
        value = f"{g.value:.0f} ms" if g.name == "latency_p95_ms" else f"{g.value:.3f}"
        lines.append(f"  {mark:<4} {g.name}: {value} over {g.n} (target {g.target})")
    lines += [f"  failure: {f}" for f in v.failures]
    lines += [f"  error: {e}" for e in v.errors]
    lines += [f"  note: {n}" for n in v.notes]
    return "\n".join(lines)
