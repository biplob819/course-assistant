"""Calibration of the course-ta judges and of the runtime support check (E2.3 CAL-01, CAL-03).

Groundedness reuses EvalKit A2.6 as it is: this module only picks the 100 answers (50 from
golden tuning cases, 50 from golden held-out cases, so rubric work never reads held-out cases,
D-5) and writes EvalKit's own blind sheet; `evalkit calibrate label` and `evalkit calibrate
score` then run unchanged and certify the run.

Integrity is a verdict on a whole conversation, not on claims, so it has a sheet of its own with
the same rules: opaque keys bound to the exact replies (L-12), redacted text, never a solution,
brief, verdict, rationale or model name, a blind relabel round, kappa per judge on the held-out
half only, and no rescoring of a held-out set under another rubric.

The runtime support check (A2.3's L2) is graded against the dual judges (§5.8): `SupportTap`
captures what L2 judged, `JudgePanel.support` asks EvalKit's groundedness judges about the same
sentences, and `support_agreement` gives kappa with a bootstrap CI over answers.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import random
import secrets
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any, Literal

from evalkit.pipeline.calibrate import (
    MIN_JUDGE_COVERAGE,
    SampledAnswer,
    _allocate,  # pyright: ignore[reportPrivateUsage]
    _opaque,  # pyright: ignore[reportPrivateUsage]
    _Pair,  # pyright: ignore[reportPrivateUsage]
    build_sheet,
    cluster_kappa,
    sheet_csv,
)
from evalkit.pipeline.judge import JUDGE_PROMPT_HASH, redact
from evalkit.schemas import Case, Interval, Record

from course_assistant.answer.ground import GroundStep, SupportParseError, parse_support
from course_assistant.answer.replay import Source
from course_assistant.eval.judges import JudgePanel, rubric_hash
from course_assistant.ingest.corpus import Corpus
from course_assistant.llm import LLMError, ResponsesRequest, ResponsesResult
from course_assistant.schemas._base import FrozenMap, Model, NonBlank, NonNegInt, PosInt

Split = Literal["tuning", "heldout"]
ProbeLabel = Literal["violation", "clean"]
MONITOR_KAPPA = 0.6  # §6.4: support-check agreement below this opens a lesson
# probe calibration covers the integrity judge only: its own rubric's hash (OWNER D-81)
RUBRIC_HASH = rubric_hash("integrity")


class CalibrationError(ValueError):
    """A sample, sheet, label file or report cannot be built. Messages name ids and counts."""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def split_of(case: Case, heldout_ids: AbstractSet[str]) -> Split:
    """A case's golden split, checked against the held-out MANIFEST ids: a listed case is
    held out whatever its meta says, and one marked tuning is refused."""
    split = case.meta.get("split")
    if split not in ("tuning", "heldout"):
        raise CalibrationError(f"case {case.id} has no golden split")
    if case.id in heldout_ids:
        if split != "heldout":
            raise CalibrationError(f"case {case.id} is in the held-out MANIFEST but marked {split}")
        return "heldout"
    return split  # type: ignore[return-value]


def _check_labelled_splits(splits: Mapping[str, str], heldout_ids: AbstractSet[str]) -> set[str]:
    """Held-out case ids of a labelled sheet; a MANIFEST case labelled tuning is refused."""
    wrong = sorted(c for c, s in splits.items() if c in heldout_ids and s != "heldout")
    if wrong:
        raise CalibrationError(f"{len(wrong)} case(s) in the held-out MANIFEST sit in the tuning "
                               "half of this sheet")  # fmt: skip
    return {c for c, s in splits.items() if s == "heldout" or c in heldout_ids}


def _check_sizes(n: int, n_relabel: int) -> None:
    if n <= 0 or n % 2 or n_relabel % 2 or not 0 <= n_relabel <= n:
        raise CalibrationError("n and n_relabel must be even, with 0 < n and n_relabel <= n")


def _take(strata: Mapping[tuple[str, str], list[str]], n: int, rng: random.Random) -> list[str]:
    """Largest-remainder allocation over the strata (EvalKit's), seeded shuffle within each."""
    pools = {k: sorted(v) for k, v in strata.items()}
    for k in sorted(pools):
        rng.shuffle(pools[k])
    take = _allocate({k: len(v) for k, v in pools.items()}, n)
    return [x for k in sorted(pools) for x in pools[k][: take[k]]]


def _halves(items: Mapping[Split, Mapping[tuple[str, str], list[str]]], n: int, n_relabel: int,
            seed: int, what: str) -> list[tuple[str, Split, bool]]:  # fmt: skip
    _check_sizes(n, n_relabel)
    rng = random.Random(f"course-ta-calibration:{what}:{seed}")
    half = n // 2
    out: list[tuple[str, Split, bool]] = []
    for split in ("tuning", "heldout"):
        strata = items.get(split, {})  # type: ignore[call-overload]
        have = sum(len(v) for v in strata.values())
        if have < half:
            raise CalibrationError(f"only {have} labellable {split} answers for {what}; "
                                   f"need {half}")  # fmt: skip
        chosen = _take(strata, half, rng)
        relabel = set(rng.sample(range(half), n_relabel // 2))
        out += [(key, split, i in relabel) for i, key in enumerate(chosen)]  # type: ignore[misc]
    return out


# =============================== groundedness (EvalKit A2.6) ===============================


def groundedness_sample(records: Sequence[Record], cases: Mapping[str, Case], *, seed: int,
                        heldout_ids: AbstractSet[str], n: int = 100,
                        n_relabel: int = 20) -> list[SampledAnswer]:  # fmt: skip
    """`n` answers that made claims: half from golden tuning cases, half from golden held-out
    cases, each half stratified by (rule, reference label), `n_relabel` split evenly."""
    keys = [f"{r.case_id}:{r.completion.model}" for r in records]
    if len(keys) != len(set(keys)):
        raise CalibrationError("duplicate records for one case and model")
    by_key = {f"{r.case_id}:{r.completion.model}": r for r in records}
    items: dict[Split, dict[tuple[str, str], list[str]]] = {"tuning": {}, "heldout": {}}
    for key, r in by_key.items():
        if not r.claims:
            continue
        case = cases[r.case_id]
        stratum = (str(case.meta.get("rule")), case.reference_label or "")
        items[split_of(case, heldout_ids)].setdefault(stratum, []).append(key)
    picked = _halves(items, n, n_relabel, seed, "groundedness")
    return [SampledAnswer(record_key=key, case_id=by_key[key].case_id,
                          model=by_key[key].completion.model, split=split, relabel=relabel)
            for key, split, relabel in picked]  # fmt: skip


def export_groundedness(run_dir: Path, *, heldout_ids: AbstractSet[str], force: bool = False,
                        n: int = 100, n_relabel: int = 20) -> tuple[Path, Path, int]:  # fmt: skip
    """EvalKit's `calibration/sheet.json` and `sheet.csv` for our split-aligned sample; then
    `evalkit calibrate label|score RUN_DIR` run on it unchanged."""
    from evalkit import run as R

    manifest = R.read_manifest(run_dir)
    if manifest.finished_at is None:
        raise CalibrationError("judging is still pending: finish the run before calibrating")
    cal = run_dir / "calibration"
    sheet = cal / "sheet.json"
    if sheet.is_file() and any(cal.glob("labels-*.json")):
        raise CalibrationError(
            f"{sheet} already has labels; a held-out set is scored once per "
            "rubric, so a new export needs a fresh held-out set"
        )
    if sheet.is_file() and not force:
        raise CalibrationError(f"{sheet} exists (force to replace it; no labels yet)")
    records, cases = R.read_records(run_dir), R.read_cases(run_dir)
    sample = groundedness_sample(records, cases, seed=manifest.seed, n=n, n_relabel=n_relabel,
                                 heldout_ids=heldout_ids)  # fmt: skip
    rows = build_sheet(sample, records, cases, seed=manifest.seed)
    _write(sheet, json.dumps([r.model_dump(mode="json") for r in rows], indent=1) + "\n")
    _write(cal / "sheet.csv", sheet_csv(rows))
    return sheet, cal / "sheet.csv", len(rows)


def score_groundedness(
    run_dir: Path, *, log: Path, heldout_ids: AbstractSet[str], labeller: str = "owner"
) -> Any:
    """The only supported way to certify groundedness: EvalKit's `cal_score`, guarded first by
    the committed per-case history (a held-out case already scored under another judge rubric
    is refused before scoring) and logged after. A run certified by `evalkit calibrate score`
    directly has no log line, and `certified_calibration` treats it as uncalibrated."""
    from evalkit import run as R
    from evalkit.pipeline.calibrate import heldout_digest as ek_digest
    from evalkit.pipeline.calibrate import import_labels
    from evalkit.pipeline.labeller import load_labels

    from course_assistant.eval.tuning import (
        cases_scored_elsewhere,
        log_calibration_scored,
        read_log,
    )

    rows = R._load_sheet(run_dir)  # pyright: ignore[reportPrivateUsage]
    first = load_labels(run_dir / "calibration" / "labels-first.json")
    if first is None:
        raise CalibrationError("no first-round groundedness labels yet")
    relabel = load_labels(run_dir / "calibration" / "labels-relabel.json")
    labels = import_labels(rows, first, relabel)
    splits = {lab.claim_id.split(":", 1)[0]: lab.split for lab in labels if lab.split != "relabel"}
    held = _check_labelled_splits(splits, heldout_ids)
    clash = cases_scored_elsewhere(read_log(log), "groundedness", held, JUDGE_PROMPT_HASH)
    if clash:
        raise CalibrationError(f"{len(clash)} held-out case(s) were already scored under another "
                               "rubric (tuning log); a changed rubric needs a fresh held-out set "
                               "(Tech Spec §5.7)")  # fmt: skip
    report = R.cal_score(run_dir)
    log_calibration_scored(log, calibration="groundedness", digest=ek_digest(labels),
                           rubric_hash=JUDGE_PROMPT_HASH, run=run_dir.name,
                           heldout_cases=held, labeller=labeller)  # fmt: skip
    return report


# =============================== integrity (probe) labels ===============================


class ProbeSample(Model):
    case_id: NonBlank
    split: Split
    relabel: bool


class ProbeRow(Model):
    sheet_id: NonBlank
    key: NonBlank  # opaque, bound to the case and the exact replies shown
    split: Split
    relabel: bool
    case_id: NonBlank  # private: joins labels to verdicts; never in the CSV
    assessment_id: NonBlank
    replies: tuple[str, ...]  # every assistant reply of the conversation, redacted


class ProbeLabelFile(Model):
    sheet_id: NonBlank
    round: Literal["first", "relabel"]
    labels: FrozenMap[ProbeLabel]


class ProbeAgreement(Model):
    judge: NonBlank
    split: Literal["heldout"] = "heldout"
    n: PosInt
    coverage: float
    kappa: Interval
    agreement: float
    confusion: tuple[tuple[NonNegInt, NonNegInt], tuple[NonNegInt, NonNegInt]]  # violation +


class ProbeCalibration(Model):
    kind: NonBlank
    rubric_hash: NonBlank
    scored_at: dt.datetime
    n_heldout: PosInt
    heldout_digest: NonBlank
    agreements: tuple[ProbeAgreement, ...]
    self_consistency: Interval | None = None


def _replies(case: Case, record: Record) -> tuple[str, ...]:
    assert record.completion.answer is not None
    prior: Any = case.meta.get("prior_turns") or ()
    return (*(redact(str(t[1])) for t in prior), redact(record.completion.answer.answer))


def _applies(record: Record, kind: str) -> bool:
    ts = record.task_scores.get(kind)
    return (ts is not None and ts.details.get("applicable") is not False
            and record.completion.answer is not None)  # fmt: skip


def probe_sample(records: Sequence[Record], cases: Mapping[str, Case], *, kind: str = "integrity",
                 seed: int, heldout_ids: AbstractSet[str], n: int = 100,
                 n_relabel: int = 20) -> list[ProbeSample]:  # fmt: skip
    """`n` answers the probe judged (B3 for integrity), half from golden tuning cases and half
    from golden held-out cases, stratified by assessment."""
    items: dict[Split, dict[tuple[str, str], list[str]]] = {"tuning": {}, "heldout": {}}
    for r in records:
        if not _applies(r, kind):
            continue
        case = cases[r.case_id]
        stratum = (str(case.meta.get("assessment_id") or case.meta.get("rule")), "")
        items[split_of(case, heldout_ids)].setdefault(stratum, []).append(r.case_id)
    picked = _halves(items, n, n_relabel, seed, kind)
    return [ProbeSample(case_id=c, split=s, relabel=rl) for c, s, rl in picked]


def build_probe_sheet(sample: Sequence[ProbeSample], records: Sequence[Record],
                      cases: Mapping[str, Case], *, seed: int,
                      secret: str | None = None) -> list[ProbeRow]:  # fmt: skip
    secret = secret or secrets.token_hex(16)
    sheet_id = _opaque("s", secret, "probe-sheet")
    by_case = {r.case_id: r for r in records}
    order = list(sample)
    random.Random(f"course-ta-probe-sheet:{seed}").shuffle(order)
    rows = []
    for s in order:
        case, rec = cases[s.case_id], by_case[s.case_id]
        replies = _replies(case, rec)
        rows.append(ProbeRow(sheet_id=sheet_id,
                             key=_opaque("k", secret, json.dumps([s.case_id, replies])),
                             split=s.split, relabel=s.relabel, case_id=s.case_id,
                             assessment_id=str(case.meta.get("assessment_id") or "none"),
                             replies=replies))  # fmt: skip
    return rows


PROBE_CSV_COLUMNS = ("key", "split", "assessment_id", "replies", "label")


def _replies_cell(row: ProbeRow) -> str:
    return "\n\n".join(f"[{i}] {r}" for i, r in enumerate(row.replies, start=1))


def probe_sheet_csv(rows: Sequence[ProbeRow], *, relabel: bool = False, seed: int = 0) -> str:
    """The blind CSV (first round), or the relabel rows alone in a new order, labels blank."""
    shown = list(rows)
    if relabel:
        shown = [r for r in rows if r.relabel]
        random.Random(f"course-ta-probe-relabel:{seed}").shuffle(shown)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(PROBE_CSV_COLUMNS)
    for r in shown:
        w.writerow([r.key, r.split, r.assessment_id, _replies_cell(r), ""])
    return buf.getvalue()


_LABELS: dict[str, ProbeLabel] = {"v": "violation", "violation": "violation", "c": "clean",
                                  "clean": "clean"}  # fmt: skip


Round = Literal["first", "relabel"]


def probe_labels_from_csv(text: str, rows: Sequence[ProbeRow],
                          round_: Round = "first") -> ProbeLabelFile:  # fmt: skip
    """Read a filled sheet. Unknown or repeated keys, edited replies, relabel keys outside the
    relabel set and labels other than V/C are errors; a blank label is still to do."""
    if not rows:
        raise CalibrationError("empty sheet")
    by_key = {r.key: r for r in rows}
    out: dict[str, ProbeLabel] = {}
    seen: set[str] = set()
    for i, line in enumerate(csv.DictReader(io.StringIO(text.lstrip("﻿"))), start=2):
        key = line.get("key") or ""
        row = by_key.get(key)
        if row is None:
            raise CalibrationError(f"line {i}: unknown key (labels from another sheet?)")
        if key in seen:
            raise CalibrationError(f"line {i}: key {key} appears twice")
        seen.add(key)
        if round_ == "relabel" and not row.relabel:
            raise CalibrationError(f"line {i}: key {key} is not in the relabel set")
        if (line.get("replies") or "") != _replies_cell(row):
            raise CalibrationError(f"line {i}: replies were edited; labels must refer to them")
        raw = (line.get("label") or "").strip().casefold()
        if not raw:
            continue
        if raw not in _LABELS:
            raise CalibrationError(f"line {i}: label must be V or C")
        out[key] = _LABELS[raw]
    return ProbeLabelFile(sheet_id=rows[0].sheet_id, round=round_, labels=out)


def _judge_verdicts(records: Iterable[Record], kind: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for r in records:
        ts = r.task_scores.get(kind)
        judges: Any = ts.details.get("judges") if ts is not None else None
        if isinstance(judges, Mapping):
            out[r.case_id] = {str(j): str(v) for j, v in judges.items()}
    return out


def heldout_digest(rows: Sequence[ProbeRow]) -> str:
    """sha256 of the sorted held-out case ids: stable across exports (sheet keys are fresh
    random values each time), so a re-exported held-out set keeps its digest, as EvalKit's
    digest of claim ids does."""
    ids = sorted(r.case_id for r in rows if r.split == "heldout")
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def _verdict_rubrics(records: Iterable[Record], kind: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for r in records:
        ts = r.task_scores.get(kind)
        if ts is not None and isinstance(ts.details.get("judges"), Mapping):
            rubric: Any = ts.details.get("rubric_hash")
            out[r.case_id] = rubric if isinstance(rubric, str) else None
    return out


def probe_report(rows: Sequence[ProbeRow], first: ProbeLabelFile, relabel: ProbeLabelFile | None,
                 records: Sequence[Record], *, kind: str = "integrity", seed: int,
                 scored_at: dt.datetime, logged: Sequence[Mapping[str, Any]],
                 heldout_ids: AbstractSet[str], previous: Sequence[ProbeCalibration] = (),
                 n_resamples: int = 10_000,
                 min_coverage: float = MIN_JUDGE_COVERAGE) -> ProbeCalibration:  # fmt: skip
    """Per-judge kappa against the owner's first-round labels on the held-out half only."""
    if not rows:
        raise CalibrationError("empty sheet")
    sheet_id = rows[0].sheet_id
    for f, want in ((first, "first"), (relabel, "relabel")):
        if f is None:
            continue
        if f.sheet_id != sheet_id:
            raise CalibrationError(f"{want}-round labels belong to another sheet")
        if f.round != want:
            raise CalibrationError(f"expected {want}-round labels, got {f.round}-round labels")
    by_key = {r.key: r for r in rows}
    unknown = [k for k in [*first.labels, *(relabel.labels if relabel else {})] if k not in by_key]
    if unknown:
        raise CalibrationError(f"labels for {len(unknown)} unknown key(s)")
    missing = [r.key for r in rows if r.key not in first.labels]
    if missing:
        raise CalibrationError(f"{len(missing)} row(s) have no first-round label")
    if relabel is not None and any(not by_key[k].relabel for k in relabel.labels):
        raise CalibrationError("relabel-round labels outside the relabel set")
    digest = heldout_digest(rows)
    from course_assistant.eval.tuning import cases_scored_elsewhere

    held_cases = _check_labelled_splits({r.case_id: r.split for r in rows}, heldout_ids)
    clash = cases_scored_elsewhere(logged, kind, held_cases, RUBRIC_HASH)
    if clash:
        raise CalibrationError(f"{len(clash)} held-out case(s) were already scored under another "
                               "rubric (tuning log); a changed rubric needs a fresh held-out set "
                               "(Tech Spec §5.4)")  # fmt: skip
    for old in previous:
        if old.heldout_digest == digest and old.rubric_hash != RUBRIC_HASH:
            raise CalibrationError("this held-out set was already scored under another rubric; "
                                   "label a fresh held-out set (Tech Spec §5.4)")  # fmt: skip
    verdicts = _judge_verdicts(records, kind)
    rubrics = _verdict_rubrics(records, kind)
    stale = [r.case_id for r in rows if r.case_id in rubrics and rubrics[r.case_id] != RUBRIC_HASH]
    if stale:  # never stamp old verdicts with the current rubric
        raise CalibrationError(f"{len(stale)} verdict(s) were made under a different rubric than "
                               "the current one: re-judge the run before scoring")  # fmt: skip
    heldout = [r for r in rows if r.split == "heldout"]
    judges = sorted({j for r in heldout for j in verdicts.get(r.case_id, {})})
    if not judges:
        raise CalibrationError(f"no {kind} judge verdicts on the held-out rows")
    agreements = []
    for judge in judges:
        covered = [r for r in heldout if judge in verdicts.get(r.case_id, {})]
        coverage = len(covered) / len(heldout)
        if coverage < min_coverage:
            raise CalibrationError(f"{judge} ruled on {len(covered)} of {len(heldout)} held-out "
                                   f"rows; at least {min_coverage:.0%} is needed")  # fmt: skip
        pairs = [_Pair(r.key, first.labels[r.key] == "violation",
                       verdicts[r.case_id][judge] == "violation") for r in covered]  # fmt: skip
        tp = sum(p.human and p.judge for p in pairs)
        fn = sum(p.human and not p.judge for p in pairs)
        fp = sum(not p.human and p.judge for p in pairs)
        tn = len(pairs) - tp - fn - fp
        agreements.append(ProbeAgreement(
            judge=judge, n=len(pairs), coverage=coverage,
            kappa=cluster_kappa(pairs, seed=seed, n_resamples=n_resamples),
            agreement=(tp + tn) / len(pairs), confusion=((tp, fn), (fp, tn))))  # fmt: skip
    consistency = None
    if relabel is not None and relabel.labels:
        pairs = [_Pair(k, first.labels[k] == "violation", v == "violation")
                 for k, v in relabel.labels.items()]  # fmt: skip
        consistency = cluster_kappa(pairs, seed=seed, n_resamples=n_resamples)
    return ProbeCalibration(kind=kind, rubric_hash=RUBRIC_HASH, scored_at=scored_at,
                            n_heldout=len(heldout), heldout_digest=digest,
                            agreements=tuple(agreements),
                            self_consistency=consistency)  # fmt: skip


# ---------- integrity files on a run folder ----------


def probe_dir(run_dir: Path, kind: str = "integrity") -> Path:
    return run_dir / "calibration" / kind


def _load_rows(run_dir: Path, kind: str) -> list[ProbeRow]:
    path = probe_dir(run_dir, kind) / "sheet.json"
    if not path.is_file():
        raise CalibrationError(f"no {kind} sheet yet: export it first")
    return [ProbeRow.model_validate_json(json.dumps(x))
            for x in json.loads(path.read_text(encoding="utf-8"))]  # fmt: skip


def export_probe(run_dir: Path, *, heldout_ids: AbstractSet[str], kind: str = "integrity",
                 force: bool = False, n: int = 100, n_relabel: int = 20) -> int:  # fmt: skip
    from evalkit import run as R

    manifest = R.read_manifest(run_dir)
    if manifest.finished_at is None:
        raise CalibrationError("judging is still pending: finish the run before calibrating")
    d = probe_dir(run_dir, kind)
    if (d / "sheet.json").is_file() and any(d.glob("labels-*.json")):
        raise CalibrationError(
            f"the {kind} sheet already has labels; a held-out set is scored "
            "once per rubric, so a new export needs a fresh held-out set"
        )
    if (d / "sheet.json").is_file() and not force:
        raise CalibrationError(f"the {kind} sheet exists (force to replace it; no labels yet)")
    records, cases = R.read_records(run_dir), R.read_cases(run_dir)
    sample = probe_sample(records, cases, kind=kind, seed=manifest.seed, n=n,
                          n_relabel=n_relabel, heldout_ids=heldout_ids)  # fmt: skip
    rows = build_probe_sheet(sample, records, cases, seed=manifest.seed)
    _write(d / "sheet.json", json.dumps([r.model_dump(mode="json") for r in rows], indent=1) + "\n")
    _write(d / "sheet.csv", probe_sheet_csv(rows))
    _write(d / "relabel.csv", probe_sheet_csv(rows, relabel=True, seed=manifest.seed))
    return len(rows)


def import_probe(run_dir: Path, csv_path: Path, *, kind: str = "integrity",
                 relabel: bool = False) -> int:  # fmt: skip
    rows = _load_rows(run_dir, kind)
    labels = probe_labels_from_csv(csv_path.read_text(encoding="utf-8"), rows,
                                   "relabel" if relabel else "first")  # fmt: skip
    target = probe_dir(run_dir, kind) / ("labels-relabel.json" if relabel else "labels-first.json")
    if target.is_file():
        raise CalibrationError(f"{target.name} already exists; move it aside to import again")
    _write(target, labels.model_dump_json(indent=1) + "\n")
    return len(labels.labels)


def score_probe(run_dir: Path, *, log: Path, heldout_ids: AbstractSet[str],
                kind: str = "integrity", labeller: str = "owner",
                now: dt.datetime | None = None) -> ProbeCalibration:  # fmt: skip
    """Score once per held-out set and rubric. `log` is the committed tuning log: it holds the
    digest-to-rubric history across run folders, and every scoring is appended to it."""
    from evalkit import run as R

    from course_assistant.eval.tuning import log_calibration_scored, read_log

    d = probe_dir(run_dir, kind)
    rows = _load_rows(run_dir, kind)
    first_path, relabel_path = d / "labels-first.json", d / "labels-relabel.json"
    if not first_path.is_file():
        raise CalibrationError(f"no first-round {kind} labels: import the filled sheet first")
    first = ProbeLabelFile.model_validate_json(first_path.read_text(encoding="utf-8"))
    relabel = (ProbeLabelFile.model_validate_json(relabel_path.read_text(encoding="utf-8"))
               if relabel_path.is_file() else None)  # fmt: skip
    hist_path = d / "calibration.json"
    history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.is_file() else {}
    previous = [ProbeCalibration.model_validate_json(json.dumps(x))
                for x in history.get("history", [])]  # fmt: skip
    report = probe_report(rows, first, relabel, R.read_records(run_dir), kind=kind,
                          seed=R.read_manifest(run_dir).seed,
                          scored_at=now or dt.datetime.now(dt.UTC), previous=previous,
                          logged=read_log(log), heldout_ids=heldout_ids)  # fmt: skip
    log_calibration_scored(
        log,
        calibration=kind,
        digest=report.heldout_digest,
        rubric_hash=report.rubric_hash,
        run=run_dir.name,
        heldout_cases=[r.case_id for r in rows if r.split == "heldout"],
        labeller=labeller,
    )
    entry = json.loads(report.model_dump_json())
    kept = [*history.get("history", []), entry]
    _write(hist_path, json.dumps({"current": entry, "history": kept}, indent=1) + "\n")
    return report


# =============================== what counts as calibrated ===============================


def _logged_by(log: Path, calibration: str, digest: str, rubric: str, run: str) -> str | None:
    """Who labelled the matching `calibration_scored` line ("owner" for lines written before
    D-76), or None when no line matches."""
    from course_assistant.eval.tuning import read_log

    hits = [str(e.get("labeller", "owner")) for e in read_log(log)
            if e["kind"] == "calibration_scored" and e.get("calibration") == calibration
            and e.get("digest") == digest and e.get("rubric_hash") == rubric
            and e.get("run") == run]  # fmt: skip
    return hits[-1] if hits else None


def _logged_for(log: Path, calibration: str, digest: str, rubric: str, run: str) -> bool:
    return _logged_by(log, calibration, digest, rubric, run) is not None


def certified_calibration(run_dir: Path, *, log: Path) -> dict[str, str]:
    """`calibrated` or `uncalibrated` per calibration, as our checks see it: a calibration
    counts only if the committed log holds a `calibration_scored` line for that held-out set
    and rubric. EvalKit's own `calibration_status` is not enough: `evalkit calibrate score`
    run directly certifies a folder without the per-case guard (verifier E2.3 pass 4). The
    line must name this run folder and the current rubric (`JUDGE_PROMPT_HASH` for
    groundedness, `RUBRIC_HASH` for integrity), so a copied folder or a changed rubric reads
    as uncalibrated (E2.4 should-fix 2). The release gate (E2.4) and the regression gate read
    this."""
    from evalkit import run as R

    out = {"groundedness": "uncalibrated", "integrity": "uncalibrated"}
    report = R.read_calibration(run_dir)
    summary_path = run_dir / "summary.json"
    status = (json.loads(summary_path.read_text(encoding="utf-8")).get("calibration_status")
              if summary_path.is_file() else None)  # fmt: skip
    if report is not None and status == "calibrated" \
            and report.judge_prompt_hash == JUDGE_PROMPT_HASH and _logged_for(
                log, "groundedness", report.heldout_digest, JUDGE_PROMPT_HASH,
                run_dir.name):  # fmt: skip
        out["groundedness"] = "calibrated"
        out["groundedness_labeller"] = (
            _logged_by(log, "groundedness", report.heldout_digest, JUDGE_PROMPT_HASH, run_dir.name)
            or "owner"
        )
    probe = probe_dir(run_dir) / "calibration.json"
    if probe.is_file():
        current = json.loads(probe.read_text(encoding="utf-8")).get("current") or {}
        if current.get("rubric_hash") == RUBRIC_HASH and _logged_for(
                log, "integrity", str(current.get("heldout_digest")), RUBRIC_HASH,
                run_dir.name):  # fmt: skip
            out["integrity"] = "calibrated"
            out["integrity_labeller"] = (
                _logged_by(
                    log, "integrity", str(current.get("heldout_digest")), RUBRIC_HASH, run_dir.name
                )
                or "owner"
            )
    return out


# =============================== runtime support check (§5.8) ===============================


class TappedSentence(Model):
    request_id: NonBlank
    n: PosInt
    text: str
    units: tuple[PosInt, ...]
    runtime_supported: bool


def _data_block(request: ResponsesRequest) -> list[tuple[str, tuple[int, ...]]]:
    content = request.input[0].content
    start, end = content.index("<data>") + len("<data>"), content.rindex("</data>")
    doc = json.loads(content[start:end])
    return [(str(s["text"]), tuple(int(str(u)[1:]) for u in s["units"]))
            for s in doc["sentences"]]  # fmt: skip


class SupportTap:
    """A `Source` that passes every call through and keeps what the support step judged: each
    sentence, its cited units and the runtime verdict. The last parsed call of a request wins
    (a parse retry); when every attempt fails to parse, the runtime fails closed, and so does
    the tap (every sentence unsupported)."""

    def __init__(self, source: Source) -> None:
        self.source = source
        self._lock = threading.Lock()
        self._parsed: dict[str, list[TappedSentence]] = {}
        self._failures: dict[str, int] = defaultdict(int)

    def call(self, request_id: str, request: ResponsesRequest) -> ResponsesResult:
        if request.purpose != "support":
            return self.source.call(request_id, request)
        items = _data_block(request)
        try:
            result = self.source.call(request_id, request)
        except LLMError as exc:
            if exc.kind == "parse":
                self._failed(request_id, items)
            raise
        try:
            verdicts = parse_support(result, len(items))
        except SupportParseError:
            self._failed(request_id, items)
            return result
        self._keep(request_id, items, verdicts)
        return result

    def _failed(self, request_id: str, items: list[tuple[str, tuple[int, ...]]]) -> None:
        with self._lock:
            self._failures[request_id] += 1
            give_up = (self._failures[request_id] >= GroundStep.MAX_PARSE_ATTEMPTS
                       and request_id not in self._parsed)  # fmt: skip
        if give_up:
            self._keep(request_id, items, [False] * len(items))

    def _keep(self, request_id: str, items: list[tuple[str, tuple[int, ...]]],
              verdicts: Sequence[bool]) -> None:  # fmt: skip
        rows = [TappedSentence(request_id=request_id, n=i, text=text, units=units,
                               runtime_supported=ok)
                for i, ((text, units), ok) in enumerate(zip(items, verdicts, strict=True),
                                                         start=1)]  # fmt: skip
        with self._lock:
            self._parsed[request_id] = rows

    @property
    def sentences(self) -> list[TappedSentence]:
        with self._lock:
            return [s for rid in sorted(self._parsed) for s in self._parsed[rid]]

    def save(self, path: Path) -> None:
        _write(path, "".join(s.model_dump_json() + "\n" for s in self.sentences))


def load_tap(path: Path) -> list[TappedSentence]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [TappedSentence.model_validate_json(x) for x in lines if x.strip()]


Judged = Mapping[tuple[str, int], Mapping[str, bool]]


def judge_support(tapped: Sequence[TappedSentence], panel: JudgePanel,
                  corpus: Corpus) -> dict[tuple[str, int], dict[str, bool]]:  # fmt: skip
    """Both judges on every tapped sentence, with the text of the units it cites."""
    by_index = {u.unit_index: u.text for u in corpus.units}
    out: dict[tuple[str, int], dict[str, bool]] = {}
    for s in tapped:
        missing = [i for i in s.units if i not in by_index]
        if missing:
            raise CalibrationError(f"{s.request_id} sentence {s.n} cites units not in the corpus")
        vs = panel.support(s.text, [(i, by_index[i]) for i in s.units])
        out[(s.request_id, s.n)] = {v.judge: v.supported for v in vs}
    return out


class SupportAgreement(Model):
    n_sentences: PosInt
    n_answers: PosInt
    judges: tuple[NonBlank, ...]
    kappa: Interval  # runtime L2 against the dual verdict (supported only if every judge says so)
    by_judge: FrozenMap[Interval]
    agreement: float
    confusion: tuple[tuple[NonNegInt, NonNegInt], tuple[NonNegInt, NonNegInt]]  # supported +
    judge_prompt_hash: NonBlank
    ground_version: NonBlank
    monitor_breach: bool  # kappa below 0.6 opens a lesson (§6.4)


def support_rows(tapped: Sequence[TappedSentence], judged: Judged) -> list[dict[str, Any]]:
    """The text-free record of a support comparison (the regression fixture's support.jsonl)."""
    rows = []
    for s in tapped:
        vs = judged.get((s.request_id, s.n))
        if not vs:
            raise CalibrationError(f"{s.request_id} sentence {s.n} has no dual verdict")
        rows.append({"request_id": s.request_id, "n": s.n, "runtime": s.runtime_supported,
                     "judges": dict(sorted(vs.items()))})  # fmt: skip
    return rows


def agreement_from_rows(rows: Sequence[Mapping[str, Any]], *, seed: int,
                        n_resamples: int = 10_000) -> SupportAgreement:  # fmt: skip
    if not rows:
        raise CalibrationError("no tapped support sentences to compare")
    judges = sorted({str(j) for r in rows for j in r["judges"]})
    for r in rows:
        if sorted(r["judges"]) != judges:
            raise CalibrationError(f"{r['request_id']} sentence {r['n']} lacks a judge's verdict")
    dual = [_Pair(str(r["request_id"]), bool(r["runtime"]), all(r["judges"].values()))
            for r in rows]  # fmt: skip
    by_judge = {j: cluster_kappa([_Pair(str(r["request_id"]), bool(r["runtime"]),
                                        bool(r["judges"][j])) for r in rows],
                                 seed=seed, n_resamples=n_resamples)
                for j in judges}  # fmt: skip
    tp = sum(p.human and p.judge for p in dual)
    fn = sum(p.human and not p.judge for p in dual)
    fp = sum(not p.human and p.judge for p in dual)
    tn = len(dual) - tp - fn - fp
    kappa = cluster_kappa(dual, seed=seed, n_resamples=n_resamples)
    return SupportAgreement(n_sentences=len(dual), n_answers=len({p.answer for p in dual}),
                            judges=tuple(judges), kappa=kappa, by_judge=by_judge,
                            agreement=(tp + tn) / len(dual), confusion=((tp, fn), (fp, tn)),
                            judge_prompt_hash=JUDGE_PROMPT_HASH,
                            ground_version=GroundStep.version(),
                            monitor_breach=kappa.point < MONITOR_KAPPA)  # fmt: skip


def support_agreement(tapped: Sequence[TappedSentence], judged: Judged, *, seed: int,
                      n_resamples: int = 10_000) -> SupportAgreement:  # fmt: skip
    """Runtime L2 against the dual judges: kappa with a bootstrap CI over answers (§5.8). A
    tapped sentence with no dual verdict is an error, never skipped (L-12)."""
    return agreement_from_rows(support_rows(tapped, judged), seed=seed, n_resamples=n_resamples)
