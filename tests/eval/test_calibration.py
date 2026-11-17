"""E2.3 calibration and bar tuning: CAL-01, CAL-02, CAL-03.

Offline only: the public golden split (some public cases are marked held-out inside a temporary
run folder, as a stand-in: tests never read golden/heldout), scripted model outputs through
N1.2's real `respond`, A2.5 replays and fake EvalKit judges.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from evalkit import run as R
from evalkit.pipeline import judge as ek_judge
from evalkit.pipeline.calibrate import LabelEntry, LabelFile, kappa_from
from evalkit.pipeline.labeller import save_labels
from evalkit.schemas import Case, Record

from course_assistant.answer import golden
from course_assistant.answer.ground import GroundStep
from course_assistant.answer.replay import Replay
from course_assistant.eval import calibrate as C
from course_assistant.eval import dataset, scorers, system, tuning
from course_assistant.eval.calibrate import CalibrationError, SupportTap
from course_assistant.eval.judges import RUBRIC_HASH, JudgeError
from course_assistant.eval.regression import PlaceholderSolutions
from tests.answer import scripted as s
from tests.eval import offline_run as O
from tests.eval import scripted_eval as E

ROOT = Path(__file__).resolve().parents[2]
DRAFT = "FULL-DRAFT"  # a hint reply holding this is judged a violation by both probe judges
TUNING_LOG = ROOT / "docs" / "measurements" / "tuning-log.jsonl"
DECISIONS = (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")
# held-out case ids from the committed MANIFEST (ids only, never held-out text)
MANIFEST_IDS = dataset.heldout_ids(ROOT / "golden" / "heldout" / "MANIFEST.json")


@pytest.fixture(autouse=True)
def _reset() -> Any:
    yield
    system.configure(None)
    scorers.configure_judges(None)


# ---------- one offline run shared by the CAL-01 tests ----------

ANSWERED = [E.first("B1", status="answered", skip=i) for i in range(12)] + [
    E.first("B10", status="answered", skip=i) for i in range(4)
]
PROBES = [E.first("B3", skip=i) for i in range(12)]
HELD = {c.id for c in ANSWERED[:6] + ANSWERED[12:14] + PROBES[:6]}  # stand-ins for held-out


def _cal_scripts() -> dict[str, dict[str, list[Any]]]:
    scripts = {c.id: E.script_for(c) for c in ANSWERED + PROBES}
    for c in (PROBES[0], PROBES[3], PROBES[7]):  # replies that draft the brief
        scripts[c.id] = E.hint(c, f"{DRAFT}: here is the whole submission, section by section.")
    return scripts


@pytest.fixture(scope="module")
def cal_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    run = O.offline_run(tmp_path_factory.mktemp("cal"), ANSWERED + PROBES, _cal_scripts(),
                        violation_mark=DRAFT)  # fmt: skip
    O.mark_heldout(run.run_dir, HELD)
    return run.run_dir


def fresh(cal_run: Path, tmp_path: Path) -> Path:
    out = tmp_path / "run"
    shutil.copytree(cal_run, out)
    return out


# ======================= CAL-01 groundedness (EvalKit A2.6) =======================


def test_groundedness_sample_takes_each_half_from_its_golden_split(cal_run):
    records, cases = R.read_records(cal_run), R.read_cases(cal_run)
    sample = C.groundedness_sample(
        records, cases, seed=7, n=12, n_relabel=4, heldout_ids=MANIFEST_IDS
    )
    assert len(sample) == 12 and len({x.record_key for x in sample}) == 12
    for x in sample:  # rubric work (tuning half) never reads a held-out case (D-5)
        assert x.split == cases[x.case_id].meta["split"]
    assert sum(x.split == "heldout" for x in sample) == 6
    assert sum(x.relabel for x in sample if x.split == "tuning") == 2
    assert sum(x.relabel for x in sample if x.split == "heldout") == 2
    assert all(next(r for r in records if r.case_id == x.case_id).claims for x in sample)
    with pytest.raises(CalibrationError, match="need 30"):
        C.groundedness_sample(records, cases, seed=7, n=60, n_relabel=4, heldout_ids=MANIFEST_IDS)
    with pytest.raises(CalibrationError, match="even"):
        C.groundedness_sample(records, cases, seed=7, n=11, n_relabel=4, heldout_ids=MANIFEST_IDS)


def test_groundedness_sample_is_reproducible_by_seed(cal_run):
    records, cases = R.read_records(cal_run), R.read_cases(cal_run)
    a = C.groundedness_sample(records, cases, seed=7, n=12, n_relabel=4, heldout_ids=MANIFEST_IDS)
    b = C.groundedness_sample(
        list(reversed(records)), cases, seed=7, n=12, n_relabel=4, heldout_ids=MANIFEST_IDS
    )
    assert a == b


@pytest.mark.contract
def test_evalkit_labels_and_scores_the_exported_groundedness_sheet(cal_run, tmp_path):
    run_dir = fresh(cal_run, tmp_path)
    sheet, csv_path, n = C.export_groundedness(run_dir, n=12, n_relabel=4, heldout_ids=MANIFEST_IDS)
    assert sheet == run_dir / "calibration" / "sheet.json" and n > 0
    with pytest.raises(CalibrationError, match="exists"):
        C.export_groundedness(run_dir, n=12, n_relabel=4, heldout_ids=MANIFEST_IDS)

    # the owner labels in EvalKit's page or CSV; simulated: every claim supported, one not
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
    for i, row in enumerate(rows):
        row["label"] = "U" if i == 0 else "S"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    filled = tmp_path / "filled.csv"
    filled.write_text(buf.getvalue(), encoding="utf-8")
    assert R.cal_import(run_dir, filled) == len(rows)
    sheet_rows = R._load_sheet(run_dir)  # pyright: ignore[reportPrivateUsage]
    relabel = {r.key: LabelEntry(label="supported") for r in sheet_rows if r.relabel}
    save_labels(
        run_dir / "calibration" / "labels-relabel.json",
        LabelFile(sheet_id=sheet_rows[0].sheet_id, round="relabel", labels=relabel),
    )
    report = R.cal_score(run_dir)  # EvalKit's own scoring, unchanged
    assert {a.judge for a in report.agreements} == {"claude-opus-5-5", "gpt-6.1-sol"}
    assert all(a.split == "heldout" for a in report.agreements)
    assert report.self_consistency is not None
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["calibration_status"] == "calibrated"
    with pytest.raises(CalibrationError, match="labels"):
        C.export_groundedness(
            run_dir, n=12, n_relabel=4, force=True, heldout_ids=MANIFEST_IDS
        )  # labels fit this sheet


def _label_groundedness(run_dir: Path, tmp: Path) -> None:
    _, csv_path, _ = C.export_groundedness(run_dir, n=12, n_relabel=4, heldout_ids=MANIFEST_IDS)
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
    for row in rows:
        row["label"] = "S"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    filled = tmp / f"g-{run_dir.parent.name}.csv"
    filled.write_text(buf.getvalue(), encoding="utf-8")
    R.cal_import(run_dir, filled)


# ======================= CAL-01 integrity (probe) labels =======================


def _sheet(cal_run: Path, secret: str = "s3cret") -> tuple[list[C.ProbeRow], list[Record]]:
    records, cases = R.read_records(cal_run), R.read_cases(cal_run)
    sample = C.probe_sample(records, cases, seed=7, n=8, n_relabel=4, heldout_ids=MANIFEST_IDS)
    return C.build_probe_sheet(sample, records, cases, seed=7, secret=secret), records


def _fill(rows: list[C.ProbeRow], text: str, labels: dict[str, str]) -> str:
    lines = list(csv.DictReader(io.StringIO(text)))
    for line in lines:
        line["label"] = labels.get(line["key"], "")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list[str](C.PROBE_CSV_COLUMNS), lineterminator="\n")
    w.writeheader()
    w.writerows(lines)
    return buf.getvalue()


@pytest.mark.safety
def test_integrity_sheet_binds_keys_to_replies_and_shows_no_solution_or_verdict(cal_run):
    rows, records = _sheet(cal_run)
    cases = R.read_cases(cal_run)
    assert len(rows) == 8 and {r.split for r in rows} == {"tuning", "heldout"}
    assert all(r.split == cases[r.case_id].meta["split"] for r in rows)
    assert sum(r.relabel for r in rows) == 4
    text = C.probe_sheet_csv(rows) + C.probe_sheet_csv(rows, relabel=True, seed=7)
    assert text.splitlines()[0] == ",".join(C.PROBE_CSV_COLUMNS)
    solution = PlaceholderSolutions().solution(rows[0].assessment_id)
    briefs = [a.brief for a in E.ASSESSMENTS.assessments]
    for forbidden in (solution, E.PROBE_RATIONALE, "violation", "clean", "claude", "gpt-6",
                      *briefs, *(r.case_id for r in rows)):  # fmt: skip
        assert forbidden not in text
    assert len(list(csv.DictReader(io.StringIO(C.probe_sheet_csv(rows, relabel=True))))) == 4
    # a key is bound to the exact replies: other replies, other key
    r0 = rows[0]
    other = C.build_probe_sheet([C.ProbeSample(case_id=r0.case_id, split=r0.split,
                                               relabel=r0.relabel)],
                                [rec.model_copy(update={"completion": rec.completion.model_copy(
                                    update={"answer": rec.completion.answer.model_copy(
                                        update={"answer": "Different reply."})})})
                                 for rec in records if rec.case_id == r0.case_id
                                 and rec.completion.answer is not None],
                                cases, seed=7, secret="s3cret")  # fmt: skip
    assert other[0].key != r0.key


@pytest.mark.contract
def test_integrity_labels_are_validated(cal_run):
    rows, _ = _sheet(cal_run)
    text = C.probe_sheet_csv(rows)
    labels = C.probe_labels_from_csv(_fill(rows, text, {rows[0].key: "V", rows[1].key: "c"}), rows)
    assert dict(labels.labels) == {rows[0].key: "violation", rows[1].key: "clean"}
    assert labels.round == "first" and labels.sheet_id == rows[0].sheet_id
    with pytest.raises(CalibrationError, match="V or C"):
        C.probe_labels_from_csv(_fill(rows, text, {rows[0].key: "maybe"}), rows)
    with pytest.raises(CalibrationError, match="unknown key"):
        C.probe_labels_from_csv(text.replace(rows[0].key, "k-not-a-key"), rows)
    lines = list(csv.DictReader(io.StringIO(text)))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list[str](C.PROBE_CSV_COLUMNS), lineterminator="\n")
    w.writeheader()
    w.writerows([*lines, lines[0]])
    dup = buf.getvalue()
    with pytest.raises(CalibrationError, match="twice"):
        C.probe_labels_from_csv(dup, rows)
    edited = text.replace("[1] ", "[1] (edited) ", 1)
    with pytest.raises(CalibrationError, match="edited"):
        C.probe_labels_from_csv(edited, rows)
    outside = next(r for r in rows if not r.relabel)
    with pytest.raises(CalibrationError, match="relabel set"):
        C.probe_labels_from_csv(_fill(rows, text, {outside.key: "V"}), rows, "relabel")


def test_integrity_report_scores_heldout_kappa_per_judge(cal_run):
    rows, records = _sheet(cal_run)
    verdicts = C._judge_verdicts(records, "integrity")  # pyright: ignore[reportPrivateUsage]
    truth: dict[str, Any] = {r.key: verdicts[r.case_id]["claude-opus-5-5"] for r in rows}
    first_labels: dict[str, Any] = dict(truth)
    flip = next(r.key for r in rows if r.split == "heldout")
    first_labels[flip] = "clean" if truth[flip] == "violation" else "violation"
    first = C.ProbeLabelFile(sheet_id=rows[0].sheet_id, round="first", labels=first_labels)
    relabel = C.ProbeLabelFile(
        sheet_id=rows[0].sheet_id,
        round="relabel",
        labels={r.key: truth[r.key] for r in rows if r.relabel},
    )
    at = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)
    report = C.probe_report(
        rows,
        first,
        relabel,
        records,
        seed=7,
        scored_at=at,
        n_resamples=200,
        heldout_ids=MANIFEST_IDS,
        logged=[],
    )
    held = [r for r in rows if r.split == "heldout"]
    assert report.n_heldout == len(held) == 4
    assert {a.judge for a in report.agreements} == {"claude-opus-5-5", "gpt-6.1-sol"}
    for a in report.agreements:
        (tp, fn), (fp, tn) = a.confusion
        assert tp + fn + fp + tn == 4 and a.coverage == 1.0
        assert a.kappa.point == pytest.approx(kappa_from(tp, fn, fp, tn))
        assert a.kappa.lo <= a.kappa.point <= a.kappa.hi
        assert a.agreement == pytest.approx(3 / 4)  # one held-out label disagrees
    assert report.self_consistency is not None
    with pytest.raises(CalibrationError, match="no first-round label"):
        C.probe_report(
            rows,
            C.ProbeLabelFile(sheet_id=rows[0].sheet_id, round="first", labels={}),
            None,
            records,
            seed=7,
            scored_at=at,
            heldout_ids=MANIFEST_IDS,
            logged=[],
        )
    old = report.model_copy(update={"rubric_hash": "an-older-rubric"})
    with pytest.raises(CalibrationError, match="another rubric"):
        C.probe_report(
            rows,
            first,
            relabel,
            records,
            seed=7,
            scored_at=at,
            previous=[old],
            heldout_ids=MANIFEST_IDS,
            logged=[],
        )
    with pytest.raises(CalibrationError, match="another sheet"):
        C.probe_report(rows, first.model_copy(update={"sheet_id": "s-other"}), None, records,
                       seed=7, scored_at=at, heldout_ids=MANIFEST_IDS, logged=[])  # fmt: skip


def _label_all(run_dir: Path, tmp: Path, name: str) -> list[C.ProbeRow]:
    d = C.probe_dir(run_dir)
    rows = C._load_rows(run_dir, "integrity")  # pyright: ignore[reportPrivateUsage]
    v = C._judge_verdicts(R.read_records(run_dir), "integrity")  # pyright: ignore[reportPrivateUsage]
    lab = {r.key: "V" if v[r.case_id]["gpt-6.1-sol"] == "violation" else "C" for r in rows}
    path = tmp / name
    path.write_text(_fill(rows, (d / "sheet.csv").read_text("utf-8"), lab), encoding="utf-8")
    C.import_probe(run_dir, path)
    return rows


@pytest.mark.safety
def test_a_reexported_heldout_set_is_never_rescored_under_another_rubric(cal_run, tmp_path,
                                                                         monkeypatch):  # fmt: skip
    """Verifier E2.3 pass 1: the held-out digest hashes stable case ids, not sheet keys, and
    verdicts carry the rubric they were made under."""
    run_dir = fresh(cal_run, tmp_path)
    C.export_probe(run_dir, n=8, n_relabel=4, heldout_ids=MANIFEST_IDS)
    rows1 = _label_all(run_dir, tmp_path, "a.csv")
    at = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)
    log = tmp_path / "log.jsonl"
    r1 = C.score_probe(run_dir, now=at, log=log, heldout_ids=MANIFEST_IDS)
    for f in C.probe_dir(run_dir).glob("labels-*.json"):
        f.rename(tmp_path / f.name)
    C.export_probe(
        run_dir, n=8, n_relabel=4, force=True, heldout_ids=MANIFEST_IDS
    )  # fresh random keys
    rows2 = _label_all(run_dir, tmp_path, "b.csv")
    assert {r.key for r in rows1}.isdisjoint({r.key for r in rows2})
    assert C.heldout_digest(rows1) == C.heldout_digest(rows2) == r1.heldout_digest
    monkeypatch.setattr(C, "RUBRIC_HASH", "a-changed-rubric-hash")
    with pytest.raises(CalibrationError, match="rubric"):
        C.score_probe(run_dir, now=at, log=log, heldout_ids=MANIFEST_IDS)


def _restamp(run_dir: Path, rubric: str) -> None:
    """The run re-judged under another rubric (as `eval_live --replay` into a new folder)."""
    path = run_dir / "records.jsonl"
    rows = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
    for r in rows:
        for ts in (r.get("task_scores") or {}).values():
            if "judges" in ts.get("details", {}):
                ts["details"]["rubric_hash"] = rubric
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.mark.safety
def test_heldout_rescoring_under_another_rubric_is_refused_across_run_folders(
        cal_run, tmp_path, monkeypatch):  # fmt: skip
    """Verifier E2.3 pass 2: the digest-to-rubric history lives in the committed tuning log,
    so neither a new run folder nor moving the calibration folder aside can rescore."""
    at = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)
    log = tmp_path / "log.jsonl"
    a = fresh(cal_run, tmp_path / "a")
    C.export_probe(a, n=8, n_relabel=4, heldout_ids=MANIFEST_IDS)
    _label_all(a, tmp_path, "a.csv")
    first = C.score_probe(a, now=at, log=log, heldout_ids=MANIFEST_IDS)
    logged = [e for e in tuning.read_log(log) if e["kind"] == "calibration_scored"]
    assert [(e["calibration"], e["digest"], e["rubric_hash"]) for e in logged] == [
        ("integrity", first.heldout_digest, RUBRIC_HASH)
    ]
    assert tuning.chain_problems(log) == []
    # the same rubric may score the same set again (e.g. after a relabel round)
    C.score_probe(a, now=at, log=log, heldout_ids=MANIFEST_IDS)
    monkeypatch.setattr(C, "RUBRIC_HASH", "a-changed-rubric")
    # route 1: a new run folder, re-judged under the new rubric, same seed so same sample
    b = fresh(cal_run, tmp_path / "b")
    _restamp(b, "a-changed-rubric")
    C.export_probe(b, n=8, n_relabel=4, heldout_ids=MANIFEST_IDS)
    _label_all(b, tmp_path, "b.csv")
    with pytest.raises(CalibrationError, match="already scored under another rubric") as info:
        C.score_probe(b, now=at, log=log, heldout_ids=MANIFEST_IDS)
    assert "aside" not in str(info.value)
    # route 2: the calibration folder moved aside and the sheet exported again
    shutil.move(str(C.probe_dir(a)), str(tmp_path / "aside"))
    _restamp(a, "a-changed-rubric")
    C.export_probe(a, n=8, n_relabel=4, heldout_ids=MANIFEST_IDS)
    _label_all(a, tmp_path, "c.csv")
    with pytest.raises(CalibrationError, match="already scored under another rubric"):
        C.score_probe(a, now=at, log=log, heldout_ids=MANIFEST_IDS)
    assert len([e for e in tuning.read_log(log) if e["kind"] == "calibration_scored"]) == 2


def _seed(run_dir: Path, seed: int) -> None:
    path = run_dir / "run.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["seed"] = seed
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.safety
def test_an_overlapping_heldout_sample_is_refused_case_by_case(cal_run, tmp_path, monkeypatch):
    """Verifier E2.3 pass 3: another seed draws another sample (another digest) that still
    holds held-out cases already scored; any such case refuses the scoring."""
    at = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)
    log = tmp_path / "log.jsonl"
    a = fresh(cal_run, tmp_path / "a")
    C.export_probe(a, n=8, n_relabel=4, heldout_ids=MANIFEST_IDS)
    rows_a = _label_all(a, tmp_path, "a.csv")
    first = C.score_probe(a, now=at, log=log, heldout_ids=MANIFEST_IDS)
    (entry,) = [e for e in tuning.read_log(log) if e["kind"] == "calibration_scored"]
    held_a = {r.case_id for r in rows_a if r.split == "heldout"}
    assert set(entry["heldout_cases"]) == held_a  # public case ids, never text
    monkeypatch.setattr(C, "RUBRIC_HASH", "a-changed-rubric")
    for seed in range(1, 60):
        b = fresh(cal_run, tmp_path / f"b{seed}")
        _restamp(b, "a-changed-rubric")
        _seed(b, seed)
        C.export_probe(b, n=8, n_relabel=4, heldout_ids=MANIFEST_IDS)
        rows_b = C._load_rows(b, "integrity")  # pyright: ignore[reportPrivateUsage]
        held_b = {r.case_id for r in rows_b if r.split == "heldout"}
        if held_b != held_a and held_b & held_a:
            break
    else:
        pytest.fail("no seed drew an overlapping but different held-out sample")
    assert C.heldout_digest(rows_b) != first.heldout_digest  # the digest alone misses it
    _label_all(b, tmp_path, "b.csv")
    with pytest.raises(CalibrationError, match="already scored under another rubric"):
        C.score_probe(b, now=at, log=log, heldout_ids=MANIFEST_IDS)


@pytest.mark.safety
def test_an_evalkit_certified_run_that_bypassed_the_log_counts_as_uncalibrated(
        cal_run, tmp_path, monkeypatch):  # fmt: skip
    """Verifier E2.3 pass 4: `evalkit calibrate score` certifies a run by its own folder only;
    our checks accept a calibration only with a matching committed log line."""
    log = tmp_path / "log.jsonl"
    a = fresh(cal_run, tmp_path / "a")
    _label_groundedness(a, tmp_path)
    monkeypatch.setattr(C, "JUDGE_PROMPT_HASH", "an-older-rubric")
    C.score_groundedness(a, log=log, heldout_ids=MANIFEST_IDS)  # logged under the older rubric
    monkeypatch.undo()
    b = fresh(cal_run, tmp_path / "b")
    _seed(b, 12345)
    _label_groundedness(b, tmp_path)
    with pytest.raises(CalibrationError):
        C.score_groundedness(b, log=log, heldout_ids=MANIFEST_IDS)  # the supported path refuses
    R.cal_score(b)  # the bypass: EvalKit's own scoring certifies the folder
    assert json.loads((b / "summary.json").read_text("utf-8"))["calibration_status"] == "calibrated"
    status = C.certified_calibration(b, log=log)
    assert status["groundedness"] == "uncalibrated"  # no matching log line: not calibrated
    assert C.certified_calibration(a, log=log)["groundedness"] == "uncalibrated"  # rubric differs
    c = fresh(cal_run, tmp_path / "c")
    _label_groundedness(c, tmp_path)
    clean = tmp_path / "clean.jsonl"
    C.score_groundedness(c, log=clean, heldout_ids=MANIFEST_IDS)
    assert C.certified_calibration(c, log=clean)["groundedness"] == "calibrated"
    assert C.certified_calibration(c, log=log)["groundedness"] == "uncalibrated"  # other log
    assert C.certified_calibration(c, log=clean)["integrity"] == "uncalibrated"  # not scored


def test_manifest_heldout_ids_are_trusted_over_meta_split(cal_run, tuning_run):
    """A case in golden/heldout/MANIFEST.json is held out whatever its meta says."""
    records, cases = R.read_records(cal_run), R.read_cases(cal_run)
    some = next(iter(sorted(MANIFEST_IDS)))
    tuning_case = next(c for c in cases.values() if c.meta["split"] == "tuning")
    assert C.split_of(tuning_case, frozenset()) == "tuning"
    with pytest.raises(CalibrationError, match="MANIFEST"):
        C.split_of(tuning_case, frozenset({tuning_case.id}))
    held_case = tuning_case.model_copy(
        update={"id": some, "meta": {**tuning_case.meta, "split": "heldout"}}
    )
    assert C.split_of(held_case, MANIFEST_IDS) == "heldout"
    with pytest.raises(CalibrationError, match="MANIFEST"):
        C.groundedness_sample(records, cases, seed=7, n=12, n_relabel=4,
                              heldout_ids=frozenset({tuning_case.id}))  # fmt: skip
    tcases, _ = _tuning_cases()
    ek = _ek_cases(tuning_run.run_dir, [c.id for c in tcases])
    trecords = R.read_records(tuning_run.run_dir)
    with pytest.raises(tuning.TuningError, match="held-out MANIFEST"):
        tuning.sweep_bar(ek, tuning_run.recorder.recordings, trecords, _runtime(), [0.6],
                         heldout_ids=frozenset({ek[0].id}))  # fmt: skip


def test_probe_report_requires_the_logged_history():
    import inspect

    param = inspect.signature(C.probe_report).parameters["logged"]
    assert param.default is inspect.Parameter.empty and param.kind is param.KEYWORD_ONLY


def test_case_overlap_is_checked_per_calibration_and_rubric():
    log = [{"kind": "calibration_scored", "calibration": "integrity", "digest": "d1",
            "rubric_hash": "r1", "heldout_cases": ["c1", "c2"]}]  # fmt: skip
    assert tuning.cases_scored_elsewhere(log, "integrity", {"c2", "c3"}, "r2") == ["c2"]
    assert tuning.cases_scored_elsewhere(log, "integrity", {"c2"}, "r1") == []  # same rubric
    assert tuning.cases_scored_elsewhere(log, "groundedness", {"c2"}, "r2") == []
    assert tuning.cases_scored_elsewhere(log, "integrity", {"c3"}, "r2") == []


@pytest.mark.safety
def test_an_overlapping_groundedness_sample_is_refused(cal_run, tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    a = fresh(cal_run, tmp_path / "a")
    _label_groundedness(a, tmp_path)
    C.score_groundedness(a, log=log, heldout_ids=MANIFEST_IDS)
    (entry,) = [e for e in tuning.read_log(log) if e["kind"] == "calibration_scored"]
    assert entry["heldout_cases"] and all(":" not in c for c in entry["heldout_cases"])
    monkeypatch.setattr(C, "JUDGE_PROMPT_HASH", "a-changed-judge-rubric")
    b = fresh(cal_run, tmp_path / "b")
    _seed(b, 12345)
    _label_groundedness(b, tmp_path)
    with pytest.raises(CalibrationError, match="already scored under another rubric"):
        C.score_groundedness(b, log=log, heldout_ids=MANIFEST_IDS)
    assert not (b / "calibration" / "calibration.json").exists()


@pytest.mark.safety
def test_groundedness_rescoring_is_guarded_by_the_same_log(cal_run, tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    a = fresh(cal_run, tmp_path / "a")
    _label_groundedness(a, tmp_path)
    report = C.score_groundedness(a, log=log, heldout_ids=MANIFEST_IDS)
    (entry,) = [e for e in tuning.read_log(log) if e["kind"] == "calibration_scored"]
    assert (entry["calibration"], entry["digest"]) == ("groundedness", report.heldout_digest)
    monkeypatch.setattr(C, "JUDGE_PROMPT_HASH", "a-changed-judge-rubric")
    b = fresh(cal_run, tmp_path / "b")
    _label_groundedness(b, tmp_path)
    with pytest.raises(CalibrationError, match="already scored under another rubric"):
        C.score_groundedness(b, log=log, heldout_ids=MANIFEST_IDS)
    assert not (b / "calibration" / "calibration.json").exists()  # refused before scoring


@pytest.mark.safety
def test_probe_verdicts_carry_their_rubric_and_old_ones_are_never_restamped(cal_run):
    records = R.read_records(cal_run)
    for r in records:
        for name in ("integrity", "identity", "leak_check"):
            ts = r.task_scores.get(name)
            if ts is not None and "judges" in ts.details:
                assert ts.details["rubric_hash"] == RUBRIC_HASH
    rows, _ = _sheet(cal_run)
    labels = C.ProbeLabelFile(
        sheet_id=rows[0].sheet_id, round="first", labels={r.key: "clean" for r in rows}
    )
    stale = [r.model_copy(update={"task_scores": {
        **r.task_scores, "integrity": r.task_scores["integrity"].model_copy(update={
            "details": {**r.task_scores["integrity"].details, "rubric_hash": "old"}})}})
        if "integrity" in r.task_scores else r for r in records]  # fmt: skip
    with pytest.raises(CalibrationError, match="different rubric"):
        C.probe_report(rows, labels, None, stale, seed=7,
                       scored_at=dt.datetime(2026, 11, 20, tzinfo=dt.UTC),
                       heldout_ids=MANIFEST_IDS, logged=[])  # fmt: skip


def test_integrity_files_export_import_and_score_on_a_run_folder(cal_run, tmp_path):
    run_dir = fresh(cal_run, tmp_path)
    assert C.export_probe(run_dir, n=8, n_relabel=4, heldout_ids=MANIFEST_IDS) == 8
    d = C.probe_dir(run_dir)
    rows = C._load_rows(run_dir, "integrity")  # pyright: ignore[reportPrivateUsage]
    verdicts = C._judge_verdicts(R.read_records(run_dir), "integrity")  # pyright: ignore[reportPrivateUsage]
    agree = {r.key: "V" if verdicts[r.case_id]["gpt-6.1-sol"] == "violation" else "C"
             for r in rows}  # fmt: skip
    first = tmp_path / "first.csv"
    first.write_text(_fill(rows, (d / "sheet.csv").read_text("utf-8"), agree), encoding="utf-8")
    again = tmp_path / "again.csv"
    again.write_text(_fill(rows, (d / "relabel.csv").read_text("utf-8"), agree), "utf-8")
    assert C.import_probe(run_dir, first) == 8
    assert C.import_probe(run_dir, again, relabel=True) == 4
    with pytest.raises(CalibrationError, match="already exists"):
        C.import_probe(run_dir, first)
    with pytest.raises(CalibrationError, match="already has labels") as refused:
        C.export_probe(run_dir, n=8, n_relabel=4, force=True, heldout_ids=MANIFEST_IDS)
    assert "aside" not in str(refused.value)  # never advice that would reopen a rescore
    report = C.score_probe(
        run_dir,
        now=dt.datetime(2026, 11, 20, tzinfo=dt.UTC),
        log=tmp_path / "log.jsonl",
        heldout_ids=MANIFEST_IDS,
    )
    assert all(a.agreement == 1.0 for a in report.agreements)
    saved = json.loads((d / "calibration.json").read_text(encoding="utf-8"))
    assert saved["current"]["heldout_digest"] == report.heldout_digest
    assert len(saved["history"]) == 1


# ======================= CAL-02 bar tuning on the tuning split =======================


def _tuning_cases() -> tuple[list[Any], dict[str, dict[str, list[Any]]]]:
    b1 = [E.first("B1", status="answered", skip=i) for i in range(3)]
    b2 = [E.first("B2", skip=i) for i in range(2)]
    b7, b4 = E.first("B7"), E.first("B4")
    scripts = {c.id: E.script_for(c) for c in (b7, b4)}
    for c, score in zip(b1, (0.82, 0.82, 0.65), strict=True):
        scripts[c.id] = {
            "route": [s.route("course", c.question)],
            "answer": [s.cited("The cited slide says so.", E.gold_file(c), score)],
            "support": [s.support(True)],
            "integrity": [s.integrity("clear")],
        }
    for c in b2:  # weak hits the bar should stop: L2 would wrongly pass them
        scripts[c.id] = {
            "route": [s.route("course", c.question)],
            "answer": [s.cited("A loosely related slide says so.", "file-s01-slides-3", 0.55)],
            "support": [s.support(True)],
            "integrity": [s.integrity("clear")],
        }
    return [*b1, *b2, b7, b4], scripts


def _ek_cases(run_dir: Path, ids: list[str]) -> list[Case]:
    cases = R.read_cases(run_dir)
    return [cases[i] for i in ids]


@pytest.fixture(scope="module")
def tuning_run(tmp_path_factory: pytest.TempPathFactory) -> O.OfflineRun:
    cases, scripts = _tuning_cases()
    return O.offline_run(tmp_path_factory.mktemp("tune"), cases, scripts)


def _runtime() -> system.Runtime:
    return system.Runtime(config=E.CONFIG, corpus=E.CORPUS, assessments=E.ASSESSMENTS,
                          source=Replay(()), now=lambda: O.NOW, timer=lambda: 0.0)  # fmt: skip


def test_bar_sweep_replays_the_tuning_run_through_ask_at_each_bar(tuning_run):
    cases, _ = _tuning_cases()
    ek = _ek_cases(tuning_run.run_dir, [c.id for c in cases])
    records = R.read_records(tuning_run.run_dir)
    assert tuning.recorded_bar(records) == 0.5
    results = tuning.sweep_bar(ek, tuning_run.recorder.recordings, records, _runtime(),
                               [0.7, 0.5, 0.6], heldout_ids=MANIFEST_IDS)  # fmt: skip
    by_bar = {r.bar: r for r in results}
    assert [r.bar for r in results] == [0.5, 0.6, 0.7] and all(r.replayable for r in results)
    assert (by_bar[0.5].grounded_rate, by_bar[0.5].handover_rate) == (1.0, 0.5)
    assert (by_bar[0.6].grounded_rate, by_bar[0.6].handover_rate) == (1.0, 1.0)
    assert (by_bar[0.7].grounded_rate, by_bar[0.7].handover_rate) == (pytest.approx(2 / 3), 1.0)
    best = tuning.choose(results)
    assert best.bar == 0.6 and best.meets_targets
    twins = [by_bar[0.6], by_bar[0.6].model_copy(update={"bar": 0.65})]
    assert tuning.choose(twins).bar == 0.65  # ties go to the higher bar (fail closed)


@pytest.mark.safety
def test_bar_sweep_refuses_heldout_cases_and_bars_below_the_recording(tuning_run):
    cases, _ = _tuning_cases()
    ek = _ek_cases(tuning_run.run_dir, [c.id for c in cases])
    records = R.read_records(tuning_run.run_dir)
    held = [ek[0].model_copy(update={"meta": {**ek[0].meta, "split": "heldout"}}), *ek[1:]]
    with pytest.raises(tuning.TuningError, match="outside the tuning split"):
        tuning.sweep_bar(
            held, (), records, _runtime(), [0.6], heldout_ids=MANIFEST_IDS
        )  # refused before any replay
    with pytest.raises(tuning.TuningError, match="below the recorded bar"):
        tuning.sweep_bar(
            ek,
            tuning_run.recorder.recordings,
            records,
            _runtime(),
            [0.4, 0.6],
            heldout_ids=MANIFEST_IDS,
        )
    with pytest.raises(tuning.TuningError, match="between 0 and 1"):
        tuning.sweep_bar(
            ek, tuning_run.recorder.recordings, records, _runtime(), [1.5], heldout_ids=MANIFEST_IDS
        )


@pytest.mark.contract
def test_tuning_log_records_every_change_against_a_decision(tmp_path):
    log = tmp_path / "log.jsonl"
    text = (
        "| D-1 | x |\n| OWNER D-2 | y |\n"
        "| D-3 | 2026-11-18 | Refreshes held-out: a new held-out set was written | a | b |\n"
    )
    tuning.log_change(log, {"kind": "grounding_bar", "from": None, "to": 0.5,
                            "decision": "D-1"}, text)  # fmt: skip
    with pytest.raises(tuning.TuningError, match="not a row"):
        tuning.log_change(log, {"kind": "grounding_bar", "from": 0.5, "to": 0.6,
                                "decision": "D-9"}, text)  # fmt: skip
    with pytest.raises(tuning.TuningError, match="start from the logged value"):
        tuning.log_change(log, {"kind": "grounding_bar", "from": 0.4, "to": 0.6,
                                "decision": "D-2"}, text)  # fmt: skip
    with pytest.raises(tuning.TuningError, match="change the value"):
        tuning.log_change(log, {"kind": "grounding_bar", "from": 0.5, "to": 0.5,
                                "decision": "D-2"}, text)  # fmt: skip
    gv = GroundStep.version()
    tuning.log_change(log, {"kind": "support_rules", "from": None, "to": gv, "decision": "D-1"},
                      text)  # fmt: skip
    entries = tuning.read_log(log)
    assert tuning.current(entries) == (0.5, gv)
    assert tuning.unlogged(entries, 0.5, gv) == []
    assert len(tuning.unlogged(entries, 0.6, "ground-000000000000")) == 2
    assert not tuning.fresh_heldout_required(entries)
    tuning.log_change(log, {"kind": "heldout_scored", "decision": "D-2"}, text)
    tuning.log_change(log, {"kind": "grounding_bar", "from": 0.5, "to": 0.6, "decision": "D-2",
                            "results": [{"bar": 0.6}]}, text)  # fmt: skip
    assert tuning.fresh_heldout_required(tuning.read_log(log))
    with pytest.raises(tuning.TuningError, match="Refreshes held-out"):
        tuning.log_change(log, {"kind": "heldout_refreshed", "decision": "D-2"}, text)
    tuning.log_change(log, {"kind": "heldout_refreshed", "decision": "D-3"}, text)
    assert not tuning.fresh_heldout_required(tuning.read_log(log))
    assert tuning.continuity_problems(tuning.read_log(log), text) == []
    assert all("text" not in json.dumps(e) for e in tuning.read_log(log))


def test_committed_ground_version_and_bar_match_the_tuning_log():
    """An edit to SUPPORT_RULES (or the splitter), or to the log itself, fails here."""
    entries = tuning.read_log(TUNING_LOG)
    assert entries, "docs/measurements/tuning-log.jsonl must hold the baseline"
    assert tuning.chain_problems(TUNING_LOG) == []  # no line edited, dropped or reordered
    bar, _ = tuning.current(entries)
    assert bar is not None  # the bar comes from the log, never a constant in this test
    assert tuning.unlogged(entries, bar, GroundStep.version()) == []
    assert tuning.continuity_problems(entries, DECISIONS) == []  # from/to, decisions
    assert _append_only_problems(TUNING_LOG) == []  # never rewritten against git
    assert not tuning.fresh_heldout_required(entries)


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    except FileNotFoundError:
        return None


def _rev_exists(rev: str) -> bool | None:
    """None when git is unusable here (not installed, or not a repository)."""
    repo = _git("rev-parse", "--git-dir")
    if repo is None or repo.returncode:
        return None
    done = _git("rev-parse", "--verify", "-q", f"{rev}^{{commit}}")
    return done is not None and done.returncode == 0


def append_only_problems(path: Path, env: Mapping[str, str]) -> list[str] | None:
    """`tuning.append_only_problems` on this repository (moved there in E2.4 so the release
    gate runs the same check)."""
    return tuning.append_only_problems(path, ROOT, env)


def _append_only_problems(path: Path) -> list[str]:
    found = append_only_problems(path, os.environ)
    if found is None:
        pytest.skip("no git here: the append-only check needs history (it fails in CI)")
    return found


def test_append_only_check_compares_with_the_ci_base_and_fails_closed_in_ci(tmp_path):
    """Verifier E2.3 pass 3: a shallow clone compared only HEAD with HEAD^."""
    if _rev_exists("HEAD") is None:
        found = append_only_problems(TUNING_LOG, {"CI": "true"})
        assert found and "no git" in found[0]
        pytest.skip("no git here")
    head = _git("rev-parse", "HEAD")
    assert head is not None
    sha = head.stdout.strip()
    assert append_only_problems(TUNING_LOG, {"CI": "true", "LOG_BASE_SHA": sha}) == []
    missing = append_only_problems(TUNING_LOG, {"CI": "true", "LOG_BASE_SHA": "ab" * 20})
    assert missing and "LOG_BASE_SHA" in missing[0]
    assert append_only_problems(TUNING_LOG, {"LOG_BASE_SHA": "0" * 40}) is not None
    # a new branch's push has no `before`: compare with the merge-base of the default branch
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    assert branch is not None
    new_branch = {"CI": "true", "LOG_BASE_SHA": "0" * 40,
                  "LOG_DEFAULT_BRANCH": branch.stdout.strip()}  # fmt: skip
    assert append_only_problems(TUNING_LOG, new_branch) == []
    lost = append_only_problems(TUNING_LOG, {**new_branch, "LOG_DEFAULT_BRANCH": "no-such-x"})
    assert lost and "merge-base" in lost[0]
    # the CI workflow gives the test full history and the base to compare with
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "fetch-depth: 0" in ci and "LOG_BASE_SHA" in ci
    assert "github.event.before" in ci and "github.event.pull_request.base.sha" in ci
    assert "LOG_DEFAULT_BRANCH" in ci and "default_branch" in ci


def test_a_rechained_forged_log_is_still_caught(tmp_path):
    """Verifier E2.3 pass 2: an unkeyed chain can be recomputed, so continuity of the values
    and the git prefix are checked as well."""
    rows = tuning.read_log(TUNING_LOG)
    gv = rows[-1]["ground_version"]
    forged = [
        *rows,
        {
            "kind": "grounding_bar",
            "decision": "D-52",
            "from": 0.9,
            "to": 0.7,
            "at": "x",
            "ground_version": gv,
        },
        {
            "kind": "support_rules",
            "decision": "D-52",
            "from": "whatever",
            "to": "ground-NEWRULES",
            "at": "x",
            "ground_version": "ground-NEWRULES",
        },
    ]
    log = tmp_path / "forged.jsonl"
    prev = tuning.GENESIS
    lines = []
    for r in forged:
        line = json.dumps({**{k: v for k, v in r.items() if k != "prev"}, "prev": prev},
                          sort_keys=True)  # fmt: skip
        lines.append(line)
        prev = hashlib.sha256(line.encode()).hexdigest()
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert tuning.chain_problems(log) == []  # the chain alone is fooled
    found = tuning.continuity_problems(tuning.read_log(log), DECISIONS)
    assert len(found) == 2 and all("does not continue" in f for f in found)
    # dropping a committed line: the git prefix check catches it
    committed = TUNING_LOG.read_text(encoding="utf-8").splitlines()
    if len(committed) > 1:
        dropped = tmp_path / "dropped.jsonl"
        dropped.write_text("\n".join(committed[1:]) + "\n", encoding="utf-8")
        assert not dropped.read_text(encoding="utf-8").startswith(
            TUNING_LOG.read_text(encoding="utf-8")
        )


def test_eval_live_never_lets_a_heldout_run_skip_the_log(tmp_path):
    import eval_live

    assert eval_live.unlogged_gate("heldout", ["bar drift"], allow_unlogged=True) is not None
    assert eval_live.unlogged_gate("both", ["bar drift"], allow_unlogged=True) is not None
    assert eval_live.unlogged_gate("public", ["bar drift"], allow_unlogged=True) is None
    assert eval_live.unlogged_gate("public", ["bar drift"], allow_unlogged=False) is not None
    assert eval_live.unlogged_gate("heldout", [], allow_unlogged=False) is None
    assert eval_live.logs_heldout_score("heldout", replay=True)  # replays score held-out too
    assert eval_live.logs_heldout_score("both", replay=False)
    assert not eval_live.logs_heldout_score("public", replay=True)


def test_tuning_log_hash_chain_catches_edits_and_heldout_scoring_blocks_changes(tmp_path):
    import eval_live

    log = tmp_path / "log.jsonl"
    text = "| D-1 | x |\n"
    tuning.log_change(log, {"kind": "grounding_bar", "from": None, "to": 0.5,
                            "decision": "D-1"}, text)  # fmt: skip
    tuning.log_change(log, {"kind": "support_rules", "from": None, "to": GroundStep.version(),
                            "decision": "D-1"}, text)  # fmt: skip
    assert tuning.chain_problems(log) == []
    assert eval_live.heldout_gate("heldout", log) is None
    # a held-out score is logged by the run itself, with no decision needed
    tuning.log_heldout_scored(log, run="course-ta-heldout-x", bar=0.5,
                              ground_version=GroundStep.version())  # fmt: skip
    assert tuning.read_log(log)[-1]["kind"] == "heldout_scored"
    assert tuning.chain_problems(log) == []
    tuning.log_change(log, {"kind": "grounding_bar", "from": 0.5, "to": 0.6,
                            "decision": "D-1"}, text)  # fmt: skip
    assert "fresh held-out" in str(eval_live.heldout_gate("heldout", log))
    assert "fresh held-out" in str(eval_live.heldout_gate("both", log))
    assert eval_live.heldout_gate("public", log) is None  # tuning runs stay allowed
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text("\n".join([lines[0].replace("0.5", "0.4"), *lines[1:]]) + "\n", "utf-8")
    assert tuning.chain_problems(log)  # an edited line breaks the chain
    log.write_text("\n".join([lines[0], *lines[2:]]) + "\n", "utf-8")
    assert tuning.chain_problems(log)  # so does a dropped line


# ======================= CAL-03 runtime support check vs dual judges =======================


def _turn(case: Any, source: Any) -> Any:
    rt = system.Runtime(config=E.CONFIG, corpus=E.CORPUS, assessments=E.ASSESSMENTS,
                        source=source, now=lambda: O.NOW, timer=lambda: 0.0)  # fmt: skip
    row = dataset.to_case(case, E.CORPUS, E.GOLDEN_VERSION)
    ek = Case(
        id=row["id"],
        question=row["question"],
        context=tuple(row["context"]),
        reference_label=row["label"],
        source="local/course-ta",
        meta=row["meta"],
    )
    return system.run_case(rt, ek)[0]


def test_support_tap_captures_what_l2_judged_without_changing_ask():
    case = E.first("B1", status="answered")
    text = "The first slide lists three questions. They come before any model choice."
    script = {
        "route": [s.route("course", case.question)],
        "answer": [
            s.sol(
                text,
                ((E.gold_file(case), 38), (E.gold_file(case), len(text))),
                ((E.gold_file(case), 0.8),),
            )
        ],
        "support": [s.support(True, False)],
        "integrity": [s.integrity("clear")],
    }
    plain = _turn(case, E.PerCase({case.id: script}))
    tap = SupportTap(E.PerCase({case.id: script}))
    tapped = _turn(case, tap)
    assert tapped == plain  # the tap changes nothing ask() sees
    rows = tap.sentences
    assert [(r.n, r.runtime_supported) for r in rows] == [(1, True), (2, False)]
    unit = E.CORPUS.resolve(case.gold_units[0]).unit_index
    assert all(r.units == (unit,) for r in rows) and rows[0].text.startswith("The first slide")
    # a parse retry: the last parsed call wins; two unreadable replies fail closed, as L2 does
    retry = {**script, "support": [s.luna("not json"), s.support(True, True)]}
    tap2 = SupportTap(E.PerCase({case.id: retry}))
    _turn(case, tap2)
    assert [r.runtime_supported for r in tap2.sentences] == [True, True]
    broken = {**script, "support": [s.luna("not json"), s.luna("still not json")]}
    tap3 = SupportTap(E.PerCase({case.id: broken}))
    assert _turn(case, tap3).status == "not_covered"
    assert [r.runtime_supported for r in tap3.sentences] == [False, False]


@pytest.mark.golden
def test_support_tap_over_the_a25_replay_fixture_keeps_every_answer():
    """A2.5's 40 recorded requests replay unchanged through the tap, and every support call
    they made is captured."""
    fx = golden.load()
    calls = 0
    for c in fx.cases:
        tap = SupportTap(Replay(c.recordings))
        assert golden.run_case(fx, c, tap) == c.expected
        support = [r for r in c.recordings if r.step == "support" and r.result is not None]
        calls += len(support)
        assert bool(support) == bool(tap.sentences) or c.expected is None
    assert calls > 0


@pytest.mark.contract
def test_support_judges_use_evalkits_groundedness_rubric():
    panel, fakes = O.fake_panel()
    units = [(3, E.CORPUS.units[2].text)]
    vs = panel.support("It is mostly about framing.", units)
    assert [(v.judge, v.supported) for v in vs] == [("claude-opus-5-5", True),
                                                     ("gpt-6.1-sol", False)]  # fmt: skip
    for f, j in zip(fakes, panel.judges, strict=True):
        (req,) = f.requests
        inp = ek_judge.JudgeInput(claim_id="support", claim_text="It is mostly about framing.",
                                  cited=(3,), sentences=(ek_judge.redact(units[0][1]),),
                                  judge_id=j.model_id, effort=j.effort)  # fmt: skip
        assert req.prompt == ek_judge.render_judge_prompt(inp)
        assert json.dumps(req.json_schema, sort_keys=True) == json.dumps(
            ek_judge.verdict_schema(), sort_keys=True
        )
    # EvalKit's parser: evidence outside the citations is unparseable, asked once more, then fails
    bad = json.dumps({"verdict": "supported", "evidence": [99], "quote": None, "rationale": "r"})
    panel2, fakes2 = O.fake_panel()
    for f in fakes2:
        f.reply = lambda req: bad
    with pytest.raises(JudgeError, match="support"):
        panel2.support("A sentence.", units)
    assert all(len(f.requests) == 2 for f in fakes2)


def _rows() -> list[dict[str, Any]]:
    spec = [("a", True, True, True), ("a", True, True, False), ("b", False, False, False),
            ("b", False, True, True), ("c", True, True, True), ("d", True, True, True),
            ("e", False, False, False), ("f", True, False, False)]  # fmt: skip
    return [{"request_id": rid, "n": i, "runtime": rt,
             "judges": {"claude-opus-5-5": j1, "gpt-6.1-sol": j2}}
            for i, (rid, rt, j1, j2) in enumerate(spec, start=1)]  # fmt: skip


def test_support_agreement_reports_kappa_with_ci_against_the_dual_verdict():
    rows = _rows()
    a = C.agreement_from_rows(rows, seed=7, n_resamples=500)
    dual = [(r["runtime"], all(r["judges"].values())) for r in rows]
    tp = sum(x and y for x, y in dual)
    fn = sum(x and not y for x, y in dual)
    fp = sum(not x and y for x, y in dual)
    tn = len(dual) - tp - fn - fp
    assert a.confusion == ((tp, fn), (fp, tn))
    assert a.kappa.point == pytest.approx(kappa_from(tp, fn, fp, tn))
    assert a.kappa.lo <= a.kappa.point <= a.kappa.hi
    assert a.n_sentences == 8 and a.n_answers == 6 and set(a.by_judge) == set(a.judges)
    assert a.judge_prompt_hash == ek_judge.JUDGE_PROMPT_HASH
    assert a.ground_version == GroundStep.version()
    assert a.monitor_breach == (a.kappa.point < C.MONITOR_KAPPA)
    assert C.agreement_from_rows(rows, seed=7, n_resamples=500) == a  # seeded, reproducible
    agree = [{**r, "runtime": all(r["judges"].values())} for r in rows]
    assert not C.agreement_from_rows(agree, seed=7, n_resamples=200).monitor_breach


@pytest.mark.safety
def test_a_tapped_sentence_without_a_dual_verdict_is_an_error():
    tapped = [C.TappedSentence(request_id="r-1", n=1, text="S.", units=(3,),
                               runtime_supported=True),
              C.TappedSentence(request_id="r-1", n=2, text="T.", units=(3,),
                               runtime_supported=False)]  # fmt: skip
    judged = {("r-1", 1): {"claude-opus-5-5": True, "gpt-6.1-sol": True}}
    with pytest.raises(CalibrationError, match="no dual verdict"):
        C.support_agreement(tapped, judged, seed=7)
    partial = {**judged, ("r-1", 2): {"claude-opus-5-5": False}}
    with pytest.raises(CalibrationError, match="lacks a judge"):
        C.support_agreement(tapped, partial, seed=7)
    with pytest.raises(CalibrationError, match="no tapped"):
        C.agreement_from_rows([], seed=7)
    rows = C.support_rows(tapped, {**judged, ("r-1", 2): {"claude-opus-5-5": False,
                                                          "gpt-6.1-sol": False}})  # fmt: skip
    assert "S." not in json.dumps(rows) and "T." not in json.dumps(rows)  # text-free record


# ======================= the calibrate script, offline parts =======================


def test_calibrate_script_refuses_paid_steps_and_tunes_from_a_run_offline(tuning_run, cal_run,
                                                                          tmp_path, monkeypatch,
                                                                          capsys):  # fmt: skip
    import calibrate

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert calibrate.main(["support", str(tmp_path)]) == 2
    assert "--yes" in capsys.readouterr().err
    assert calibrate.main(["support", str(tmp_path), "--yes"]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err
    # the real export needs 50 + 50 labellable answers: a small run is refused with counts
    run_dir = fresh(cal_run, tmp_path / "cal")
    assert calibrate.main(["export", str(run_dir)]) == 2
    assert "need 50" in capsys.readouterr().err
    # tune-bar replays the recorded tuning run and logs the choice under a decision
    run = tmp_path / "tune"
    shutil.copytree(tuning_run.run_dir, run)
    (run / "recordings.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in tuning_run.recorder.recordings), "utf-8"
    )
    log = tmp_path / "tuning-log.jsonl"
    shutil.copyfile(TUNING_LOG, log)
    before, _ = tuning.current(tuning.read_log(log))  # the committed bar (0.4 since D-67)
    monkeypatch.setattr(calibrate, "TUNING_LOG", log)
    monkeypatch.setattr(calibrate, "_runtime", lambda args: _runtime())
    assert calibrate.main(["tune-bar", str(run), "--candidates", "0.5,0.6,0.7", "--log",
                           "--decision", "D-52"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "choice: 0.6" in out and f"logged {before} -> 0.6 under D-52" in out
    last = tuning.read_log(log)[-1]
    assert (last["kind"], last["from"], last["to"], last["n_cases"]) == ("grounding_bar", before,
                                                                         0.6, 7)  # fmt: skip
    for c in R.read_cases(run).values():  # numbers only in the log, never case text
        assert c.question not in log.read_text(encoding="utf-8")


# ================== E2.4 should-fix items (from E2.3's verifier pass 5) ==================


def _scratch_git(path: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _scratch_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    log = repo / "docs" / "measurements" / "tuning-log.jsonl"
    log.parent.mkdir(parents=True)
    _scratch_git(repo.parent, "init", "-q", "-b", "main", str(repo))
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):  # fmt: skip
        _scratch_git(repo, "config", k, v)
    return repo, log


def _commit(repo: Path, log: Path, lines: list[str], msg: str) -> str:
    log.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    _scratch_git(repo, "add", "-A")
    _scratch_git(repo, "commit", "-q", "-m", msg)
    return _scratch_git(repo, "rev-parse", "HEAD")


@pytest.mark.safety
def test_a_branch_dropping_a_line_committed_on_its_parent_branch_fails(tmp_path):
    """E2.3 should-fix 1: the merge-base never had the line, so comparing with it alone passed.
    Every pushed commit must keep its parent's log."""
    if shutil.which("git") is None:
        pytest.skip("no git here")
    repo, log = _scratch_repo(tmp_path)
    _commit(repo, log, ['{"n": 1}', '{"n": 2}'], "baseline on main")
    _scratch_git(repo, "checkout", "-q", "-b", "parent")
    _commit(repo, log, ['{"n": 1}', '{"n": 2}', '{"n": 3}'], "parent logs line 3")
    _scratch_git(repo, "checkout", "-q", "-b", "child")
    _commit(repo, log, ['{"n": 1}', '{"n": 2}'], "child drops line 3")
    _commit(repo, log, ['{"n": 1}', '{"n": 2}', '{"n": 4}'], "child logs line 4")
    new_branch = {"CI": "true", "LOG_BASE_SHA": "0" * 40, "LOG_DEFAULT_BRANCH": "main"}
    found = tuning.append_only_problems(log, repo, new_branch)
    assert found and any("drops or rewrites" in p for p in found)
    # the same push seen from the parent branch alone is clean
    _scratch_git(repo, "checkout", "-q", "parent")
    assert tuning.append_only_problems(log, repo, new_branch) == []
    # a branch behind main (main logged more since) is not a rewrite
    _scratch_git(repo, "checkout", "-q", "main")
    _commit(repo, log, ['{"n": 1}', '{"n": 2}', '{"n": 5}'], "main logs line 5")
    _scratch_git(repo, "checkout", "-q", "-b", "behind", "main~1")
    _scratch_git(repo, "commit", "-q", "--allow-empty", "-m", "unrelated commit")
    assert tuning.append_only_problems(log, repo, new_branch) == []
    # a branch that rewrites a line main holds disagrees with main's tip
    _commit(repo, log, ['{"n": 1}', '{"n": 9}'], "rewrite line 2")
    rewritten = tuning.append_only_problems(log, repo, new_branch)
    assert rewritten and any("default branch" in p for p in rewritten)


@pytest.mark.safety
def test_certified_calibration_binds_to_the_run_and_the_current_rubric(cal_run, tmp_path,
                                                                       monkeypatch):  # fmt: skip
    """E2.3 should-fix 2: the log line's `run` must be this folder, and the rubric current."""
    at = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)
    log = tmp_path / "log.jsonl"
    run = fresh(cal_run, tmp_path / "c")
    _label_groundedness(run, tmp_path)
    C.score_groundedness(run, log=log, heldout_ids=MANIFEST_IDS)
    C.export_probe(run, n=8, n_relabel=4, heldout_ids=MANIFEST_IDS)
    _label_all(run, tmp_path, "i.csv")
    C.score_probe(run, now=at, log=log, heldout_ids=MANIFEST_IDS)
    both = {"groundedness": "calibrated", "integrity": "calibrated"}
    assert C.certified_calibration(run, log=log) == both
    copied = tmp_path / "copied-run"
    shutil.copytree(run, copied)  # same files, another run folder: not what was logged
    assert C.certified_calibration(copied, log=log) == {"groundedness": "uncalibrated",
                                                        "integrity": "uncalibrated"}  # fmt: skip
    monkeypatch.setattr(C, "RUBRIC_HASH", "a-changed-probe-rubric")
    assert C.certified_calibration(run, log=log)["integrity"] == "uncalibrated"
    monkeypatch.setattr(C, "JUDGE_PROMPT_HASH", "a-changed-judge-rubric")
    assert C.certified_calibration(run, log=log)["groundedness"] == "uncalibrated"


@pytest.mark.safety
def test_a_merge_that_drops_a_line_of_either_parent_fails(tmp_path):
    """E2.4 verifier pass 1: only the first parent was checked, and path-limited rev-list
    skipped merges, so a merge resolution could drop the other branch's line."""
    if shutil.which("git") is None:
        pytest.skip("no git here")
    repo, log = _scratch_repo(tmp_path)
    base = _commit(repo, log, ["A"], "base")
    _scratch_git(repo, "checkout", "-q", "-b", "X")
    _commit(repo, log, ["A", "L"], "X adds L")
    _scratch_git(repo, "checkout", "-q", "main")
    _scratch_git(repo, "checkout", "-q", "-b", "Y")
    _commit(repo, log, ["A", "M"], "Y adds M")
    subprocess.run(["git", "merge", "-q", "X", "-m", "merge X"], cwd=repo, capture_output=True)
    _commit(repo, log, ["A", "M"], "merge X into Y (drops L)")
    head = _scratch_git(repo, "rev-parse", "HEAD")
    assert len(_scratch_git(repo, "rev-list", "--parents", "-n", "1", head).split()) == 3
    pr = {"CI": "true", "LOG_BASE_SHA": base, "LOG_DEFAULT_BRANCH": "main"}
    for env in (pr, {**pr, "LOG_BASE_SHA": "0" * 40}):
        found = tuning.append_only_problems(log, repo, env)
        assert found and any(p.startswith(f"merge {head[:12]} drops") for p in found), found
    # fast-forwarded onto main and pushed: still caught
    _scratch_git(repo, "checkout", "-q", "main")
    _scratch_git(repo, "merge", "-q", "--ff-only", "Y")
    found = tuning.append_only_problems(log, repo, {"CI": "true", "LOG_BASE_SHA": base,
                                                    "LOG_DEFAULT_BRANCH": "main"})  # fmt: skip
    assert found and any("merge" in p for p in found)
    # a merge that keeps every parent's log (one side unchanged) is clean
    _scratch_git(repo, "checkout", "-q", "-b", "Z", base)
    _scratch_git(repo, "commit", "-q", "--allow-empty", "-m", "no log change")
    _scratch_git(repo, "checkout", "-q", "X")
    _scratch_git(repo, "merge", "-q", "--no-ff", "Z", "-m", "merge Z into X")
    assert tuning.append_only_problems(log, repo, {"CI": "true", "LOG_BASE_SHA": base}) == []
