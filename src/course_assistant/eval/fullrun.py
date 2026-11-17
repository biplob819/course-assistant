"""The full run and the model choice (E2.5, FRN-01 to FRN-04).

`run_summary` turns one live course-ta run folder (written by `scripts/eval_live.py`) into the
§6.4 gate values, latency percentiles and measured cost; `compare` is the paired comparison of
two settings (EvalKit's bootstrap with Holm, through `release.regressions`); `choose` is the
FRN-02 rule; `cost_model` projects a month from measured numbers (FRN-03, NFR6); and
`sample_for_reading` draws the records to read end to end before trusting a headline (FRN-04,
L-14). `render_report` writes the published report.

Everything here reads and returns ids, numbers and verdict words; nothing returns case text.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evalkit import run as R
from evalkit.stats import percentile_ms

from course_assistant.eval import release
from course_assistant.llm.ledger import WARN_AT

LATENCY_TARGET_MS = release.LATENCY_P95_MS  # NFR5
QUALITY = ("grounded", "integrity", "injection", "leak", "identity")  # FRN-02, never traded
JUDGING_PURPOSES = frozenset({"eval_judging", "probe_judging"})  # ledger lines eval_live adds
MONTHLY_CAP_USD = 150.0  # OWNER D-37
ALARM_SHARE = WARN_AT  # OAI-04 and OWNER D-64: the ledger's alarm share, $135 of $150
PRD_NFR6_USD = 50.0  # the PRD's NFR6 figure, superseded by OWNER D-37's cap


class FullRunError(ValueError):
    """A run folder or an amount cannot be summarised as given (never defaulted, L-12)."""


def _amount(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) \
            or value < 0:  # fmt: skip
        raise FullRunError(f"{what} must be a finite non-negative number")
    return float(value)


@dataclass(frozen=True)
class RunSummary:
    """One run folder, as ids and numbers. `values` (per-case gate values) feed `compare`."""

    run: str
    label: str
    n: int
    n_errors: int
    judge_mode: str
    calibration: str
    gates: dict[str, float]
    passed: dict[str, bool]
    latency_p50_ms: int
    latency_p95_ms: int
    system_usd: float  # ledger rows of this run id: route, answer, checks, hints
    system_by_purpose: dict[str, float]
    judging_usd: float  # EvalKit's extraction and judging, plus the probe judges
    grounded_cases: int
    values: dict[str, dict[str, float]] = field(repr=False, default_factory=dict)

    @property
    def cost_per_question_usd(self) -> float:
        return self.system_usd / self.n if self.n else 0.0

    @property
    def cost_per_grounded_usd(self) -> float | None:
        return self.system_usd / self.grounded_cases if self.grounded_cases else None

    @property
    def total_usd(self) -> float:
        return self.system_usd + self.judging_usd

    def quality(self) -> dict[str, float]:
        return {m: self.gates[m] for m in QUALITY}


def evalkit_folder(run_dir: Path) -> Path:
    """The EvalKit run folder inside an eval_live folder (RUN/evalkit/<run>/run.json)."""
    if (run_dir / "run.json").is_file():
        return run_dir
    found = sorted((run_dir / "evalkit").glob("*/run.json"))
    if len(found) != 1:
        raise FullRunError(f"{run_dir.name} holds {len(found)} EvalKit run folders (needs one)")
    return found[0].parent


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def spend_of(rows: Iterable[Mapping[str, Any]], run_id: str) -> dict[str, float]:
    """Ledger spend of one run id, by purpose (every amount checked, L-6)."""
    out: dict[str, float] = {}
    for r in rows:
        if r.get("run_id") == run_id:
            purpose = str(r.get("purpose"))
            out[purpose] = out.get(purpose, 0.0) + _amount(r.get("cost_usd"), "a ledger cost")
    return out


def run_summary(run_dir: Path, ledger_rows: Sequence[Mapping[str, Any]],
                label: str) -> RunSummary:  # fmt: skip
    """Gate values, latency and measured cost of one eval_live folder. Records whose system
    call failed are counted as errors and left out; any other record without scores raises."""
    ek = evalkit_folder(run_dir)
    manifest = R.read_manifest(ek)
    cases = R.read_cases(ek)
    records = R.read_records(ek)
    if not records:
        raise FullRunError(f"{run_dir.name} has no records")
    ok = [r for r in records if r.completion.error is None]
    for r in ok:
        if r.scores is None:
            raise FullRunError(f"record {r.case_id} has no scores (judging not finished)")
    values = release.case_values(ok, cases)
    gates, errors = release.gates(values, [r.completion.latency_ms for r in ok])
    if errors:
        raise FullRunError("; ".join(errors))
    latencies = [r.completion.latency_ms for r in ok]
    by_purpose = spend_of(ledger_rows, run_dir.name)
    system = math.fsum(v for k, v in by_purpose.items() if k not in JUDGING_PURPOSES)
    judging = math.fsum(v for k, v in by_purpose.items() if k in JUDGING_PURPOSES)
    summary_path = ek / "summary.json"
    calibration = "unknown"
    if summary_path.is_file():
        calibration = str(json.loads(summary_path.read_text("utf-8")).get("calibration_status"))
    return RunSummary(
        run=run_dir.name, label=label, n=len(records), n_errors=len(records) - len(ok),
        judge_mode=str(manifest.judge_mode), calibration=calibration,
        gates={g.name: g.value for g in gates}, passed={g.name: g.passed for g in gates},
        latency_p50_ms=int(percentile_ms(latencies, 0.50)),
        latency_p95_ms=int(percentile_ms(latencies, 0.95)),
        system_usd=system, system_by_purpose=dict(sorted(by_purpose.items())),
        judging_usd=judging,
        grounded_cases=int(sum(values["grounded"].values())), values=values,
    )  # fmt: skip


def judging_spend(run_dir: Path) -> tuple[float, float]:
    """(EvalKit extraction and judging, probe judges) of one eval_live folder: EvalKit's
    `spent_usd` less the system costs its records report, and the probe panel's spend."""
    ek = evalkit_folder(run_dir)
    spent = _amount(R.read_manifest(ek).spent_usd, "EvalKit spent_usd")
    reported = math.fsum(_amount(r.completion.cost_usd, "a record cost")
                         for r in R.read_records(ek))  # fmt: skip
    probe_file = run_dir / "probe-judges.json"
    probe = 0.0
    if probe_file.is_file():
        probe = _amount(json.loads(probe_file.read_text("utf-8")).get("probe_judge_usd"),
                        "probe_judge_usd")  # fmt: skip
    return max(0.0, spent - reported), probe


@dataclass(frozen=True)
class Difference:
    metric: str
    n: int
    diff: float
    lo: float
    hi: float
    separated: bool  # Holm-rejected, either direction


def compare(a: RunSummary, b: RunSummary, seed: int = 7) -> list[Difference]:
    """a minus b per gated metric over the cases both runs share (paired bootstrap, Holm
    across metrics). A difference is separated when Holm rejects it, in either direction."""
    from evalkit.stats import holm

    rows = release.regressions(a.values, b.values, seed)
    rejected = holm({r.metric: r.p for r in rows}) if rows else {}
    return [Difference(r.metric, r.n, r.diff, r.lo, r.hi, bool(rejected[r.metric]))
            for r in rows]  # fmt: skip


@dataclass(frozen=True)
class Candidate:
    """One setting's measured result, as `choose` reads it."""

    label: str
    latency_p95_ms: int
    quality: dict[str, float]
    cost_per_grounded_usd: float | None


@dataclass(frozen=True)
class Choice:
    chosen: str | None
    reasons: dict[str, list[str]]  # why each setting did not qualify (empty: it qualified)
    order: list[str]  # qualifying settings, best first


def choose(candidates: Sequence[Candidate], reference: str) -> Choice:
    """FRN-02: quality first, then cost per grounded answer, under NFR5. A setting qualifies
    when its p95 is at most 8 s and no quality value is below the reference setting's; the
    highest grounded rate wins, then the lowest cost per grounded answer. None qualifying
    gives no choice: the owner decides the trade-off (D-44)."""
    by = {c.label: c for c in candidates}
    if reference not in by:
        raise FullRunError(f"no reference setting {reference!r}")
    ref = by[reference]
    reasons: dict[str, list[str]] = {}
    for c in candidates:
        why = []
        if c.latency_p95_ms > LATENCY_TARGET_MS:
            why.append(f"latency p95 {c.latency_p95_ms} ms > {LATENCY_TARGET_MS}")
        for m in QUALITY:
            if m not in c.quality or m not in ref.quality:
                raise FullRunError(f"{c.label}: quality value {m} is missing")
            if c.quality[m] < ref.quality[m]:
                why.append(f"{m} {c.quality[m]:.3f} below the reference {ref.quality[m]:.3f}")
        if c.cost_per_grounded_usd is None:
            why.append("no grounded answer, so no cost per grounded answer")
        reasons[c.label] = why
    ok = [c for c in candidates if not reasons[c.label]]
    ok.sort(key=lambda c: (-c.quality["grounded"], c.cost_per_grounded_usd or 0.0, c.label))
    return Choice(ok[0].label if ok else None, reasons, [c.label for c in ok])


@dataclass(frozen=True)
class CostModel:
    per_question_usd: float
    questions_per_month: int
    run_usd: float
    runs_per_month: int
    answer_path_usd: float
    runs_usd: float
    monthly_usd: float
    cap_usd: float
    within_cap: bool
    under_alarm: bool
    under_prd_nfr6: bool


def cost_model(per_question_usd: float, questions_per_month: int, run_usd: float,
               runs_per_month: int, cap_usd: float = MONTHLY_CAP_USD) -> CostModel:  # fmt: skip
    """A month from measured numbers: questions times cost per question plus full runs times
    cost per run, against the cap (OWNER D-37), its alarm (OWNER D-64) and the PRD's NFR6 figure."""
    per_q = _amount(per_question_usd, "cost per question")
    run = _amount(run_usd, "cost per run")
    cap = _amount(cap_usd, "the cap")
    for name, n in (("questions_per_month", questions_per_month),
                    ("runs_per_month", runs_per_month)):  # fmt: skip
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise FullRunError(f"{name} must be a whole number")
    answers, runs = per_q * questions_per_month, run * runs_per_month
    total = answers + runs
    return CostModel(per_q, questions_per_month, run, runs_per_month, answers, runs, total, cap,
                     total <= cap, total < ALARM_SHARE * cap, total <= PRD_NFR6_USD)  # fmt: skip


def sample_for_reading(rules_by_id: Mapping[str, str], heldout_ids: Iterable[str], n: int = 20,
                       seed: int = 7) -> list[str]:  # fmt: skip
    """n public record ids to read end to end (FRN-04, L-14): round-robin over the rules in a
    fixed order, each rule's ids shuffled by the seed. Held-out ids are never drawn."""
    held = set(heldout_ids)
    pools: dict[str, list[str]] = {}
    for cid in sorted(rules_by_id):
        if cid not in held:
            pools.setdefault(rules_by_id[cid], []).append(cid)
    rng = random.Random(seed)
    for ids in pools.values():
        rng.shuffle(ids)
    out: list[str] = []
    while len(out) < n and any(pools.values()):
        for rule in sorted(pools):
            if pools[rule] and len(out) < n:
                out.append(pools[rule].pop())
    return out


def _usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.4f}"


@dataclass(frozen=True)
class SmokeRow:
    """One latency smoke run (30 public questions through `ask()`)."""

    label: str
    run: str
    n: int
    p50_ms: int
    p95_ms: int
    mean_usd: float
    status_match: int


def smoke_row(path: Path, label: str, ledger_rows: Sequence[Mapping[str, Any]]) -> SmokeRow:
    """A `scripts/smoke_live.py` result file as a SmokeRow (numbers only). Cost per question
    is the run id's ledger spend over the questions: with `early_finalize` a discarded answer
    call is ledgered but not in the turn's `cost_usd`."""
    data = json.loads(path.read_text(encoding="utf-8"))
    s = data["smoke"]["summary"]
    n = int(s["n"])
    spent = math.fsum(spend_of(ledger_rows, str(data["run_id"])).values())
    return SmokeRow(label, str(data["run_id"]), n, int(s["question"]["p50_ms"]),
                    int(s["question"]["p95_ms"]), spent / n if n else 0.0,
                    int(data["smoke"]["status_match"]))  # fmt: skip


def render_report(summaries: Sequence[RunSummary], differences: Mapping[str, list[Difference]],
                  choice: Choice | None, notes: Sequence[str] = (),
                  smoke: Sequence[SmokeRow] = ()) -> str:  # fmt: skip
    """The published report (US8): the latency smoke, one row per run, the gates, the paired
    differences and the choice. Built from ids and numbers only."""
    lines = ["# E2.5 full run report", "",
             "Generated by `scripts/full_run_report.py` from the live run folders and "
             "`runs/ledger.jsonl`. Ids and numbers only; no case text.", ""]  # fmt: skip
    if smoke:
        lines += ["## Latency runs through ask() (public split, no judges)", "",
                  "| Setting | Run | n | p50 | p95 | Cost/question | Status as expected |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]  # fmt: skip
        for r in smoke:
            lines.append(f"| {r.label} | {r.run} | {r.n} | {r.p50_ms / 1000:.1f} s | "
                         f"{r.p95_ms / 1000:.1f} s | {_usd(r.mean_usd)} | "
                         f"{r.status_match}/{r.n} |")  # fmt: skip
        lines.append("")
    head = "| Setting | Run | Cases | Errors | Judges | Calibration | p50 | p95 | Cost/question " \
           "| Cost/grounded | Run total |"  # fmt: skip
    if summaries:
        lines += ["## Judged runs", "", head, "|" + " --- |" * 11]
    for s in summaries:
        lines.append(f"| {s.label} | {s.run} | {s.n} | {s.n_errors} | {s.judge_mode} | "
                     f"{s.calibration} | {s.latency_p50_ms / 1000:.1f} s | "
                     f"{s.latency_p95_ms / 1000:.1f} s | {_usd(s.cost_per_question_usd)} | "
                     f"{_usd(s.cost_per_grounded_usd)} | {_usd(s.total_usd)} |")  # fmt: skip
    names = list(summaries[0].gates) if summaries else []
    if names:
        lines += ["", "## Gates (§6.4)", "", "| Setting | " + " | ".join(names) + " |",
                  "|" + " --- |" * (len(names) + 1)]  # fmt: skip
    for s in summaries:
        cells = [f"{s.gates[m]:.3f} {'pass' if s.passed[m] else 'FAIL'}" for m in names]
        lines.append(f"| {s.label} | " + " | ".join(cells) + " |")
    for title, rows in differences.items():
        lines += ["", f"## {title}", "", "| Metric | Shared cases | Difference | 95% CI | "
                  "Separated (Holm) |", "| --- | --- | --- | --- | --- |"]  # fmt: skip
        for d in rows:
            lines.append(f"| {d.metric} | {d.n} | {d.diff:+.3f} | [{d.lo:+.3f}, {d.hi:+.3f}] | "
                         f"{'yes' if d.separated else 'no'} |")  # fmt: skip
    if choice is not None:
        chosen = choice.chosen or "none qualifies; owner decides (D-44)"
        lines += ["", "## Choice (FRN-02)", "", f"Chosen: **{chosen}**", ""]
        for label, why in choice.reasons.items():
            lines.append(f"- {label}: {'qualifies' if not why else '; '.join(why)}")
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"
