"""Bar and support-rule tuning on the tuning split only (E2.3 CAL-02, D-5, Tech Spec §5.4).

`sweep_bar` replays a recorded tuning-split run through `ask()` at each candidate
`grounding_bar`. A higher bar only removes calls (L1 failing skips L2), so every candidate at or
above the recorded bar is decided by the real pipeline with no API call; a candidate below it
would need calls never recorded and is refused. Statuses come from the replay, groundedness
from the recorded dual-judge record of the same answer text.

Every change to the bar or to `SUPPORT_RULES` is a line in the tuning log
(`docs/measurements/tuning-log.jsonl`): numbers only, naming the decision that made it. The
log is what a run's bar and the code's ground version are checked against.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evalkit.schemas import Case, Record

from course_assistant.answer.replay import Recording, Replay, ReplayError
from course_assistant.eval import system
from course_assistant.schemas._base import FrozenMap, Model, NonNegInt

TARGET_GROUNDED = 0.90  # §6.4 grounded answer rate (point)
TARGET_HANDOVER = 0.95  # §6.4 correct handover
HANDOVER_RULES = frozenset({"B2", "B4", "B7"})
KINDS = ("grounding_bar", "support_rules", "heldout_scored", "heldout_refreshed",
         "calibration_scored")
REFRESH_OPENING = "Refreshes held-out"  # a heldout_refreshed decision must open with this
_DECISION = re.compile(r"^D-\d+$")


class TuningError(ValueError):
    """Tuning cannot run as asked. Messages name ids and counts, never text."""


class BarResult(Model):
    bar: float
    replayable: bool
    unreplayable: NonNegInt
    n_b1: NonNegInt
    n_handover: NonNegInt
    grounded_rate: float
    handover_rate: float
    statuses: FrozenMap[NonNegInt]

    @property
    def total(self) -> float:
        return self.grounded_rate + self.handover_rate

    @property
    def meets_targets(self) -> bool:
        """The §6.4 targets, on tuning numbers (never a held-out claim)."""
        return self.grounded_rate >= TARGET_GROUNDED and self.handover_rate >= TARGET_HANDOVER


def recorded_bar(records: Sequence[Record]) -> float:
    """The one `grounding_bar` the recorded run answered with (from the completions)."""
    bars = {r.completion.params.get("grounding_bar") for r in records
            if r.completion.answer is not None}  # fmt: skip
    if len(bars) != 1 or None in bars:
        raise TuningError(f"the recorded run must use one grounding_bar, found {len(bars)}")
    return float(str(bars.pop()))


def tuning_digest(cases: Sequence[Case]) -> str:
    return hashlib.sha256("\n".join(sorted(c.id for c in cases)).encode("utf-8")).hexdigest()


def _fully_grounded(record: Record | None) -> bool:
    if record is None or record.scores is None or not record.scores.fully_grounded:
        return False
    return all(record.scores.fully_grounded.values())


def sweep_bar(cases: Sequence[Case], recordings: Sequence[Recording], records: Sequence[Record],
              runtime: system.Runtime, candidates: Sequence[float]) -> list[BarResult]:  # fmt: skip
    """Each candidate bar on the recorded tuning run, replayed through `ask()`."""
    held = [c.id for c in cases if c.meta.get("split") != "tuning"]
    if held:
        raise TuningError(f"{len(held)} case(s) outside the tuning split offered to tuning (D-5)")
    if not cases:
        raise TuningError("no tuning cases")
    bad = [c for c in candidates if not (isinstance(c, float | int) and math.isfinite(c)
                                         and 0.0 <= c <= 1.0)]  # fmt: skip
    if bad or not candidates:
        raise TuningError("candidates must be bars between 0 and 1")
    floor = recorded_bar(records)
    below = sorted(c for c in candidates if c < floor)
    if below:
        raise TuningError(f"candidate(s) {below} are below the recorded bar {floor}: record a "
                          "tuning run at that floor first")  # fmt: skip
    by_case = {r.case_id: r for r in records}
    missing = [c.id for c in cases if c.id not in by_case]
    if missing:
        raise TuningError(f"{len(missing)} tuning case(s) have no recorded record")
    b1 = [c for c in cases if c.meta.get("rule") == "B1"]
    handover = [c for c in cases if c.meta.get("rule") in HANDOVER_RULES]
    out = []
    for bar in sorted({float(c) for c in candidates}):
        config = runtime.config.model_copy(update={"grounding_bar": bar})
        rt = dataclasses.replace(runtime, config=config, source=Replay(recordings))
        statuses: dict[str, str] = {}
        unreplayable = 0
        for case in cases:
            try:
                answer, _ = system.run_case(rt, case)
            except ReplayError:  # this bar would take a path the recording never made
                unreplayable += 1
                continue
            statuses[case.id] = answer.status
        grounded = sum(statuses.get(c.id) == "answered" and _fully_grounded(by_case[c.id])
                       for c in b1)  # fmt: skip
        handed = sum(statuses.get(c.id) == c.reference_label for c in handover)
        counts: dict[str, int] = {}
        for s in statuses.values():
            counts[s] = counts.get(s, 0) + 1
        out.append(BarResult(bar=bar, replayable=unreplayable == 0, unreplayable=unreplayable,
                             n_b1=len(b1), n_handover=len(handover),
                             grounded_rate=grounded / len(b1) if b1 else 0.0,
                             handover_rate=handed / len(handover) if handover else 0.0,
                             statuses=counts))  # fmt: skip
    return out


def choose(results: Sequence[BarResult]) -> BarResult:
    """The replayable bar with the best grounded + handover rate; ties go to the higher bar
    (fail closed)."""
    ok = [r for r in results if r.replayable]
    if not ok:
        raise TuningError("no candidate bar could be replayed")
    return max(ok, key=lambda r: (round(r.total, 12), r.bar))


# ---------- the tuning log ----------


def read_log(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def current(log: Sequence[Mapping[str, Any]]) -> tuple[float | None, str | None]:
    """The logged bar and ground version (the last change of each)."""
    bar: float | None = None
    ground: str | None = None
    for e in log:
        if e["kind"] == "grounding_bar":
            bar = float(e["to"])
        elif e["kind"] == "support_rules":
            ground = str(e["to"])
    return bar, ground


def _decision_row(decision: str, decisions_text: str) -> str | None:
    if not _DECISION.fullmatch(decision):
        return None
    row = re.compile(rf"^\|\s*(?:OWNER\s+)?{re.escape(decision)}\s*\|.*$", re.M)
    m = row.search(decisions_text)
    return m.group(0) if m else None


def decision_exists(decision: str, decisions_text: str) -> bool:
    return _decision_row(decision, decisions_text) is not None


def decision_refreshes(decision: str, decisions_text: str) -> bool:
    """The decision's own text (a cell after its id) opens with "Refreshes held-out"."""
    row = _decision_row(decision, decisions_text)
    cells = [c.strip() for c in (row or "").strip().strip("|").split("|")][1:]
    return any(c.startswith(REFRESH_OPENING) for c in cells)


GENESIS = "0" * 64


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def chain_problems(path: Path) -> list[str]:
    """Each line names the sha256 of the line before it (`prev`), so an edited, dropped,
    inserted or reordered line breaks the chain."""
    prev = GENESIS
    out = []
    lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for i, line in enumerate(lines, start=1):
        if json.loads(line).get("prev") != prev:
            out.append(f"tuning log line {i} does not follow line {i - 1} (edited or reordered)")
        prev = _line_hash(line)
    return out


def _append(path: Path, row: dict[str, Any]) -> dict[str, Any]:
    if path.is_file() and chain_problems(path):
        raise TuningError("the tuning log's hash chain is broken: restore it before logging")
    lines = ([x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
             if path.is_file() else [])  # fmt: skip
    row = {**row, "prev": _line_hash(lines[-1]) if lines else GENESIS}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def log_heldout_scored(path: Path, *, run: str, bar: float, ground_version: str) -> dict[str, Any]:
    """Written by a live run that scored the held-out split (no decision needed: it is an
    event, not a change). A bar or rules change after it needs a fresh held-out set (§5.4)."""
    return _append(path, {"kind": "heldout_scored", "run": run, "bar": bar,
                          "ground_version": ground_version,
                          "at": dt.date.today().isoformat()})  # fmt: skip


def log_change(path: Path, entry: Mapping[str, Any], decisions_text: str) -> dict[str, Any]:
    """Append one change. It must name an existing decision and continue from the last logged
    value; held-out events carry no value."""
    kind = entry.get("kind")
    if kind not in KINDS:
        raise TuningError(f"unknown change kind {kind!r}")
    if kind == "heldout_scored":
        return log_heldout_scored(path, run=str(entry.get("run", "")),
                                  bar=float(entry.get("bar", 0.0)),
                                  ground_version=str(entry.get("ground_version", "")))  # fmt: skip
    if kind == "calibration_scored":
        raise TuningError("calibration scores are logged by scoring itself")
    decision = str(entry.get("decision", ""))
    if not decision_exists(decision, decisions_text):
        raise TuningError(f"decision {decision!r} is not a row of decisions.md")
    if kind == "heldout_refreshed" and not decision_refreshes(decision, decisions_text):
        raise TuningError(f"a heldout_refreshed entry needs a decision opening with "
                          f"{REFRESH_OPENING!r}; {decision} does not")  # fmt: skip
    log = read_log(path)
    bar, ground = current(log)
    row: dict[str, Any] = {"kind": kind, "decision": decision,
                           "at": str(entry.get("at") or dt.date.today().isoformat())}  # fmt: skip
    if kind in ("grounding_bar", "support_rules"):
        before = bar if kind == "grounding_bar" else ground
        if entry.get("from") != before:
            raise TuningError(f"a {kind} change must start from the logged value {before!r}")
        to = entry.get("to")
        if kind == "grounding_bar" and not (isinstance(to, float | int) and not isinstance(to, bool)
                                            and 0.0 <= to <= 1.0):  # fmt: skip
            raise TuningError("a grounding_bar change needs a bar between 0 and 1")
        if kind == "support_rules" and (not isinstance(to, str) or not to.startswith("ground-")):
            raise TuningError("a support_rules change names the new ground version")
        if to == before:
            raise TuningError("a change must change the value")
        ground_version = entry.get("ground_version") or (to if kind == "support_rules" else ground)
        row.update({"from": before, "to": to, "ground_version": ground_version})
        for k in ("tuning_digest", "n_cases", "results"):
            if k in entry:
                row[k] = entry[k]
    return _append(path, row)


def log_calibration_scored(path: Path, *, calibration: str, digest: str, rubric_hash: str,
                           run: str) -> dict[str, Any]:  # fmt: skip
    """One held-out calibration scoring: which held-out set (digest) under which rubric. The
    committed log is the one place this history lives, across run folders (CAL-01)."""
    return _append(path, {"kind": "calibration_scored", "calibration": calibration,
                          "digest": digest, "rubric_hash": rubric_hash, "run": run,
                          "at": dt.date.today().isoformat()})  # fmt: skip


def scored_rubrics(log: Sequence[Mapping[str, Any]], calibration: str, digest: str) -> set[str]:
    return {str(e["rubric_hash"]) for e in log if e["kind"] == "calibration_scored"
            and e.get("calibration") == calibration and e.get("digest") == digest}  # fmt: skip


def continuity_problems(log: Sequence[Mapping[str, Any]], decisions_text: str) -> list[str]:
    """Replays the log: every change continues from the value before it and names an existing
    decision; a refresh names a "Refreshes held-out" decision. A chain recomputed after a
    forged line still fails here."""
    bar: float | None = None
    ground: str | None = None
    out = []
    for i, e in enumerate(log, start=1):
        kind = e.get("kind")
        if kind not in KINDS:
            out.append(f"line {i}: unknown kind {kind!r}")
            continue
        if kind in ("grounding_bar", "support_rules", "heldout_refreshed") and not \
                decision_exists(str(e.get("decision", "")), decisions_text):  # fmt: skip
            out.append(f"line {i}: decision {e.get('decision')!r} is not a row of decisions.md")
        if kind == "heldout_refreshed" and not decision_refreshes(str(e.get("decision", "")),
                                                                  decisions_text):  # fmt: skip
            out.append(f"line {i}: heldout_refreshed needs a 'Refreshes held-out' decision")
        if kind == "grounding_bar":
            if e.get("from") != bar or e.get("to") == bar:
                out.append(f"line {i}: a grounding_bar change does not continue from {bar!r}")
            bar = float(e["to"])
        elif kind == "support_rules":
            to = e.get("to")
            if e.get("from") != ground or to == ground or not str(to).startswith("ground-"):
                out.append(f"line {i}: a support_rules change does not continue from {ground!r}")
            ground = str(to)
    return out


def unlogged(log: Sequence[Mapping[str, Any]], bar: float | None, ground_version: str) -> list[str]:
    """Differences between what runs (a config's bar, the code's ground version) and the log."""
    logged_bar, logged_ground = current(log)
    out = []
    if bar is not None and (logged_bar is None or not math.isclose(bar, logged_bar)):
        out.append(f"grounding_bar {bar} is not the logged bar {logged_bar}")
    if ground_version != logged_ground:
        out.append(f"ground version {ground_version} is not the logged {logged_ground} "
                   "(SUPPORT_RULES or the splitter changed without a logged change)")  # fmt: skip
    return out


def fresh_heldout_required(log: Sequence[Mapping[str, Any]]) -> bool:
    """True when the bar or the support rules changed after the held-out split was scored and
    no fresh held-out set has been written since (§5.4)."""
    scored = required = False
    for e in log:
        if e["kind"] == "heldout_scored":
            scored = True
        elif e["kind"] in ("grounding_bar", "support_rules") and scored:
            required = True
        elif e["kind"] == "heldout_refreshed":
            scored = required = False
    return required


def results_for_log(results: Sequence[BarResult]) -> list[dict[str, Any]]:
    return [{"bar": r.bar, "replayable": r.replayable, "grounded_rate": round(r.grounded_rate, 6),
             "handover_rate": round(r.handover_rate, 6), "n_b1": r.n_b1,
             "n_handover": r.n_handover} for r in results]  # fmt: skip
