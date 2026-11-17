#!/usr/bin/env python3
"""The E2.5 full run report (FRN-01 to FRN-03): no API call, ids and numbers only.

    uv run python scripts/full_run_report.py \\
        --run sol-low=runs/course-ta-heldout-<stamp> --run luna=runs/course-ta-heldout-<stamp> \\
        --reference sol-low --smoke sol-low=runs/smoke-sol-low-<stamp>.json \\
        --write docs/measurements/e2.5-report.md

Each `--run LABEL=FOLDER` is an eval_live folder (`LABEL=FOLDER#heldout` or `#tuning` takes
one golden split of a run on both splits, D-67); its cost comes from runs/ledger.jsonl rows
with the folder's name as run id (system calls, plus the judging lines eval_live appends).
Runs are compared pairwise (first minus each later one, paired bootstrap with Holm over shared
cases), `fullrun.choose` applies FRN-02 with `--reference`, and `--questions` and `--runs`
project a month (FRN-03). Never prints a case, a reply or a solution.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def pair(value: str) -> tuple[str, Path]:
    label, sep, path = value.partition("=")
    if not sep or not label or not path:
        raise argparse.ArgumentTypeError("expected LABEL=PATH")
    return label, Path(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--run", type=pair, action="append", default=[], help="LABEL=eval_live folder")
    ap.add_argument("--smoke", type=pair, action="append", default=[], help="LABEL=smoke json")
    ap.add_argument("--reference", help="setting the FRN-02 rule compares quality against")
    ap.add_argument(
        "--compare",
        action="append",
        default=[],
        help="A:B to compare run A minus run B (default: the first minus each other)",
    )
    ap.add_argument("--ledger", type=Path, default=ROOT / "runs" / "ledger.jsonl")
    ap.add_argument("--questions", type=int, default=1000, help="questions a month (FRN-03)")
    ap.add_argument("--runs", type=int, default=6, help="full runs a month (FRN-03)")
    ap.add_argument("--cost-from", help="smoke LABEL whose ledger cost per question to project")
    ap.add_argument("--run-usd", type=float, help="cost of one full run, when no --run gives it")
    ap.add_argument("--note", action="append", default=[], help="a line for the Notes section")
    ap.add_argument("--write", type=Path, help="write the report here")
    args = ap.parse_args(argv)

    from course_assistant.eval import fullrun as F

    rows = F.read_ledger(args.ledger)
    summaries = []
    for label, path in args.run:
        folder, _, split = str(path).partition("#")
        summaries.append(F.run_summary(Path(folder), rows, label, split or None))
    smoke = [F.smoke_row(path, label, rows) for label, path in args.smoke]
    differences = {}
    by_label = {s.label: s for s in summaries}
    pairs = []
    for spec in args.compare:
        a, sep, b = spec.partition(":")
        if not sep or a not in by_label or b not in by_label:
            ap.error(f"--compare {spec}: expected A:B naming two --run labels")
        pairs.append((by_label[a], by_label[b]))
    if not args.compare:
        pairs = [(summaries[0], other) for other in summaries[1:]]
    for first, other in pairs:
        title = f"{first.label} minus {other.label} (paired, Holm)"
        differences[title] = F.compare(first, other)
    choice = None
    if args.reference and summaries:
        choice = F.choose([F.Candidate(s.label, s.latency_p95_ms, s.quality(),
                                       s.cost_per_grounded_usd) for s in summaries],
                          args.reference)  # fmt: skip
    notes = list(args.note)
    basis: tuple[str, float, float] | None = None
    if summaries:
        s = summaries[0]
        basis = (s.label, s.cost_per_question_usd, s.total_usd)
    elif args.cost_from and args.run_usd is not None:
        row = next((r for r in smoke if r.label == args.cost_from), None)
        if row is None:
            ap.error(f"--cost-from {args.cost_from}: no such --smoke label")
        basis = (row.label, row.mean_usd, args.run_usd)
    if basis is not None:
        label, per_question, run_usd = basis
        model = F.cost_model(per_question, args.questions, run_usd, args.runs)
        notes.append(f"Cost model ({label}): {args.questions} questions a month at "
                     f"${model.per_question_usd:.4f} = ${model.answer_path_usd:.2f}; {args.runs} "
                     f"full runs at ${model.run_usd:.2f} = ${model.runs_usd:.2f}; month "
                     f"${model.monthly_usd:.2f} against the ${model.cap_usd:.0f} cap "
                     f"({'within' if model.within_cap else 'OVER'}; alarm at "
                     f"${F.ALARM_SHARE * model.cap_usd:.0f} "
                     f"{'not reached' if model.under_alarm else 'reached'}).")  # fmt: skip
    text = F.render_report(summaries, differences, choice, notes, smoke)
    print(text, end="")
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
