#!/usr/bin/env python3
"""Live golden run of the EvalKit task `course-ta` (built in E2.2, run in E2.5, by the owner).

    uv run --env-file .env python scripts/eval_live.py --config CONFIG.yaml --split heldout --yes
    uv run python scripts/eval_live.py --config CONFIG.yaml --split public --dry-run

CONFIG.yaml is an AppConfig whose vector store has been filled by
`course-assistant ingest --config CONFIG.yaml` (corpus store next to it by default).

What it does, under runs/course-ta-<split>-<stamp>/ (git-ignored):
1. builds the EvalKit cases from the golden split (public, held-out or both; held-out is read
   only if it matches golden/heldout/MANIFEST.json) with the corpus units as context, and
   writes cases.jsonl, course-ta.yaml and an evalkit.yaml (system `course-ta`, judge
   gpt-6.1-sol, extractor gpt-6-luna: OWNER D-80; until then the dual judges claude-opus-5-5
   and gpt-6.1-sol with extractor claude-haiku-5-5);
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

Refuses without --yes and the API keys its judges and extractor need. --dry-run writes the
folder and prints the case count and the estimate, with no key and no call.
Never prints a case, a reply or a solution.
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
# the panel, the cost figures and the run pieces are the packaged ones (`course-assistant
# eval`, R3.1), so this script and a course team's own evaluation price and run the same way
from course_assistant.eval.course import (  # noqa: E402
    EVALKIT_USD_PER_CASE,
    JUDGE_SHARE,
    JUDGE_USD_PER_PROBE,
    MEASURED_E25,
    PANEL_HEADROOM,
    SUPPORT_USD_PER_ANSWER,
    SYSTEM_ALIAS,
    estimate,
    evalkit_args,
    evalkit_config,
    evalkit_estimate,
    panel_estimate,
    plan_budget,
    probes,
)
from course_assistant.eval.panel import EXTRACTOR, JUDGES  # noqa: E402  OWNER D-80

TUNING_LOG = ROOT / "docs" / "measurements" / "tuning-log.jsonl"
__all__ = ["EVALKIT_USD_PER_CASE", "EXTRACTOR", "JUDGES", "JUDGE_SHARE", "JUDGE_USD_PER_PROBE",
           "MEASURED_E25", "PANEL_HEADROOM", "SUPPORT_USD_PER_ANSWER", "SYSTEM_ALIAS",
           "estimate", "evalkit_args", "evalkit_config", "evalkit_estimate", "panel_estimate",
           "plan_budget", "probes"]  # fmt: skip


def select(split: str, heldout_dir: Path = HELDOUT) -> list[Any]:
    from course_assistant.eval.dataset import load_heldout, load_public

    if split == "public":
        return load_public(PUBLIC)
    if split == "heldout":
        return load_heldout(heldout_dir)
    return load_public(PUBLIC) + load_heldout(heldout_dir)


RULE_NAMES = ("B1", "B2", "B3", "B4", "B7", "B8", "B9", "B10")


def subset(cases: list[Any], split: str, rules: str | None) -> list[Any]:
    """The cases of the named rules ("B1" or "B1,B7"), for a cheap tuning run on the public
    split (OWNER D-71). A held-out run is always the whole set: a subset is refused."""
    if rules is None:
        return cases
    if split != "public":
        raise ValueError("--rules is for the public split only (the held-out set runs whole)")
    wanted = {r.strip() for r in rules.split(",") if r.strip()}
    unknown = sorted(wanted - set(RULE_NAMES))
    if unknown or not wanted:
        raise ValueError(f"unknown rule(s) {unknown}; use {', '.join(RULE_NAMES)}")
    return [c for c in cases if c.rule in wanted]


def heldout_gate(split: str, log_path: Path = TUNING_LOG) -> str | None:
    """Why a held-out run must not start, or None: after the bar or the support rules changed
    following a held-out score, a fresh held-out set comes first (Tech Spec §5.4)."""
    from course_assistant.eval.tuning import fresh_heldout_required, read_log

    if split in ("heldout", "both") and fresh_heldout_required(read_log(log_path)):
        return ("the bar or the support rules changed after the held-out split was scored: "
                "write a fresh held-out set (and log heldout_refreshed) first")  # fmt: skip
    return None


def unlogged_gate(split: str, drift: list[str], *, allow_unlogged: bool) -> str | None:
    """`--allow-unlogged` is for tuning runs only: a held-out run always needs a logged bar
    and ground version."""
    if not drift:
        return None
    if split in ("heldout", "both"):
        return "a held-out run needs the bar and support rules logged (no --allow-unlogged)"
    if not allow_unlogged:
        return ("log the change first (calibrate.py tune-bar --log), or pass --allow-unlogged "
                "for a tuning run")  # fmt: skip
    return None


def logs_heldout_score(split: str, *, replay: bool, resume: bool = False) -> bool:
    """Every run that scores the held-out split logs it, a replayed one too; a resumed run
    (collecting pending judge batches, E2.5) was logged when it first ran."""
    del replay  # a replay scores the held-out split just the same
    return split in ("heldout", "both") and not resume


def resume_inputs(folder: Path) -> tuple[Path, int, str]:
    """(task YAML, live case count, split) of an eval_live folder to resume (EvalKit exit 3
    or 4: rerun to finish or collect batches). The folder's own cases are reused as written."""
    task, cases = folder / "course-ta.yaml", folder / "cases.jsonl"
    if not (task.is_file() and cases.is_file() and (folder / "drift.json").is_file()):
        raise ValueError(f"{folder.name} is not an eval_live folder (task, cases, drift.json)")
    # cases.jsonl holds the run's cases after drift (retired ones are already left out)
    n = sum(1 for x in cases.read_text(encoding="utf-8").splitlines() if x.strip())
    parts = folder.name.split("-")
    split = parts[2] if len(parts) >= 4 and parts[:2] == ["course", "ta"] else ""
    if split not in ("public", "heldout", "both"):
        raise ValueError(f"{folder.name}: cannot tell the split from the folder name")
    return task, n, split


def merge_probe_summary(previous: dict[str, Any], calls: int, usd: float) -> dict[str, Any]:
    """The probe panel's totals across a run and its resumes."""
    return {"probe_judge_calls": int(previous.get("probe_judge_calls", 0)) + calls,
            "probe_judge_usd": float(previous.get("probe_judge_usd", 0.0)) + usd}  # fmt: skip


def prepare(out_dir: Path, cases: list[Any], corpus: Any) -> Path:
    """The packaged `course.prepare` with the sample course's manifest, from the repo root:
    drift first, then the task folder, drift.json and evalkit.yaml (E2.3, E2.4 GAT-04)."""
    from course_assistant.eval import course
    from course_assistant.schemas.config import load_manifest

    return course.prepare(out_dir, cases, corpus, load_manifest(MANIFEST), ROOT)


class _Resumed(Exception):
    """A resume with nothing new to tap: skip the support re-judging, keep the files."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def ledger_judging(ledger: Any, out_dir: Path) -> tuple[float, float]:
    """Append the run's judging spend to the ledger (E2.5), so every live dollar of a run is
    in runs/ledger.jsonl under its run id: EvalKit's extraction and judging (its spend less the
    system costs already ledgered call by call) and the probe judges. Only what the ledger does
    not hold yet for this run id is added, so a resumed run never ledgers twice. Returns the
    amounts added; nothing is written for a run with no EvalKit folder yet."""
    from course_assistant.eval.fullrun import FullRunError, judging_spend, read_ledger, spend_of

    try:
        evalkit_total, probe_total = judging_spend(out_dir)
    except FullRunError:
        return 0.0, 0.0
    held = spend_of(read_ledger(Path(ledger.path)), out_dir.name)
    evalkit = max(0.0, evalkit_total - held.get("eval_judging", 0.0))
    probe = max(0.0, probe_total - held.get("probe_judging", 0.0))
    evalkit = 0.0 if evalkit < 1e-9 else evalkit
    probe = 0.0 if probe < 1e-9 else probe
    for purpose, cost in (("eval_judging", evalkit), ("probe_judging", probe)):
        if cost > 0:
            ledger.append(purpose=purpose, model="unknown", tokens_in=0, tokens_cached=0,
                          tokens_out=0, file_search_calls=0, cost_usd=cost,
                          run_id=out_dir.name)  # fmt: skip
    return evalkit, probe


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", required=True, type=Path, help="AppConfig YAML (ingested)")
    ap.add_argument("--store", type=Path, help="corpus store folder (default: next to config)")
    ap.add_argument("--split", choices=("public", "heldout", "both"), default="heldout")
    ap.add_argument("--max-cost", type=float, default=40.0, help="stop if the estimate is over")
    ap.add_argument("--replay", type=Path, help="recordings.jsonl from an earlier live run")
    ap.add_argument("--resume", type=Path,
                    help="an eval_live folder whose EvalKit run stopped (exit 3 or 4): finish it "
                    "in place, reusing its cases (no new held-out log line)")  # fmt: skip
    ap.add_argument("--out", type=Path, default=ROOT / "runs")
    ap.add_argument("--dry-run", action="store_true", help="write the folder, call nothing")
    ap.add_argument(
        "--allow-unlogged",
        action="store_true",
        help="run with a bar or support rules not in the tuning log (tuning runs)",
    )
    ap.add_argument("--yes", action="store_true", help="confirm that this run spends money")
    ap.add_argument("--live-judges", action="store_true",
                    help="judge at full price now instead of provider batches")  # fmt: skip
    ap.add_argument(
        "--fresh-store",
        action="store_true",
        help="give EvalKit an empty cache in the run folder, so every case is "
        "generated and judged again (D-89: a rerun after a provider outage)",
    )
    ap.add_argument("--rules", help="public tuning runs only: just these rules, e.g. B1 or B1,B7")
    args = ap.parse_args(argv)
    if args.rules is not None and (args.split != "public" or args.resume is not None):
        print("eval_live: --rules is for the public split only (the held-out set runs whole)",
              file=sys.stderr)  # fmt: skip
        return 2
    if not args.dry_run:
        if not args.yes:
            print("eval_live: this run makes paid API calls; pass --yes to confirm",
                  file=sys.stderr)  # fmt: skip
            return 2
        from course_assistant.eval.panel import needed_keys

        need = needed_keys(JUDGES, EXTRACTOR)
        missing = sorted(k for k in need if not os.environ.get(k))
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
    if args.resume is not None:
        try:
            task, n_live, args.split = resume_inputs(args.resume)
        except ValueError as exc:
            print(f"eval_live: {exc}", file=sys.stderr)
            return 2
        out_dir = args.resume
        cases = select(args.split)
    else:
        try:
            cases = subset(select(args.split), args.split, args.rules)
        except ValueError as exc:
            print(f"eval_live: {exc}", file=sys.stderr)
            return 2
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        out_dir = args.out / f"course-ta-{args.split}-{stamp}"
        task = prepare(out_dir, cases, corpus)
        n_live = -1
    cost = estimate(cases, replay=bool(args.replay))
    print(f"course-ta: {len(cases)} cases ({probes(cases)} probes) -> {out_dir}")
    print(f"estimate: ${cost:.2f} (cap ${args.max_cost:.2f})")
    if args.dry_run:
        return 0
    gate = None if args.resume is not None else heldout_gate(args.split)
    if gate is not None:
        print(f"eval_live: {gate}", file=sys.stderr)
        return 2
    refusal = unlogged_gate(args.split, drift, allow_unlogged=args.allow_unlogged)
    if refusal is not None:
        print(f"eval_live: {refusal}", file=sys.stderr)
        return 2
    try:
        budget = plan_budget(cases, args.max_cost, replay=bool(args.replay))
    except ValueError as exc:
        print(f"eval_live: {exc}", file=sys.stderr)
        return 3
    if n_live < 0:
        retired = json.loads((out_dir / "drift.json").read_text(encoding="utf-8"))["retired"]
        n_live = len(cases) - len(retired)
    code = _run(args, config, corpus, out_dir, task, n_live, budget)
    if logs_heldout_score(args.split, replay=bool(args.replay), resume=args.resume is not None):
        from course_assistant.eval.tuning import log_heldout_scored

        log_heldout_scored(TUNING_LOG, run=out_dir.name, bar=config.grounding_bar,
                           ground_version=GroundStep.version())  # fmt: skip
    return code


def write_recordings(path: Path, new: Any, *, resume: bool) -> None:
    """The run's recordings. A resume adds its calls to the first pass's instead of replacing
    them (the first single-judge resume left 14 of the run's calls), so `--replay` still
    reruns the whole run."""
    rows = [r.model_dump_json() for r in new]
    if resume and path.is_file():
        rows = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] + rows
    path.write_text("".join(x + "\n" for x in rows), encoding="utf-8")


def _run(args: argparse.Namespace, config: Any, corpus: Any, out_dir: Path, task: Path,
         n: int, budget: Any) -> int:  # fmt: skip
    import openai
    from evalkit import cli as evalkit_cli
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
    from course_assistant.eval.judges import DirSolutionBook, build_panel
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
                                    source=tap, replay=bool(args.replay)))  # fmt: skip
    prices = evalkit_prices()
    panel = build_panel(JUDGES, prices,
                       cache=out_dir / "probe-verdicts.jsonl", budget=budget)  # fmt: skip
    scorers.configure_judges(panel, DirSolutionBook(SOLUTIONS), assessments)
    try:
        own = args.fresh_store or (out_dir / "evalkit-store").is_dir()  # a resume keeps it
        code = evalkit_cli.main(evalkit_args(out_dir, task, n, budget.evalkit_usd,
                                             live_judges=args.live_judges,
                                             fresh_store=own))  # fmt: skip
        if args.resume is not None and not tap.sentences:
            # a resume that answered nothing new keeps the first pass's tap, support rows and
            # agreement as they are
            raise _Resumed(code)
        tap.save(out_dir / "support-tap.jsonl")  # private: holds answer sentences
        rows = support_rows(tap.sentences, judge_support(tap.sentences, panel, corpus))
        (out_dir / "support.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
        )
        if rows:
            agreement = support_agreement(tap.sentences, {(r["request_id"], r["n"]): r["judges"]
                                                          for r in rows}, seed=7)  # fmt: skip
            (out_dir / "support-agreement.json").write_text(agreement.model_dump_json(indent=1)
                                                            + "\n", encoding="utf-8")  # fmt: skip
            print(f"support check vs dual judges: kappa {agreement.kappa.point:.2f} "
                  f"[{agreement.kappa.lo:.2f}, {agreement.kappa.hi:.2f}] over "
                  f"{agreement.n_sentences} sentences")  # fmt: skip
    except _Resumed as done:
        code = done.code
    finally:
        if recorder is not None and not (args.resume is not None and not recorder.recordings):
            write_recordings(out_dir / "recordings.jsonl", recorder.recordings,
                             resume=args.resume is not None)  # fmt: skip
        previous: dict[str, Any] = {}
        summary_file = out_dir / "probe-judges.json"
        if args.resume is not None and summary_file.is_file():
            previous = json.loads(summary_file.read_text(encoding="utf-8"))
        summary = merge_probe_summary(previous, panel.calls, panel.spent_usd)
        (out_dir / "probe-judges.json").write_text(json.dumps(summary, indent=1) + "\n",
                                                   encoding="utf-8")  # fmt: skip
        judged, probed = ledger_judging(ledger, out_dir)
        print(f"judging ledgered: EvalKit ${judged:.4f}, probe judges ${probed:.4f}")
    print(f"probe judges: {panel.calls} calls, ${panel.spent_usd:.4f}")
    return code


if __name__ == "__main__":
    sys.exit(main())
