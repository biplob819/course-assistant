#!/usr/bin/env python3
"""Live smoke run for A2.5 (ASK-06): real API calls through `ask()`, run by the owner.

    uv run --env-file .env python scripts/smoke_live.py --config CONFIG.yaml --yes

CONFIG.yaml is an AppConfig whose vector store has been filled by
`course-assistant ingest --config CONFIG.yaml` (the corpus store defaults to `corpus_store/`
next to it). Four parts, all from the public golden split (never golden/heldout):

1. 30 questions stratified over the rules: p50 and p95 latency per step and per question,
   cost per question, status against expected (for Tech Spec §2.6 and §9).
2. The public planted B7 questions: each must come back `not_covered` (GRD-04, D-38). The
   public split holds 5 of the 10; the other 5 are held out and run in the held-out eval.
3. The public multi-turn B3 cases: earlier turns seeded, the assembling turn asked live;
   caught means `hint_only` (INT-04, D-40).
4. The public B1 follow-up cases: recall@8 against gold units, raw question with the window
   against the route's standalone rewrite alone (§2 parallel-start bullet).

`--latency-only` (E2.5) runs part 1 only, and `--label` names the setting in the run id and
the result file, so each candidate setting's smoke run is ledgered under its own run id.
`--public-all` (E2.5) runs every public case instead of the 30 (earlier turns seeded), with
statuses against expected per rule: latency and cost with no judge (the judged run is
`scripts/eval_live.py`).

Prints the estimate first and stops over --max-cost. Spend goes to runs/ledger.jsonl; results
to runs/smoke-<stamp>.json (git-ignored).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from course_assistant.answer.measure import percentile, recall_at_k  # noqa: E402

PUBLIC = ROOT / "golden" / "public" / "cases.jsonl"
PER_QUESTION_USD = 0.03  # Tech Spec §9 planning figure, rounded up
QUOTA = {"B1": 10, "B2": 3, "B3": 4, "B4": 4, "B8": 2, "B9": 2, "B10": 5}  # 30


def public_cases() -> list[dict[str, Any]]:
    rows = [json.loads(x) for x in PUBLIC.read_text(encoding="utf-8").splitlines() if x.strip()]
    for r in rows:
        r.setdefault("prior_turns", [])
        r.setdefault("gold_units", [])
    return rows


def smoke_set(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """30 questions: a fixed quota per rule, taken in file order, single-turn first."""
    out: list[dict[str, Any]] = []
    for rule, k in QUOTA.items():
        pool = sorted((c for c in cases if c["rule"] == rule), key=lambda c: bool(c["prior_turns"]))
        out.extend(pool[:k])
    if len(out) != 30:
        raise SystemExit(f"smoke set has {len(out)} questions, not 30")
    return out


def planted(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in cases if c["rule"] == "B7"]


def decomposition(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in cases if c["rule"] == "B3" and c["prior_turns"]]


def follow_ups(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in cases if c["rule"] == "B1" and c["prior_turns"] and c["gold_units"]]


def summarise(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """p50 and p95 of question latency and of each step, and cost per question."""
    rows = list(rows)
    lat = [r["latency_ms"] for r in rows]
    cost = [r["cost_usd"] for r in rows]
    steps: dict[str, list[float]] = {}
    for r in rows:
        for step, ms in r["steps"].items():
            steps.setdefault(step, []).append(ms)
    return {
        "n": len(rows),
        "question": {"p50_ms": percentile(lat, 50), "p95_ms": percentile(lat, 95)},
        "steps": {k: {"n": len(v), "p50_ms": percentile(v, 50), "p95_ms": percentile(v, 95)}
                  for k, v in sorted(steps.items())},
        "cost": {"mean_usd": sum(cost) / len(cost), "p50_usd": percentile(cost, 50),
                 "p95_usd": percentile(cost, 95), "total_usd": sum(cost)},
    }  # fmt: skip


def by_rule(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per rule: cases, statuses as expected, and errors (status `error`)."""
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        d = out.setdefault(r["rule"], {"n": 0, "as_expected": 0, "errors": 0})
        d["n"] += 1
        d["as_expected"] += r["status"] == r["expected"]
        d["errors"] += r["status"] == "error"
    return dict(sorted(out.items()))


def label_arg(value: str) -> str:
    """A setting label safe in a run id and a file name."""
    import re

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", value):
        raise argparse.ArgumentTypeError("label: 1-40 lowercase letters, digits or dashes")
    return value


def _pid(case_id: str) -> str:
    return hashlib.sha256(f"smoke:{case_id}".encode()).hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", required=True, type=Path, help="AppConfig YAML (ingested)")
    ap.add_argument("--store", type=Path, help="corpus store folder (default: next to config)")
    ap.add_argument("--max-cost", type=float, default=5.0, help="stop if the estimate is over")
    ap.add_argument("--latency-only", action="store_true",
                    help="part 1 only: the 30 questions (E2.5 latency smoke)")  # fmt: skip
    ap.add_argument("--public-all", action="store_true",
                    help="every public case, not the 30 (implies --latency-only)")  # fmt: skip
    ap.add_argument("--label", type=label_arg, default=None,
                    help="setting name for the run id (letters, digits, dashes)")  # fmt: skip
    ap.add_argument("--yes", action="store_true", help="confirm that this run spends money")
    args = ap.parse_args(argv)
    if not args.yes:
        print("smoke_live: this run makes paid API calls; pass --yes to confirm",
              file=sys.stderr)  # fmt: skip
        return 2
    if not os.environ.get("OPENAI_API_KEY"):
        print("smoke_live: OPENAI_API_KEY is not set (uv run --env-file .env ...)",
              file=sys.stderr)  # fmt: skip
        return 2

    cases = public_cases()
    smoke, plant, probes, follow = (smoke_set(cases), planted(cases), decomposition(cases),
                                    follow_ups(cases))  # fmt: skip
    if args.public_all:
        smoke = cases
    if args.latency_only or args.public_all:
        plant, probes, follow = [], [], []
    n_calls = len(smoke) + len(plant) + len(probes) + 2 * len(follow)
    estimate = n_calls * PER_QUESTION_USD
    print(f"estimate: {n_calls} questions x ${PER_QUESTION_USD} = ${estimate:.2f}")
    if estimate > args.max_cost:
        print(f"smoke_live: estimate over --max-cost ${args.max_cost:.2f}", file=sys.stderr)
        return 3
    return _run(args, smoke, plant, probes, follow)


def _run(args: argparse.Namespace, smoke: list[dict[str, Any]], plant: list[dict[str, Any]],
         probes: list[dict[str, Any]], follow: list[dict[str, Any]]) -> int:  # fmt: skip
    import openai

    from course_assistant.answer import responder
    from course_assistant.answer.answer import AnswerStep
    from course_assistant.answer.ask import Assistant
    from course_assistant.answer.replay import Recorder
    from course_assistant.answer.route import RouteStep
    from course_assistant.ingest import CorpusStore
    from course_assistant.llm import JsonlSink, Ledger, LLMError, load_prices
    from course_assistant.schemas.config import load_app_config, load_assessments
    from course_assistant.schemas.request import Conversation
    from course_assistant.store.db import Store

    config = load_app_config(args.config)
    corpus = CorpusStore(args.store or args.config.parent / "corpus_store", config.cohort).current()
    if corpus is None:
        print(f"smoke_live: no corpus; run course-assistant ingest --config {args.config} first",
              file=sys.stderr)  # fmt: skip
        return 2
    assessments = load_assessments(ROOT / "sample_course" / "assessments.yaml")
    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"smoke-{args.label}-{stamp}" if args.label else f"smoke-{stamp}"
    ledger = Ledger(runs / "ledger.jsonl", config.monthly_budget_usd)
    sink = JsonlSink(runs / f"{run_id}-trace.jsonl")
    live = responder(client=openai.OpenAI(), prices=load_prices(), ledger=ledger, sink=sink,
                     run_id=run_id)  # fmt: skip
    recorder = Recorder(live)
    store = Store(runs / f"{run_id}.sqlite")
    assistant = Assistant(config, corpus, assessments, store, recorder, sink=sink)

    def ask(case: dict[str, Any], cid: str | None = None) -> dict[str, Any]:
        t0 = time.monotonic()
        turn = assistant.turn(case["question"], _pid(case["id"]), case.get("session_scope"), cid)
        wall = int((time.monotonic() - t0) * 1000)
        return {"id": case["id"], "rule": case["rule"], "expected": case["expected_status"],
                "status": turn.answer.status, "latency_ms": turn.answer.latency_ms,
                "wall_ms": wall, "cost_usd": turn.answer.cost_usd,
                "steps": {t.step: t.latency_ms for t in turn.steps},
                "step_cost": {t.step: t.cost_usd for t in turn.steps},
                "errors": {t.step: t.error for t in turn.steps if t.error},
                "request_id": turn.request_id}  # fmt: skip

    def seeded(case: dict[str, Any]) -> str:
        """The case's earlier turns as a stored conversation (D-40 decomposition probes)."""
        from course_assistant.answer.golden import Case, Fixture, seed

        pid = _pid(case["id"])
        now = dt.datetime.now(dt.UTC)
        fx = Fixture(format=1, note="smoke", now=now, config=config, corpus=corpus,
                     assessments=assessments, cases=())  # fmt: skip
        rid = "r-" + hashlib.sha256(f"{run_id}{case['id']}".encode()).hexdigest()[:16]
        c = Case(id=case["id"], source=case["id"], scenario="smoke", request_id=rid,
                 learner_pid=pid, session=case.get("session_scope"), question=case["question"],
                 prior_turns=tuple(tuple(t) for t in case["prior_turns"]),
                 expected_status="hint_only", recordings=())  # fmt: skip
        cid = seed(store, fx, c)
        assert cid is not None and isinstance(store.get_conversation(cid), Conversation)
        return cid

    results: dict[str, Any] = {"run_id": run_id, "config": config.model_dump(mode="json"),
                               "rules_hash": Assistant.rules_hash(),
                               "corpus_version": corpus.corpus_version}  # fmt: skip
    smoke_rows = [ask(c, seeded(c) if c["prior_turns"] else None) for c in smoke]
    results["smoke"] = {
        "by_rule": by_rule(smoke_rows),
        "rows": smoke_rows,
        "summary": summarise(smoke_rows),
        "status_match": sum(r["status"] == r["expected"] for r in smoke_rows),
    }
    plant_rows = [ask(c) for c in plant]
    results["planted_b7"] = {
        "rows": plant_rows,
        "public_cases": len(plant_rows),
        "not_covered": sum(r["status"] == "not_covered" for r in plant_rows),
        "note": "5 more B7 cases are held out (E2 held-out eval)",
    }
    probe_rows = [ask(c, seeded(c)) for c in probes]
    results["decomposition_b3"] = {"rows": probe_rows,
                                   "caught": sum(r["status"] == "hint_only" for r in probe_rows),
                                   "cases": len(probe_rows)}  # fmt: skip

    route_step = RouteStep(config, live)
    answer_step = AnswerStep(config, corpus, live)
    recall_rows = []
    for c in follow:
        window = [tuple(t) for t in c["prior_turns"]]
        try:
            route = route_step.run(c["question"], window, assessments, question_id="q-" + "0" * 16)
            raw = answer_step.run(c["question"], window, c.get("session_scope"))
            rewritten = answer_step.run(route.standalone_question, (), c.get("session_scope"))
        except LLMError as exc:
            recall_rows.append({"id": c["id"], "error": exc.kind})
            continue
        recall_rows.append(
            {
                "id": c["id"],
                "gold": c["gold_units"],
                "raw": recall_at_k(raw.hits, c["gold_units"], corpus),
                "rewritten": recall_at_k(rewritten.hits, c["gold_units"], corpus),
            }
        )
    ok = [r for r in recall_rows if "raw" in r]
    results["recall_at_8"] = {"rows": recall_rows, "n": len(ok),
                              "raw_mean": sum(r["raw"] for r in ok) / len(ok) if ok else None,
                              "rewritten_mean": (sum(r["rewritten"] for r in ok) / len(ok)
                                                 if ok else None)}  # fmt: skip
    results["recordings"] = len(recorder.recordings)

    out = runs / f"{run_id}.json"
    out.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    summary = results["smoke"]["summary"]
    print(
        f"smoke: {summary['n']} questions; latency p50 {summary['question']['p50_ms']} ms, "
        f"p95 {summary['question']['p95_ms']} ms; cost mean "
        f"${summary['cost']['mean_usd']:.4f}, total ${summary['cost']['total_usd']:.4f}"
    )
    for step, v in summary["steps"].items():
        print(f"  {step:<10} p50 {v['p50_ms']:>6} ms  p95 {v['p95_ms']:>6} ms  (n={v['n']})")
    print(f"status matches expected: {results['smoke']['status_match']}/{summary['n']}")
    p = results["planted_b7"]
    print(f"planted B7 not_covered: {p['not_covered']}/{p['public_cases']} (public split)")
    d = results["decomposition_b3"]
    print(f"decomposition caught by the assembling turn: {d['caught']}/{d['cases']}")
    r = results["recall_at_8"]
    print(
        f"recall@8 on follow-ups (n={r['n']}): raw {r['raw_mean']}, rewritten {r['rewritten_mean']}"
    )
    print(f"wrote {out.relative_to(ROOT)}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
