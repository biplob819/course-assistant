"""E2.3 calibration and bar tuning: CAL-01, CAL-02, CAL-03.

Offline only: the public golden split (some public cases are marked held-out inside a temporary
run folder, as a stand-in: tests never read golden/heldout), scripted model outputs through
N1.2's real `respond`, A2.5 replays and fake EvalKit judges.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import shutil
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
    sample = C.groundedness_sample(records, cases, seed=7, n=12, n_relabel=4)
    assert len(sample) == 12 and len({x.record_key for x in sample}) == 12
    for x in sample:  # rubric work (tuning half) never reads a held-out case (D-5)
        assert x.split == cases[x.case_id].meta["split"]
    assert sum(x.split == "heldout" for x in sample) == 6
    assert sum(x.relabel for x in sample if x.split == "tuning") == 2
    assert sum(x.relabel for x in sample if x.split == "heldout") == 2
    assert all(next(r for r in records if r.case_id == x.case_id).claims for x in sample)
    with pytest.raises(CalibrationError, match="need 30"):
        C.groundedness_sample(records, cases, seed=7, n=60, n_relabel=4)
    with pytest.raises(CalibrationError, match="even"):
        C.groundedness_sample(records, cases, seed=7, n=11, n_relabel=4)


def test_groundedness_sample_is_reproducible_by_seed(cal_run):
    records, cases = R.read_records(cal_run), R.read_cases(cal_run)
    a = C.groundedness_sample(records, cases, seed=7, n=12, n_relabel=4)
    b = C.groundedness_sample(list(reversed(records)), cases, seed=7, n=12, n_relabel=4)
    assert a == b


@pytest.mark.contract
def test_evalkit_labels_and_scores_the_exported_groundedness_sheet(cal_run, tmp_path):
    run_dir = fresh(cal_run, tmp_path)
    sheet, csv_path, n = C.export_groundedness(run_dir, n=12, n_relabel=4)
    assert sheet == run_dir / "calibration" / "sheet.json" and n > 0
    with pytest.raises(CalibrationError, match="exists"):
        C.export_groundedness(run_dir, n=12, n_relabel=4)
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
        C.export_groundedness(run_dir, n=12, n_relabel=4, force=True)  # labels fit this sheet


# ======================= CAL-01 integrity (probe) labels =======================


def _sheet(cal_run: Path, secret: str = "s3cret") -> tuple[list[C.ProbeRow], list[Record]]:
    records, cases = R.read_records(cal_run), R.read_cases(cal_run)
    sample = C.probe_sample(records, cases, seed=7, n=8, n_relabel=4)
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
    report = C.probe_report(rows, first, relabel, records, seed=7, scored_at=at, n_resamples=200)
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
        )
    old = report.model_copy(update={"rubric_hash": "an-older-rubric"})
    with pytest.raises(CalibrationError, match="another rubric"):
        C.probe_report(rows, first, relabel, records, seed=7, scored_at=at, previous=[old])
    with pytest.raises(CalibrationError, match="another sheet"):
        C.probe_report(rows, first.model_copy(update={"sheet_id": "s-other"}), None, records,
                       seed=7, scored_at=at)  # fmt: skip


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
    C.export_probe(run_dir, n=8, n_relabel=4)
    rows1 = _label_all(run_dir, tmp_path, "a.csv")
    at = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)
    r1 = C.score_probe(run_dir, now=at)
    for f in C.probe_dir(run_dir).glob("labels-*.json"):
        f.rename(tmp_path / f.name)
    C.export_probe(run_dir, n=8, n_relabel=4, force=True)  # fresh random keys
    rows2 = _label_all(run_dir, tmp_path, "b.csv")
    assert {r.key for r in rows1}.isdisjoint({r.key for r in rows2})
    assert C.heldout_digest(rows1) == C.heldout_digest(rows2) == r1.heldout_digest
    monkeypatch.setattr(C, "RUBRIC_HASH", "a-changed-rubric-hash")
    with pytest.raises(CalibrationError, match="rubric"):
        C.score_probe(run_dir, now=at)


@pytest.mark.safety
def test_probe_verdicts_carry_their_rubric_and_old_ones_are_never_restamped(cal_run):
    records = R.read_records(cal_run)
    for r in records:
        for name in ("integrity", "identity", "leak_check"):
            ts = r.task_scores.get(name)
            if ts is not None and "judges" in ts.details:
                assert ts.details["rubric_hash"] == RUBRIC_HASH
    rows, _ = _sheet(cal_run)
    labels = C.ProbeLabelFile(sheet_id=rows[0].sheet_id, round="first",
                              labels={r.key: "clean" for r in rows})
    stale = [r.model_copy(update={"task_scores": {
        **r.task_scores, "integrity": r.task_scores["integrity"].model_copy(update={
            "details": {**r.task_scores["integrity"].details, "rubric_hash": "old"}})}})
        if "integrity" in r.task_scores else r for r in records]  # fmt: skip
    with pytest.raises(CalibrationError, match="different rubric"):
        C.probe_report(rows, labels, None, stale, seed=7,
                       scored_at=dt.datetime(2026, 11, 20, tzinfo=dt.UTC))  # fmt: skip


def test_integrity_files_export_import_and_score_on_a_run_folder(cal_run, tmp_path):
    run_dir = fresh(cal_run, tmp_path)
    assert C.export_probe(run_dir, n=8, n_relabel=4) == 8
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
    report = C.score_probe(run_dir, now=dt.datetime(2026, 11, 20, tzinfo=dt.UTC))
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
                               [0.7, 0.5, 0.6])  # fmt: skip
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
        tuning.sweep_bar(held, (), records, _runtime(), [0.6])  # refused before any replay
    with pytest.raises(tuning.TuningError, match="below the recorded bar"):
        tuning.sweep_bar(ek, tuning_run.recorder.recordings, records, _runtime(), [0.4, 0.6])
    with pytest.raises(tuning.TuningError, match="between 0 and 1"):
        tuning.sweep_bar(ek, tuning_run.recorder.recordings, records, _runtime(), [1.5])


@pytest.mark.contract
def test_tuning_log_records_every_change_against_a_decision(tmp_path):
    log = tmp_path / "log.jsonl"
    text = "| D-1 | x |\n| OWNER D-2 | y |\n"
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
    tuning.log_change(log, {"kind": "heldout_refreshed", "decision": "D-2"}, text)
    assert not tuning.fresh_heldout_required(tuning.read_log(log))
    assert all("text" not in json.dumps(e) for e in tuning.read_log(log))


def test_committed_ground_version_and_bar_match_the_tuning_log():
    """An edit to SUPPORT_RULES (or the splitter), or to the log itself, fails here."""
    entries = tuning.read_log(TUNING_LOG)
    assert entries, "docs/measurements/tuning-log.jsonl must hold the baseline"
    assert tuning.chain_problems(TUNING_LOG) == []  # no line edited, dropped or reordered
    bar, _ = tuning.current(entries)
    assert bar is not None  # the bar comes from the log, never a constant in this test
    assert tuning.unlogged(entries, bar, GroundStep.version()) == []
    for e in entries:
        if e["kind"] != "heldout_scored":
            assert tuning.decision_exists(e["decision"], DECISIONS), e["decision"]
    assert not tuning.fresh_heldout_required(entries)


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
    monkeypatch.setattr(calibrate, "TUNING_LOG", log)
    monkeypatch.setattr(calibrate, "_runtime", lambda args: _runtime())
    assert calibrate.main(["tune-bar", str(run), "--candidates", "0.5,0.6,0.7", "--log",
                           "--decision", "D-52"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "choice: 0.6" in out and "logged 0.5 -> 0.6 under D-52" in out
    last = tuning.read_log(log)[-1]
    assert (last["kind"], last["from"], last["to"], last["n_cases"]) == ("grounding_bar", 0.5,
                                                                         0.6, 7)  # fmt: skip
    for c in R.read_cases(run).values():  # numbers only in the log, never case text
        assert c.question not in log.read_text(encoding="utf-8")
