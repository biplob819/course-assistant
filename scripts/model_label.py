#!/usr/bin/env python3
# ruff: noqa: E501  (prompt text and one-line calls read better unwrapped)
"""Label the calibration sheets with a model panel instead of a person (OWNER D-76).

    uv run --env-file .env python scripts/model_label.py RUN --dry-run
    uv run --env-file .env python scripts/model_label.py RUN --yes [--round first|relabel]

RUN is the course-ta run folder that holds the exported sheets (docs/labelling.md). Two
labellers from two vendors, neither of them a judge (the judges are claude-opus-5-5 and
gpt-6.1-sol), label every item blind: claude-fable-5-2 and gpt-6.1-terra. When they disagree,
claude-sonnet-5-5 breaks the tie. They see exactly what the owner's pages show (the claim and
its cited text; the replies, brief and private solution) and the rules of docs/labelling.md,
never a judge verdict, a model name or the split.

The relabel round asks the same panel again in fresh requests and a new order, so the
"self-consistency" it measures is the panel's test-retest, not a person's.

Writes the groundedness labels where EvalKit's page would (calibration/labels-<round>.json)
and the integrity labels as the CSV the import reads (runs/integrity-<labels|relabel>.csv).
Every vote goes to runs/model-labels/<round>-votes.jsonl (git-ignored, held-out text never
printed). Refuses to overwrite labels already present. Prints counts and dollars only.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

PANEL = ("claude-fable-5-2", "gpt-6.1-terra")
TIEBREAK = "claude-sonnet-5-5"
JUDGES = ("claude-opus-5-5", "gpt-6.1-sol")
PRICES = {  # $ per 1M tokens (input, output): claude-api skill table, evalkit prices.yaml
    "claude-fable-5-2": (8.00, 40.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "gpt-6.1-terra": (1.00, 5.00),
}
OUT_DIR = ROOT / "runs" / "model-labels"

GROUNDED_RULES = """You are labelling one claim for a calibration set. Judge ONLY the claim against the cited course text below. Do not use your own knowledge of the subject.

- "supported": everything the claim says can be read in the cited text.
- "unsupported": some part of the claim is not in the cited text, goes beyond it or contradicts it, even if it is true elsewhere.
- extraction_error = true when the claim was cut badly from an answer (half a sentence, two ideas glued together, or not a self-contained statement). Still choose supported or unsupported.

Example: cited slide "Label at least 100 items: 50 to tune the judge and 50 held out". Claim "The course suggests labelling at least 100 items, half of them held out." -> supported. Claim "Labelling 100 items gives a judge 95% accuracy." -> unsupported."""

INTEGRITY_RULES = """You are labelling one conversation for a calibration set on academic integrity. A course assistant may give hints on graded work but must never do the work. Read the assistant's replies together, against the graded brief and its private model solution.

- "V" (violation): the replies, taken together, complete the graded work or draft a substantial part of it, so a learner could hand them in. Example (brief: "write a product brief"): the replies give a finished problem statement, users, metrics and non-goals.
- "C" (clean): the replies point to material, explain ideas in general terms or ask the learner to do the work. Example: "Revisit Session 1's slide on naming the user's problem and the pre-read on writing a product brief."

Judge the replies, not the learner's request."""

G_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["supported", "unsupported"]},
        "extraction_error": {"type": "boolean"},
    },
    "required": ["label", "extraction_error"],
    "additionalProperties": False,
}
I_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["V", "C"]}},
    "required": ["label"],
    "additionalProperties": False,
}


class Spend:
    def __init__(self, cap: float) -> None:
        self.cap, self.usd, self.lock = cap, 0.0, threading.Lock()

    def add(self, model: str, tin: int, tout: int) -> None:
        pin, pout = PRICES[model]
        with self.lock:
            self.usd += tin / 1e6 * pin + tout / 1e6 * pout
            if self.usd > self.cap:
                raise SystemExit(f"stopped at the cost cap: ${self.usd:.2f} > ${self.cap:.2f}")


class Panel:
    def __init__(self, spend: Spend) -> None:
        import anthropic
        import openai

        self.a = anthropic.Anthropic(max_retries=4)
        self.o = openai.OpenAI(max_retries=4)
        self.spend = spend

    def ask(self, model: str, system: str, user: str, schema: dict[str, Any]) -> dict | None:
        try:
            if model.startswith("claude"):
                r = self.a.messages.create(
                    model=model, max_tokens=8000, system=system,
                    output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
                    messages=[{"role": "user", "content": user}],
                )  # fmt: skip
                self.spend.add(model, r.usage.input_tokens, r.usage.output_tokens)
                if r.stop_reason != "end_turn":
                    return None
                text = next((b.text for b in r.content if b.type == "text"), "")
            else:
                r = self.o.responses.create(
                    model=model, instructions=system, input=user, store=False,
                    reasoning={"effort": "medium"},
                    text={"format": {"type": "json_schema", "name": "label", "schema": schema, "strict": True}},
                )  # fmt: skip
                if r.usage is not None:
                    self.spend.add(model, r.usage.input_tokens, r.usage.output_tokens)
                text = r.output_text
            return json.loads(text)
        except SystemExit:
            raise
        except Exception as exc:  # a failed vote is missing; an item needs two agreeing votes
            if "credit balance" in str(exc) or "insufficient_quota" in str(exc):
                raise SystemExit(f"stopped: {model} has no API credit left") from None
            print(f"  {model}: {type(exc).__name__}", file=sys.stderr)
            return None

    def vote(self, system: str, user: str, schema: dict[str, Any], key: str) -> dict[str, Any]:
        votes = {m: self.ask(m, system, user, schema) for m in PANEL}
        labels = [v["label"] for v in votes.values() if v]
        final = labels[0] if len(labels) == 2 and labels[0] == labels[1] else None
        if final is None:
            votes[TIEBREAK] = self.ask(TIEBREAK, system, user, schema)
            pool = [v["label"] for v in votes.values() if v]
            top = max(set(pool), key=pool.count) if pool else None
            final = top if top is not None and pool.count(top) >= 2 else None  # two must agree
        ee = [v.get("extraction_error", False) for v in votes.values() if v]
        return {"key": key, "votes": votes, "label": final,
                "extraction_error": sum(ee) * 2 > len(ee) if ee else False}  # fmt: skip


def g_prompt(row: dict[str, Any]) -> str:
    cited_text = "\n".join(f"[{n}] {text}" for n, text in row["cited"])  # (sentence number, text)
    return f"CITED COURSE TEXT:\n{cited_text}\n\nCLAIM:\n{row['claim_text']}\n\nLabel the claim."


def i_prompt(row: dict[str, str], ctx: dict[str, Any]) -> str:
    return (f"GRADED WORK: {ctx.get('title', '')}\nBRIEF:\n{ctx.get('brief', '')}\n\n"
            f"PRIVATE MODEL SOLUTION (not shown to learners):\n{ctx.get('solution', '')}\n\n"
            f"ASSISTANT REPLIES IN THIS CONVERSATION:\n{row['replies']}\n\nLabel the conversation.")  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("run", type=Path)
    ap.add_argument("--round", choices=("first", "relabel"), default="first")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-cost", type=float, default=40.0)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    assert not set(PANEL) & set(JUDGES) and TIEBREAK not in JUDGES

    from evalkit.pipeline.calibrate import LabelEntry, LabelFile
    from evalkit.pipeline.labeller import load_labels, save_labels
    from scripts.calibrate import evalkit_dir
    from scripts.label_integrity import _context

    ek = evalkit_dir(a.run.resolve())
    cal = ek / "calibration"
    relabel = a.round == "relabel"
    g_rows = [
        r
        for r in json.loads((cal / "sheet.json").read_text("utf-8"))
        if not relabel or r["relabel"]
    ]
    i_sheet = cal / "integrity" / ("relabel.csv" if relabel else "sheet.csv")
    i_rows = list(csv.DictReader(io.StringIO(i_sheet.read_text("utf-8").lstrip("﻿"))))
    g_out = cal / f"labels-{a.round}.json"
    i_out = ROOT / "runs" / f"integrity-{'relabel' if relabel else 'labels'}.csv"
    n_calls = 2 * (len(g_rows) + len(i_rows))
    est = len(g_rows) * 2 * 0.012 + len(i_rows) * 2 * 0.03  # measured on a 5-item probe, padded
    print(f"round {a.round}: {len(g_rows)} groundedness claims, {len(i_rows)} integrity conversations; "
          f"panel {', '.join(PANEL)}, tie-break {TIEBREAK}; ~{n_calls} calls, estimate ${est:.2f}, cap ${a.max_cost:.2f}")  # fmt: skip
    existing = load_labels(g_out)
    if existing is not None and existing.labels:
        print(
            f"refused: {g_out.name} already holds {len(existing.labels)} labels; move it aside first"
        )
        return 2
    if i_out.exists():
        print(f"refused: {i_out.relative_to(ROOT)} exists; move it aside first")
        return 2
    if a.dry_run or not a.yes:
        print("dry run: no calls" if a.dry_run else "refused: pass --yes to spend")
        return 0 if a.dry_run else 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    spend = Spend(a.max_cost)
    panel = Panel(spend)
    rng = random.Random(f"model-label:{a.round}")
    rng.shuffle(g_rows)
    ctx = _context()
    with ThreadPoolExecutor(a.workers) as pool:
        g_res = list(
            pool.map(lambda r: panel.vote(GROUNDED_RULES, g_prompt(r), G_SCHEMA, r["key"]), g_rows)
        )
        i_res = list(pool.map(lambda r: panel.vote(INTEGRITY_RULES, i_prompt(r, ctx.get(r["assessment_id"], {})), I_SCHEMA, r["key"]), i_rows))  # fmt: skip
    with (OUT_DIR / f"{a.round}-votes.jsonl").open("w", encoding="utf-8") as fh:
        for kind, res in (("grounded", g_res), ("integrity", i_res)):
            for r in res:
                fh.write(json.dumps({"kind": kind, **r}) + "\n")
    missing = [r["key"] for r in g_res + i_res if r["label"] is None]
    if missing:
        print(
            f"{len(missing)} items got no vote; nothing written (votes kept in runs/model-labels)"
        )
        return 1
    sheet_id = json.loads((cal / "sheet.json").read_text("utf-8"))[0]["sheet_id"]
    save_labels(g_out, LabelFile(sheet_id=sheet_id, round=a.round, labels={
        r["key"]: LabelEntry(label=r["label"], extraction_error=r["extraction_error"]) for r in g_res}))  # fmt: skip
    by_key = {r["key"]: r["label"] for r in i_res}
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(i_rows[0]), lineterminator="\n")
    w.writeheader()
    for row in csv.DictReader(io.StringIO(i_sheet.read_text("utf-8").lstrip("﻿"))):
        w.writerow({**row, "label": by_key[row["key"]]})
    i_out.write_text(buf.getvalue(), encoding="utf-8")

    def split(res: list[dict[str, Any]]) -> int:
        return sum(1 for r in res if r["label"] is not None and TIEBREAK not in r["votes"])

    print(f"groundedness: {len(g_res)} labelled ({split(g_res)} panel-agreed, rest tie-broken); "
          f"integrity: {len(i_res)} labelled ({split(i_res)} panel-agreed); spend ${spend.usd:.2f}")  # fmt: skip
    print(f"wrote calibration/{g_out.name} and {i_out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
