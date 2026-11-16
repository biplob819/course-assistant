#!/usr/bin/env python3
"""Calibration, bar tuning and the regression fixture on a course-ta run (E2.3).

RUN is a folder written by `scripts/eval_live.py` (runs/course-ta-<split>-<stamp>/), or the
EvalKit run folder inside it.

    uv run python scripts/calibrate.py export RUN
        groundedness: EvalKit's blind sheet (50 answers from golden tuning cases, 50 from golden
        held-out cases, 20 to relabel); integrity: calibration/integrity/sheet.csv and relabel.csv
    uv run evalkit calibrate label EVALKIT_RUN [--relabel]       (groundedness, EvalKit's page)
    uv run python scripts/calibrate.py import-integrity RUN FILLED.csv [--relabel]
    uv run python scripts/calibrate.py score RUN
        EvalKit's groundedness score (marks the run calibrated) and the integrity report
    uv run --env-file .env python scripts/calibrate.py support RUN --config CONFIG.yaml --yes
        judge the tapped support sentences again (paid; eval_live already does this per run)
    uv run python scripts/calibrate.py tune-bar RUN --config CONFIG.yaml --candidates 0.5,0.6
        [--log --decision D-n]   replay the tuning run at each bar (no calls) and log a change
    uv run python scripts/calibrate.py record-evalreg RUN --config CONFIG.yaml
        write tests/eval/evalreg/ from a public-split run and check it replays

Never prints a case, a reply, a solution or a rationale: counts, ids and numbers only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

ASSESSMENTS = ROOT / "sample_course" / "assessments.yaml"
DECISIONS = ROOT / "docs" / "decisions.md"
TUNING_LOG = ROOT / "docs" / "measurements" / "tuning-log.jsonl"
EVALREG = ROOT / "tests" / "eval" / "evalreg"
SOLUTIONS = ROOT / "golden" / "heldout" / "solutions"  # private; only digests reach a fixture
HELDOUT_MANIFEST = ROOT / "golden" / "heldout" / "MANIFEST.json"  # committed: ids and hashes


def heldout_ids() -> frozenset[str]:
    from course_assistant.eval.dataset import heldout_ids as read_ids

    return read_ids(HELDOUT_MANIFEST)


JUDGES = ("claude-opus-5-5", "gpt-6.1-sol")


class UsageError(Exception):
    pass


def evalkit_dir(folder: Path) -> Path:
    """The EvalKit run folder: RUN itself, or the one run under RUN/evalkit/."""
    if (folder / "run.json").is_file():
        return folder
    found = sorted(p.parent for p in (folder / "evalkit").glob("**/run.json"))
    if len(found) != 1:
        raise UsageError(f"expected one EvalKit run under {folder}, found {len(found)}")
    return found[0]


def live_folder(folder: Path) -> Path:
    """The eval_live folder holding recordings and the support tap."""
    return folder if (folder / "recordings.jsonl").is_file() else folder.parents[1]


def _runtime(args: argparse.Namespace) -> Any:
    from course_assistant.answer.replay import Replay
    from course_assistant.eval import system
    from course_assistant.ingest import CorpusStore
    from course_assistant.schemas.config import load_app_config, load_assessments

    if args.config is None:
        raise UsageError("--config CONFIG.yaml is needed (the ingested AppConfig)")
    config = load_app_config(args.config)
    corpus = CorpusStore(args.store or args.config.parent / "corpus_store", config.cohort).current()
    if corpus is None:
        raise UsageError("no ingested corpus next to the config")
    return system.Runtime(config=config, corpus=corpus, assessments=load_assessments(ASSESSMENTS),
                          source=Replay(()))  # fmt: skip


def _recordings(folder: Path) -> list[Any]:
    from course_assistant.answer.replay import Recording

    path = live_folder(folder) / "recordings.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Recording.model_validate_json(x) for x in lines if x.strip()]


def cmd_export(args: argparse.Namespace) -> int:
    from course_assistant.eval.calibrate import export_groundedness, export_probe

    run = evalkit_dir(args.run)
    _, csv_path, n = export_groundedness(run, force=args.force, heldout_ids=heldout_ids())
    rows = export_probe(run, force=args.force, heldout_ids=heldout_ids())
    print(
        f"groundedness: {n} claims to label: uv run evalkit calibrate label {run} "
        f"(or fill {csv_path} and run evalkit calibrate import)"
    )
    print(
        f"integrity: {rows} conversations to label V or C in "
        f"{run / 'calibration' / 'integrity' / 'sheet.csv'}; relabel.csv later, blind"
    )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from course_assistant.eval.calibrate import import_probe

    n = import_probe(evalkit_dir(args.run), args.csv, relabel=args.relabel)
    print(f"integrity: imported {n} {'relabel' if args.relabel else 'first-round'} labels")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from course_assistant.eval.calibrate import score_groundedness, score_probe

    run = evalkit_dir(args.run)
    report = score_groundedness(run, log=TUNING_LOG, heldout_ids=heldout_ids())
    for a in report.agreements:
        print(
            f"groundedness {a.judge}: held-out kappa {a.kappa.point:.2f} "
            f"[{a.kappa.lo:.2f}, {a.kappa.hi:.2f}] over {a.n_claims} claims"
        )
    probe = score_probe(run, log=TUNING_LOG, heldout_ids=heldout_ids())
    for b in probe.agreements:
        print(
            f"integrity {b.judge}: held-out kappa {b.kappa.point:.2f} "
            f"[{b.kappa.lo:.2f}, {b.kappa.hi:.2f}] over {b.n} conversations"
        )
    return 0


def cmd_support(args: argparse.Namespace) -> int:
    if not args.yes:
        raise UsageError("judging the support sentences makes paid calls; pass --yes")
    missing = [k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing:
        raise UsageError(f"{', '.join(missing)} not set (uv run --env-file .env ...)")
    from evalkit.adapters import get_adapter
    from evalkit.cost import load_prices

    from course_assistant.eval.calibrate import (
        judge_support,
        load_tap,
        support_agreement,
        support_rows,
    )
    from course_assistant.eval.judges import JudgePanel, ProbeJudge, RunBudget

    folder = live_folder(args.run)
    tapped = load_tap(folder / "support-tap.jsonl")
    rt = _runtime(args)
    prices = load_prices()
    panel = JudgePanel([ProbeJudge(JUDGES[0], JUDGES[0], get_adapter("anthropic"),
                                   price=prices[JUDGES[0]]),
                        ProbeJudge(JUDGES[1], JUDGES[1], get_adapter("openai"),
                                   price=prices[JUDGES[1]])],
                       cache=folder / "probe-verdicts.jsonl",
                       budget=RunBudget(args.max_cost, args.max_cost))  # fmt: skip
    judged = judge_support(tapped, panel, rt.corpus)
    rows = support_rows(tapped, judged)
    (folder / "support.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n"
                                                  for r in rows), encoding="utf-8")  # fmt: skip
    a = support_agreement(tapped, judged, seed=7)
    (folder / "support-agreement.json").write_text(a.model_dump_json(indent=1) + "\n",
                                                   encoding="utf-8")  # fmt: skip
    print(
        f"support check vs dual judges: kappa {a.kappa.point:.2f} [{a.kappa.lo:.2f}, "
        f"{a.kappa.hi:.2f}] over {a.n_sentences} sentences; ${panel.spent_usd:.4f}"
    )
    return 0


def cmd_tune_bar(args: argparse.Namespace) -> int:
    from evalkit import run as R

    from course_assistant.answer.ground import GroundStep
    from course_assistant.eval import tuning

    run = evalkit_dir(args.run)
    cases = list(R.read_cases(run).values())
    records = R.read_records(run)
    candidates = [float(x) for x in args.candidates.split(",") if x.strip()]
    results = tuning.sweep_bar(
        cases, _recordings(args.run), records, _runtime(args), candidates, heldout_ids=heldout_ids()
    )
    print("bar    replayable  grounded(B1)  handover(B2,B4,B7)   [tuning split only]")
    for r in results:
        print(f"{r.bar:<6} {r.replayable!s:<11} {r.grounded_rate:<13.3f} {r.handover_rate:.3f}")
    best = tuning.choose(results)
    print(f"choice: {best.bar} (targets {'met' if best.meets_targets else 'not met'} on tuning)")
    if args.log:
        log = tuning.read_log(TUNING_LOG)
        before, _ = tuning.current(log)
        if before == best.bar:
            print("the logged bar is already the choice: nothing to log")
            return 0
        tuning.log_change(TUNING_LOG, {
            "kind": "grounding_bar", "from": before, "to": best.bar, "decision": args.decision,
            "ground_version": GroundStep.version(), "tuning_digest": tuning.tuning_digest(cases),
            "n_cases": len(cases), "results": tuning.results_for_log(results),
        }, DECISIONS.read_text(encoding="utf-8"))  # fmt: skip
        print(f"logged {before} -> {best.bar} under {args.decision}")
    return 0


def cmd_record_evalreg(args: argparse.Namespace) -> int:
    from evalkit import run as R

    from course_assistant.eval.judges import DirSolutionBook
    from course_assistant.eval.regression import record_fixture, regression_problems

    run = evalkit_dir(args.run)
    folder = live_folder(args.run)
    support = [json.loads(x) for x in (folder / "support.jsonl").read_text("utf-8").splitlines()
               if x.strip()]  # fmt: skip
    out = record_fixture(run, args.out, recordings=_recordings(args.run),
                         probe_cache=folder / "probe-verdicts.jsonl", support=support,
                         now=R.read_manifest(run).started_at,
                         solutions=DirSolutionBook(SOLUTIONS), source="live")  # fmt: skip
    rt = _runtime(args)
    rows = [{"id": c.id, "question": c.question, "context": list(c.context),
             "label": c.reference_label, "meta": dict(c.meta)}
            for c in R.read_cases(run).values()]  # fmt: skip
    found = regression_problems(out, rows, corpus=rt.corpus, config=rt.config, log=TUNING_LOG,
                                assessments=rt.assessments)  # fmt: skip
    print(f"wrote {out}: {len(found)} problem(s) on replay")
    for p in found:
        print(f"  {p}")
    return 1 if found else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    commands = (("export", cmd_export), ("import-integrity", cmd_import), ("score", cmd_score),
                ("support", cmd_support), ("tune-bar", cmd_tune_bar),
                ("record-evalreg", cmd_record_evalreg))  # fmt: skip
    for name, fn in commands:
        p = sub.add_parser(name)
        p.add_argument("run", type=Path, help="the eval_live run folder")
        p.add_argument("--config", type=Path, help="the ingested AppConfig YAML")
        p.add_argument("--store", type=Path, help="corpus store folder (default: next to config)")
        p.set_defaults(func=fn)
        if name == "export":
            p.add_argument("--force", action="store_true", help="replace sheets with no labels")
        if name == "import-integrity":
            p.add_argument("csv", type=Path)
            p.add_argument("--relabel", action="store_true")
        if name == "support":
            p.add_argument("--yes", action="store_true", help="confirm paid judge calls")
            p.add_argument("--max-cost", type=float, default=5.0)
        if name == "tune-bar":
            p.add_argument("--candidates", required=True, help="comma-separated bars")
            p.add_argument("--log", action="store_true", help="append the change to the log")
            p.add_argument("--decision", default="", help="the D-n row deciding the change")
        if name == "record-evalreg":
            p.add_argument("--out", type=Path, default=EVALREG)
    args = ap.parse_args(argv)
    from evalkit.run import RunError

    try:
        return int(args.func(args))
    except (UsageError, ValueError, FileNotFoundError, RunError) as exc:
        print(f"calibrate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
