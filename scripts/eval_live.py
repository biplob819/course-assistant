#!/usr/bin/env python3
"""Live golden run of the EvalKit task `course-ta` (built in E2.2, run in E2.5, by the owner).

    uv run --env-file .env python scripts/eval_live.py --config CONFIG.yaml --split heldout --yes
    uv run python scripts/eval_live.py --config CONFIG.yaml --split public --dry-run

CONFIG.yaml is an AppConfig whose vector store has been filled by
`course-assistant ingest --config CONFIG.yaml` (corpus store next to it by default).

What it does, under runs/course-ta-<split>-<stamp>/ (git-ignored):
1. builds the EvalKit cases from the golden split (public, held-out or both; held-out is read
   only if it matches golden/heldout/MANIFEST.json) with the corpus units as context, and
   writes cases.jsonl, course-ta.yaml and an evalkit.yaml (system `course-ta`, judges
   claude-opus-5-5 and gpt-6.1-sol, extractor claude-haiku-5-5);
2. installs the runtime (live `respond` through a `Recorder`, spend to runs/ledger.jsonl) and
   the dual probe judges (EvalKit's Anthropic and OpenAI adapters; solutions read from
   golden/heldout/solutions/ by the integrity judge only), then runs `evalkit run`;
3. saves the recordings, so `--replay RECORDINGS` reruns the system offline (judges are still
   live, but cached verdicts are reused);
4. grades the runtime support check against the dual judges (E2.3 CAL-03): every sentence the
   L2 check judged is tapped, judged by both judges with EvalKit's groundedness rubric, and
   `support-agreement.json` (kappa with a CI) is written next to `support.jsonl`.

One cap for the whole run (`--max-cost`, E2.3): the probe judges get a share sized from the
probe count and reserve every call before making it; EvalKit gets the rest as its own
`--max-cost`. The golden corpus version is checked against the ingested corpus, and the
config's bar and the code's ground version against docs/measurements/tuning-log.jsonl
(`--allow-unlogged` is for the tuning run itself).

Refuses without --yes and both API keys. --dry-run writes the folder and prints the case count
and the estimate, with no key and no call. Never prints a case, a reply or a solution.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PUBLIC = ROOT / "golden" / "public" / "cases.jsonl"
HELDOUT = ROOT / "golden" / "heldout"
SOLUTIONS = HELDOUT / "solutions"
MANIFEST = ROOT / "sample_course" / "manifest.yaml"
ASSESSMENTS = ROOT / "sample_course" / "assessments.yaml"
JUDGES = ("claude-opus-5-5", "gpt-6.1-sol")
EXTRACTOR = "claude-haiku-5-5"
SYSTEM_ALIAS = "course-ta"
JUDGE_USD_PER_PROBE = 0.04  # two judges, one probe (planning figure, replaced in E2.5)
EVALKIT_USD_PER_CASE = 0.03  # extractor plus two groundedness judges per answer (planning)
SUPPORT_USD_PER_ANSWER = 0.02  # two judges on an answer's few L2 sentences (planning)
PANEL_HEADROOM = 2.0  # the judges' share covers a re-ask of every probe
TUNING_LOG = ROOT / "docs" / "measurements" / "tuning-log.jsonl"


def evalkit_config(seed: int = 7) -> str:
    return (f"models: [{SYSTEM_ALIAS}]\nsystems: {{{SYSTEM_ALIAS}: course_ta}}\n"
            f"judges: [{', '.join(JUDGES)}]\nextractor: {EXTRACTOR}\nseed: {seed}\n")  # fmt: skip


def select(split: str, heldout_dir: Path = HELDOUT) -> list[Any]:
    from course_assistant.eval.dataset import load_heldout, load_public

    if split == "public":
        return load_public(PUBLIC)
    if split == "heldout":
        return load_heldout(heldout_dir)
    return load_public(PUBLIC) + load_heldout(heldout_dir)


def probes(cases: list[Any]) -> int:
    """Cases a probe judge reads: B3 (integrity), B9 (identity), B8 and B10 (leak)."""
    return sum(c.rule in ("B3", "B8", "B9", "B10") for c in cases)


def evalkit_estimate(cases: list[Any]) -> float:
    from course_assistant.eval.system import PER_CASE_USD

    return len(cases) * (PER_CASE_USD + EVALKIT_USD_PER_CASE)


def panel_estimate(cases: list[Any]) -> float:
    answered = sum(c.expected_status == "answered" for c in cases)
    return (probes(cases) * JUDGE_USD_PER_PROBE * PANEL_HEADROOM
            + answered * SUPPORT_USD_PER_ANSWER)  # fmt: skip


def estimate(cases: list[Any]) -> float:
    return evalkit_estimate(cases) + panel_estimate(cases)


def plan_budget(cases: list[Any], max_cost: float) -> Any:
    """One cap for the run: the judges' share sized from the probes, EvalKit the rest. Refused
    when either share would be below its estimate."""
    from course_assistant.eval.judges import RunBudget

    panel = panel_estimate(cases)
    if panel + evalkit_estimate(cases) > max_cost:
        raise ValueError(f"the estimate ${estimate(cases):.2f} is over the cap ${max_cost:.2f}")
    return RunBudget(max_cost, panel)


def prepare(out_dir: Path, cases: list[Any], corpus: Any) -> Path:
    """Write the task folder and evalkit.yaml; return the task YAML path. The golden version
    is accepted only for the corpus that was ingested (E2.3)."""
    from course_assistant.eval.dataset import build_rows, golden_version_for, write_task_dir
    from course_assistant.schemas.config import load_manifest

    rows = build_rows(cases, corpus, golden_version_for(corpus, load_manifest(MANIFEST), ROOT))
    task = write_task_dir(out_dir, rows)
    (out_dir / "evalkit.yaml").write_text(evalkit_config(), encoding="utf-8")
    return task


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, type=Path, help="AppConfig YAML (ingested)")
    ap.add_argument("--store", type=Path, help="corpus store folder (default: next to config)")
    ap.add_argument("--split", choices=("public", "heldout", "both"), default="heldout")
    ap.add_argument("--max-cost", type=float, default=40.0, help="stop if the estimate is over")
    ap.add_argument("--replay", type=Path, help="recordings.jsonl from an earlier live run")
    ap.add_argument("--out", type=Path, default=ROOT / "runs")
    ap.add_argument("--dry-run", action="store_true", help="write the folder, call nothing")
    ap.add_argument("--allow-unlogged", action="store_true",
                    help="run with a bar or support rules not in the tuning log (tuning runs)")
    ap.add_argument("--yes", action="store_true", help="confirm that this run spends money")
    args = ap.parse_args(argv)
    if not args.dry_run:
        if not args.yes:
            print("eval_live: this run makes paid API calls; pass --yes to confirm",
                  file=sys.stderr)  # fmt: skip
            return 2
        missing = [k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
        if missing:
            print(f"eval_live: {', '.join(missing)} not set (uv run --env-file .env ...)",
                  file=sys.stderr)  # fmt: skip
            return 2

    from course_assistant.answer.ground import GroundStep
    from course_assistant.eval.tuning import read_log, unlogged
    from course_assistant.ingest import CorpusStore
    from course_assistant.schemas.config import load_app_config

    config = load_app_config(args.config)
    corpus = CorpusStore(args.store or args.config.parent / "corpus_store", config.cohort).current()
    if corpus is None:
        print(f"eval_live: no corpus; run course-assistant ingest --config {args.config} first",
              file=sys.stderr)  # fmt: skip
        return 2
    drift = unlogged(read_log(TUNING_LOG), config.grounding_bar, GroundStep.version())
    for line in drift:
        print(f"eval_live: {line}", file=sys.stderr)
    cases = select(args.split)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out / f"course-ta-{args.split}-{stamp}"
    task = prepare(out_dir, cases, corpus)
    cost = estimate(cases)
    print(f"course-ta: {len(cases)} cases ({probes(cases)} probes) -> {out_dir}")
    print(f"estimate: ${cost:.2f} (cap ${args.max_cost:.2f})")
    if args.dry_run:
        return 0
    if drift and not args.allow_unlogged:
        print("eval_live: log the change first (calibrate.py tune-bar --log), or pass "
              "--allow-unlogged for a tuning run", file=sys.stderr)  # fmt: skip
        return 2
    try:
        budget = plan_budget(cases, args.max_cost)
    except ValueError as exc:
        print(f"eval_live: {exc}", file=sys.stderr)
        return 3
    return _run(args, config, corpus, out_dir, task, len(cases), budget)


def _run(args: argparse.Namespace, config: Any, corpus: Any, out_dir: Path, task: Path,
         n: int, budget: Any) -> int:  # fmt: skip
    import openai
    from evalkit import cli as evalkit_cli
    from evalkit.adapters import get_adapter
    from evalkit.cost import load_prices as evalkit_prices

    from course_assistant.answer import responder
    from course_assistant.answer.replay import Recorder, Recording, Replay
    from course_assistant.eval import scorers, system
    from course_assistant.eval.calibrate import (
        SupportTap,
        judge_support,
        support_agreement,
        support_rows,
    )
    from course_assistant.eval.judges import DirSolutionBook, JudgePanel, ProbeJudge
    from course_assistant.llm import JsonlSink, Ledger, load_prices
    from course_assistant.schemas.config import load_assessments

    assessments = load_assessments(ASSESSMENTS)
    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    ledger = Ledger(runs / "ledger.jsonl", config.monthly_budget_usd)
    sink = JsonlSink(out_dir / "trace.jsonl")
    recorder: Recorder | None = None
    if args.replay:
        rows = args.replay.read_text(encoding="utf-8").splitlines()
        source: Any = Replay(Recording.model_validate_json(x) for x in rows if x.strip())
    else:
        live = responder(client=openai.OpenAI(), prices=load_prices(), ledger=ledger, sink=sink,
                         run_id=out_dir.name)  # fmt: skip
        recorder = Recorder(live)
        source = recorder
    tap = SupportTap(source)
    system.configure(system.Runtime(config=config, corpus=corpus, assessments=assessments,
                                    source=tap))  # fmt: skip
    prices = evalkit_prices()
    panel = JudgePanel([ProbeJudge(JUDGES[0], JUDGES[0], get_adapter("anthropic"),
                                   price=prices[JUDGES[0]]),
                        ProbeJudge(JUDGES[1], JUDGES[1], get_adapter("openai"),
                                   price=prices[JUDGES[1]])],
                       cache=out_dir / "probe-verdicts.jsonl", budget=budget)  # fmt: skip
    scorers.configure_judges(panel, DirSolutionBook(SOLUTIONS), assessments)
    try:
        code = evalkit_cli.main(["run", str(out_dir / "evalkit.yaml"), "--task", str(task),
                                 "--n", str(n), "--out", str(out_dir / "evalkit"),
                                 "--max-cost", f"{budget.evalkit_usd:.4f}"])  # fmt: skip
        tap.save(out_dir / "support-tap.jsonl")  # private: holds answer sentences
        rows = support_rows(tap.sentences, judge_support(tap.sentences, panel, corpus))
        (out_dir / "support.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
        if rows:
            agreement = support_agreement(tap.sentences, {(r["request_id"], r["n"]): r["judges"]
                                                          for r in rows}, seed=7)  # fmt: skip
            (out_dir / "support-agreement.json").write_text(agreement.model_dump_json(indent=1)
                                                            + "\n", encoding="utf-8")  # fmt: skip
            print(f"support check vs dual judges: kappa {agreement.kappa.point:.2f} "
                  f"[{agreement.kappa.lo:.2f}, {agreement.kappa.hi:.2f}] over "
                  f"{agreement.n_sentences} sentences")  # fmt: skip
    finally:
        if recorder is not None:
            (out_dir / "recordings.jsonl").write_text(
                "".join(r.model_dump_json() + "\n" for r in recorder.recordings),
                encoding="utf-8")  # fmt: skip
        summary = {"probe_judge_calls": panel.calls, "probe_judge_usd": panel.spent_usd}
        (out_dir / "probe-judges.json").write_text(json.dumps(summary, indent=1) + "\n",
                                                   encoding="utf-8")  # fmt: skip
    print(f"probe judges: {panel.calls} calls, ${panel.spent_usd:.4f}")
    return code


if __name__ == "__main__":
    sys.exit(main())
