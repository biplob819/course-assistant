#!/usr/bin/env python3
"""Build the demo replay file (D3.1, RPL-01; run by `make demo-cache`, live, by the owner).

    uv run --env-file .env python scripts/demo_cache.py --config CONFIG.yaml --yes
    uv run python scripts/demo_cache.py --config CONFIG.yaml --dry-run
    uv run python scripts/demo_cache.py --check [demo/replay.json]

CONFIG.yaml is the sample course's AppConfig whose vector store has been filled by
`course-assistant ingest --config CONFIG.yaml` (corpus store next to it by default), on the
separate demo OpenAI project (RPL-03).

The curated public questions (`course_assistant.demo.cache.CURATED`, about 20, at least two
per shown status) run through the real `ask()` with the live `respond` (spend to
runs/ledger.jsonl), each under the guard's per-request hold; their recordings are replayed to
the stored Answers and written to demo/replay.json. A question whose status is not the
curated one is left out and named; the file is written only when two remain per status.
The estimate is printed before any call. Refuses without --yes, without OPENAI_API_KEY, or
when the estimate is over --max-cost. --dry-run prints the plan with no key and no call;
--check replays an existing file offline (exit 1 on a mismatch). Never prints a question or
an answer.
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
ASSESSMENTS = ROOT / "sample_course" / "assessments.yaml"
OUT = ROOT / "demo" / "replay.json"


def public_rows(path: Path = PUBLIC) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def check(path: Path) -> int:
    from course_assistant.demo._errors import DemoCacheError
    from course_assistant.demo.cache import load_cache, verify_cache

    try:
        results = verify_cache(load_cache(path))
    except DemoCacheError as exc:
        print(f"demo_cache: {exc}", file=sys.stderr)
        return 2
    bad = [cid for cid, ok in results if not ok]
    print(f"demo cache: {len(results) - len(bad)}/{len(results)} replay exactly")
    for cid in bad:
        print(f"  mismatch: {cid}")
    return 1 if bad or not results else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", type=Path, help="AppConfig YAML (ingested sample course)")
    ap.add_argument("--store", type=Path, help="corpus store folder (default: next to config)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--max-cost", type=float, default=15.0, help="stop if the estimate is over")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, call nothing")
    ap.add_argument("--yes", action="store_true", help="confirm that this run spends money")
    ap.add_argument("--check", nargs="?", const=OUT, type=Path, metavar="CACHE",
                    help="replay an existing demo cache offline")  # fmt: skip
    args = ap.parse_args(argv)
    if args.check is not None:
        return check(args.check)
    if args.config is None:
        print("demo_cache: --config is required", file=sys.stderr)
        return 2
    from course_assistant.demo.cache import curated_cases, estimate_usd
    from course_assistant.demo.guard import GuardConfig
    from course_assistant.ingest import CorpusStore
    from course_assistant.schemas.config import load_app_config

    cases = curated_cases(public_rows())
    hold = GuardConfig().request_hold_usd
    cost = estimate_usd(len(cases), hold)
    print(f"demo cache: {len(cases)} curated public questions -> {args.out}")
    print(f"estimate: at most ${cost:.2f} (${hold:.2f} per question; cap ${args.max_cost:.2f})")
    if cost > args.max_cost:
        print("demo_cache: estimate over --max-cost", file=sys.stderr)
        return 3
    # the estimate is printed first; only then are --yes and the key required (pass 1)
    if not args.dry_run:
        if not args.yes:
            print("demo_cache: this run makes paid API calls; pass --yes to confirm",
                  file=sys.stderr)  # fmt: skip
            return 2
        if not os.environ.get("OPENAI_API_KEY"):
            print("demo_cache: OPENAI_API_KEY is not set (the demo project's key, "
                  "uv run --env-file .env ...)", file=sys.stderr)  # fmt: skip
            return 2
    config = load_app_config(args.config)
    if args.dry_run:
        return 0
    corpus = CorpusStore(args.store or args.config.parent / "corpus_store", config.cohort).current()
    if corpus is None:
        print(f"demo_cache: no corpus; run course-assistant ingest --config {args.config} "
              "first", file=sys.stderr)  # fmt: skip
        return 2
    return _run(args, config, corpus, cases, hold)


def _run(args: argparse.Namespace, config: Any, corpus: Any, cases: list[Any],
         hold: float) -> int:  # fmt: skip
    import openai

    from course_assistant.answer import responder
    from course_assistant.answer.replay import Live
    from course_assistant.demo._errors import DemoCacheError
    from course_assistant.demo.cache import build_cache, dumps
    from course_assistant.demo.guard import CappedSource
    from course_assistant.llm import JsonlSink, Ledger, load_prices
    from course_assistant.schemas.config import load_assessments

    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    prices = load_prices()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    live = responder(client=openai.OpenAI(), prices=prices,
                     ledger=Ledger(runs / "ledger.jsonl", config.monthly_budget_usd),
                     sink=JsonlSink(runs / f"demo-cache-{stamp}.trace.jsonl"),
                     run_id=f"demo-cache-{stamp}")  # fmt: skip
    try:
        fx, report = build_cache(cases, config=config, corpus=corpus,
                                 assessments=load_assessments(ASSESSMENTS),
                                 source_for=lambda _c: CappedSource(Live(live), hold, prices),
                                 now=dt.datetime.now(dt.UTC))  # fmt: skip
    except DemoCacheError as exc:
        report = getattr(exc, "report", None)
        if report is not None:
            print(report.render())
        print(f"demo_cache: {exc}; nothing written. Adjust CURATED and rerun.", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(dumps(fx), encoding="utf-8")
    print(report.render())
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
