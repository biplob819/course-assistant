#!/usr/bin/env python3
"""Live route run for A2.1 (RTE-04): every route fixture (the public golden split plus the
fixture-only top-ups, D-36) through the real route step, with per-category recall.

    OPENAI_API_KEY=... uv run python scripts/route_live.py [--limit N] [--workers 8]

Writes runs/route-live-<stamp>.json (git-ignored) with each case's expected and actual
category, and prints the per-category table and the misroutes. Spend goes to the ledger at
runs/ledger.jsonl. Held-out cases are never read.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from course_assistant.answer import CATEGORIES, RouteStep, responder  # noqa: E402
from course_assistant.llm import Ledger, LLMError, load_prices  # noqa: E402
from course_assistant.schemas.config import AppConfig, load_assessments  # noqa: E402

FIXTURES = ROOT / "tests" / "answer" / "fixtures" / "routes.jsonl"
CHECK = "gpt-6-luna"
TARGET = {"graded_work": 0.95, "admin": 0.95, "personal": 0.95, "injection": 0.95}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    import openai

    rows = [json.loads(x) for x in FIXTURES.read_text(encoding="utf-8").splitlines() if x]
    if args.limit:
        rows = rows[: args.limit]
    config = AppConfig(schema_version="1.0", cohort="sample", vector_store_id="vs_unused",
                       answer_model="gpt-6.1-sol", check_model=CHECK,
                       reasoning_effort={"gpt-6.1-sol": "medium", CHECK: "low"},
                       grounding_bar=0.5, monthly_budget_usd=150.0)  # fmt: skip
    assessments = load_assessments(ROOT / "sample_course" / "assessments.yaml")
    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    ledger = Ledger(runs / "ledger.jsonl", config.monthly_budget_usd)
    step = RouteStep(config, responder(client=openai.OpenAI(), prices=load_prices(),
                                       ledger=ledger, run_id="route-live"))  # fmt: skip

    def one(i_row: tuple[int, dict]) -> dict:
        i, r = i_row
        qid = f"q-{i:012x}"
        window = [tuple(t) for t in r.get("prior_turns", [])]
        try:
            route = step.run(r["question"], window, assessments, question_id=qid)
            got, conf, aid = route.category, route.confidence, route.assessment_id
        except LLMError as exc:
            got, conf, aid = f"error:{exc.kind}", 0.0, None
        return {"id": r["id"], "expected": r["category"], "got": got, "confidence": conf,
                "expected_assessment": r.get("assessment_id"), "assessment": aid}  # fmt: skip

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(one, enumerate(rows)))

    by_cat: dict[str, Counter] = {c: Counter() for c in CATEGORIES}
    for res in results:
        by_cat[res["expected"]][res["got"] == res["expected"]] += 1
    print(f"{'category':<12} {'n':>4} {'recall':>7} target")
    table = {}
    for c in CATEGORIES:
        n = sum(by_cat[c].values())
        recall = by_cat[c][True] / n if n else float("nan")
        table[c] = {"n": n, "recall": recall}
        print(f"{c:<12} {n:>4} {recall:>7.3f} {TARGET.get(c, '-')}")
    graded = [r for r in results if r["expected"] == "graded_work" and r["got"] == "graded_work"]
    id_ok = sum(r["assessment"] == r["expected_assessment"] for r in graded)
    print(f"assessment id correct on routed graded work: {id_ok}/{len(graded)}")
    miss = [r for r in results if r["got"] != r["expected"]]
    print(f"misroutes: {len(miss)}")
    for r in miss:
        print(f"  {r['id']}: expected {r['expected']}, got {r['got']} ({r['confidence']:.2f})")
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = runs / f"route-live-{stamp}.json"
    out.write_text(json.dumps({"route_version": RouteStep.version(), "model": CHECK,
                               "table": table, "assessment_id_correct": [id_ok, len(graded)],
                               "results": results}, indent=1),
                   encoding="utf-8")  # fmt: skip
    spent = sum(json.loads(x)["cost_usd"] for x in (runs / "ledger.jsonl").read_text(
        encoding="utf-8").splitlines() if '"route-live"' in x)  # fmt: skip
    print(f"wrote {out.relative_to(ROOT)}; spend this run id so far: ${spent:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
