"""E2.5: the full run and the model choice (FRN-01 to FRN-04), offline.

A scripted course-ta run of public cases (fake judges, no network) stands in for the live run
folders; ledgers are synthetic. Tests never read golden/heldout.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from evalkit import run as R

import eval_live
from course_assistant.eval import fullrun as F
from course_assistant.eval import release
from course_assistant.llm import Ledger
from tests.eval import offline_run as O
from tests.eval import scripted_eval as E

ROOT = Path(__file__).resolve().parents[2]
RULES = ("B1", "B1", "B1", "B2", "B3", "B3", "B4", "B7", "B8", "B9", "B9", "B10", "B10")
REPORT = ROOT / "docs" / "measurements" / "e2.5-report.md"


def _cases() -> list[Any]:
    out, seen = [], {}
    for r in RULES:
        k = seen.get(r, 0)
        seen[r] = k + 1
        out.append(E.first(r, multi=False, skip=k) if r == "B1" else E.first(r, skip=k))
    return out


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cases = _cases()
    run = O.offline_run(tmp_path_factory.mktemp("full"), cases,
                        {c.id: E.script_for(c) for c in cases})  # fmt: skip
    return run.run_dir


def ledger_rows(run: str, **by_purpose: float) -> list[dict[str, Any]]:
    return [{"run_id": run, "purpose": p, "cost_usd": c} for p, c in by_purpose.items()] + [
        {"run_id": "another-run", "purpose": "answer", "cost_usd": 99.0}
    ]


# ---------- FRN-01: summaries, comparison, report ----------


def test_run_summary_reads_gates_latency_and_the_run_ids_ledger_spend(run_dir: Path):
    rows = ledger_rows(run_dir.name, answer=0.30, route=0.05, eval_judging=1.0, probe_judging=0.5)
    s = F.run_summary(run_dir, rows, "sol")
    assert s.n == len(RULES) and s.judge_mode == "dual"
    assert set(F.QUALITY) <= set(s.gates) and "latency_p95_ms" in s.gates
    assert s.system_usd == pytest.approx(0.35) and s.judging_usd == pytest.approx(1.5)
    assert s.total_usd == pytest.approx(1.85)
    assert s.cost_per_question_usd == pytest.approx(0.35 / s.n)
    assert s.grounded_cases == int(sum(s.values["grounded"].values()))


def test_run_summary_refuses_a_record_without_scores(run_dir: Path, tmp_path: Path):
    bad = tmp_path / "run"
    shutil.copytree(run_dir, bad)
    path = bad / "records.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows[0]["scores"] = None
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    with pytest.raises(F.FullRunError, match="no scores"):
        F.run_summary(bad, [], "sol")


def test_compare_of_a_run_with_itself_separates_nothing(run_dir: Path):
    s = F.run_summary(run_dir, [], "sol")
    diffs = F.compare(s, s)
    assert {d.metric for d in diffs} == set(release.GATED_METRICS)
    assert all(d.diff == 0 and not d.separated for d in diffs)


def test_the_report_holds_ids_and_numbers_and_no_case_text(run_dir: Path):
    s = F.run_summary(run_dir, ledger_rows(run_dir.name, answer=0.2), "sol")
    choice = F.choose([F.Candidate("sol", 7000, s.quality(), 0.01)], "sol")
    text = F.render_report([s], {"Sol minus Sol": F.compare(s, s)}, choice, ["a note"])
    assert "| sol |" in text and "dual" in text and "Chosen: **sol**" in text
    for case in R.read_cases(run_dir).values():
        assert case.question not in text


def test_the_published_report_quotes_no_public_question():
    # every published E2.5 report, the held-out ones too (E2.5 verifier: only the first was read)
    reports = [REPORT, *sorted(REPORT.parent.glob("e2.5-heldout*-report.md"))]
    reports = [r for r in reports if r.is_file()]
    if not reports:
        pytest.skip("the report is written by scripts/full_run_report.py after the live run")
    from course_assistant.eval.dataset import load_public

    questions = [c.question for c in load_public(E.PUBLIC)]
    for r in reports:
        text = r.read_text(encoding="utf-8")
        assert all(q not in text for q in questions), r.name
    assert any("heldout4" in r.name for r in reports)


def test_eval_live_ledgers_judging_spend_under_the_run_id(run_dir: Path, tmp_path: Path):
    live = tmp_path / "course-ta-heldout-20261117T000000Z"
    (live / "evalkit").mkdir(parents=True)
    shutil.copytree(run_dir, live / "evalkit" / run_dir.name)
    manifest = live / "evalkit" / run_dir.name / "run.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    reported = sum(r.completion.cost_usd for r in R.read_records(run_dir))
    data["spent_usd"] = reported + 1.25
    manifest.write_text(json.dumps(data), encoding="utf-8")
    (live / "probe-judges.json").write_text(json.dumps({"probe_judge_usd": 0.5}), "utf-8")
    ledger = Ledger(tmp_path / "ledger.jsonl", 150.0)
    judged, probed = eval_live.ledger_judging(ledger, live)
    assert judged == pytest.approx(1.25) and probed == pytest.approx(0.5)
    rows = F.read_ledger(tmp_path / "ledger.jsonl")
    assert F.spend_of(rows, live.name) == pytest.approx({"eval_judging": 1.25,
                                                         "probe_judging": 0.5})  # fmt: skip
    empty = tmp_path / "no-run"
    empty.mkdir()
    assert eval_live.ledger_judging(ledger, empty) == (0.0, 0.0)


# ---------- FRN-02: the choice rule ----------

GOOD = {"grounded": 0.9, "integrity": 1.0, "injection": 1.0, "leak": 1.0, "identity": 1.0}


def cand(label: str, p95: int, cost: float | None = 0.01, **quality: float) -> F.Candidate:
    return F.Candidate(label, p95, {**GOOD, **quality}, cost)


def test_choose_takes_quality_first_then_cost_under_the_latency_target():
    c = F.choose([cand("ref", 17000, 0.02), cand("fast-cheap", 7000, 0.005, grounded=0.92),
                  cand("fast-better", 7900, 0.02, grounded=0.95)], "ref")  # fmt: skip
    assert c.chosen == "fast-better" and c.order == ["fast-better", "fast-cheap"]
    assert "latency p95 17000" in c.reasons["ref"][0]


def test_nfr5_is_11_seconds_at_p95_and_the_cached_setting_meets_it():
    """OWNER D-65: NFR5 restated to p95 <= 11 s; the cached Sol setting measured 10.2 s."""
    assert release.LATENCY_P95_MS == 11_000 and F.LATENCY_TARGET_MS == 11_000
    c = F.choose([cand("ref", 10200), cand("over", 11001)], "ref")
    assert c.chosen == "ref" and any("latency p95 11001" in r for r in c.reasons["over"])
    gates, errors = release.gates({m: {"a": 1.0, "b": 1.0} for m in release.GATED_METRICS},
                                  [10200, 10900, 11000])  # fmt: skip
    assert not errors and next(g for g in gates if g.name == "latency_p95_ms").passed


def test_choose_breaks_a_quality_tie_on_cost_per_grounded_answer():
    c = F.choose([cand("ref", 7000, 0.02), cand("cheap", 7000, 0.01)], "ref")
    assert c.chosen == "cheap"


def test_choose_never_trades_a_safety_value_for_speed_or_cost():
    c = F.choose([cand("ref", 12000), cand("leaky", 5000, 0.001, leak=0.95),
                  cand("slow", 11001)], "ref")  # fmt: skip
    assert c.chosen is None and c.order == []
    assert any("leak" in r for r in c.reasons["leaky"])
    assert any("latency" in r for r in c.reasons["slow"])
    with pytest.raises(F.FullRunError):
        F.choose([cand("a", 1)], "missing")


# ---------- FRN-03: the cost model ----------


def test_cost_model_projects_a_month_against_the_cap_and_alarm():
    m = F.cost_model(0.007, 6000, 15.0, 6)
    assert m.answer_path_usd == pytest.approx(42.0) and m.runs_usd == pytest.approx(90.0)
    assert m.monthly_usd == pytest.approx(132.0) and m.within_cap and m.under_alarm
    assert not m.under_prd_nfr6
    assert not F.cost_model(0.007, 6000, 16.0, 6).under_alarm  # $138 is past the $135 alarm
    assert pytest.approx(135) == F.ALARM_SHARE * F.MONTHLY_CAP_USD  # OWNER D-64


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.01, True, "1"])
def test_cost_model_refuses_a_bad_amount(bad: Any):
    with pytest.raises(F.FullRunError):
        F.cost_model(bad, 10, 1.0, 1)
    with pytest.raises(F.FullRunError):
        F.spend_of([{"run_id": "r", "purpose": "answer", "cost_usd": bad}], "r")


# ---------- FRN-04: the records to read ----------


def test_sample_for_reading_is_20_public_ids_stratified_and_fixed_by_seed():
    rules = {f"ta-{r}-{i:03d}": r for r in ("b1", "b2", "b3", "b8") for i in range(30)}
    held = {f"ta-b1-{i:03d}" for i in range(15)}
    a = F.sample_for_reading(rules, held, 20, seed=7)
    assert a == F.sample_for_reading(rules, held, 20, seed=7)
    assert len(a) == len(set(a)) == 20 and not set(a) & held
    assert {rules[i] for i in a} == {"b1", "b2", "b3", "b8"}
    assert max(sum(rules[i] == r for i in a) for r in ("b1", "b2", "b3", "b8")) == 5
    assert F.sample_for_reading(rules, held, 20, seed=8) != a


def test_full_run_report_script_writes_the_report_with_no_case_text(run_dir: Path,
                                                                   tmp_path: Path):  # fmt: skip
    import full_run_report

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"run_id": run_dir.name, "purpose": "answer", "cost_usd": 0.1,
                                  "at": "2026-11-17T00:00:00+00:00"}) + "\n", "utf-8")  # fmt: skip
    out = tmp_path / "report.md"
    code = full_run_report.main(["--run", f"sol={run_dir}", "--run", f"luna={run_dir}",
                                 "--reference", "sol", "--ledger", str(ledger),
                                 "--write", str(out)])  # fmt: skip
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "sol minus luna" in text and "Cost model (sol)" in text
    for case in R.read_cases(run_dir).values():
        assert case.question not in text


def test_smoke_row_costs_questions_from_the_ledger_not_the_turns(tmp_path: Path):
    smoke = {
        "run_id": "smoke-x-1",
        "smoke": {
            "status_match": 25,
            "summary": {
                "n": 30,
                "question": {"p50_ms": 5000, "p95_ms": 9000},
                "cost": {"mean_usd": 0.001},
            },
        },
    }
    path = tmp_path / "smoke.json"
    path.write_text(json.dumps(smoke), encoding="utf-8")
    rows = ledger_rows("smoke-x-1", answer=0.24, route=0.06)
    row = F.smoke_row(path, "x", rows)
    assert row.mean_usd == pytest.approx(0.01) and row.p95_ms == 9000 and row.status_match == 25
    text = F.render_report([], {}, None, smoke=[row])
    assert "| x | smoke-x-1 | 30 | 5.0 s | 9.0 s | $0.0100 | 25/30 |" in text


def test_report_projects_cost_from_a_smoke_row_when_no_judged_run_exists(tmp_path: Path):
    import full_run_report

    smoke = {"run_id": "smoke-pub-x-1", "smoke": {"status_match": 1, "summary": {
        "n": 10, "question": {"p50_ms": 1, "p95_ms": 2}, "cost": {"mean_usd": 0.0}}}}  # fmt: skip
    path = tmp_path / "smoke.json"
    path.write_text(json.dumps(smoke), encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"run_id": "smoke-pub-x-1", "purpose": "answer",
                                  "cost_usd": 0.07}) + "\n", "utf-8")  # fmt: skip
    out = tmp_path / "r.md"
    assert full_run_report.main(["--smoke", f"x={path}", "--cost-from", "x", "--run-usd", "20",
                                 "--ledger", str(ledger), "--write", str(out)]) == 0  # fmt: skip
    text = out.read_text(encoding="utf-8")
    assert "Cost model (x): 1000 questions a month at $0.0070 = $7.00" in text
    assert "6 full runs at $20.00 = $120.00; month $127.00" in text


def test_eval_live_planning_figures_cover_the_e25_measurement_with_margin():
    """E2.5 replaced eval_live's planning figures with the public tuning run's measurement
    (214 cases, 110 probes, 98 answered): each figure stays at least 1.5 times the measured
    cost, so a run's cap still covers what it spends."""
    m = eval_live.MEASURED_E25
    assert 1.5 * m["evalkit_usd_per_case"] <= eval_live.EVALKIT_USD_PER_CASE
    assert 1.5 * m["judge_usd_per_probe"] <= eval_live.JUDGE_USD_PER_PROBE
    assert 1.5 * m["support_usd_per_answer"] <= eval_live.SUPPORT_USD_PER_ANSWER
    assert eval_live.PANEL_HEADROOM >= 2.0


def test_run_summary_of_one_split_of_a_both_split_run(run_dir: Path, tmp_path: Path):
    """D-67: one run on both splits; the report shows each split's gates (numbers only)."""
    both = tmp_path / "both"
    shutil.copytree(run_dir, both)
    ids = sorted(R.read_cases(both))
    O.mark_heldout(both, set(ids))
    rows = ledger_rows(both.name, answer=1.3, eval_judging=2.6)
    whole = F.run_summary(both, rows, "all")
    held = F.run_summary(both, rows, "held-out", split="heldout")
    assert held.n == whole.n == len(ids) and held.gates == whole.gates
    assert held.system_usd == pytest.approx(1.3) and held.judging_usd == pytest.approx(2.6)
    with pytest.raises(F.FullRunError, match="no case"):
        F.run_summary(both, rows, "tuning", split="tuning")


def test_report_script_takes_one_split_of_a_run(run_dir: Path, tmp_path: Path):
    import full_run_report

    both = tmp_path / "both"
    shutil.copytree(run_dir, both)
    O.mark_heldout(both, set(R.read_cases(both)))
    out = tmp_path / "r.md"
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", "utf-8")
    assert full_run_report.main(["--run", f"held-out={both}#heldout", "--ledger", str(ledger),
                                 "--write", str(out)]) == 0  # fmt: skip
    assert "| held-out | both |" in out.read_text(encoding="utf-8")


def test_ledger_judging_ledgers_only_spend_not_yet_ledgered(run_dir: Path, tmp_path: Path):
    """A resumed run (EvalKit exit 4, batches collected later) must not ledger twice."""
    live = tmp_path / "course-ta-both-20261117T000000Z"
    (live / "evalkit").mkdir(parents=True)
    shutil.copytree(run_dir, live / "evalkit" / run_dir.name)
    manifest = live / "evalkit" / run_dir.name / "run.json"
    reported = sum(r.completion.cost_usd for r in R.read_records(run_dir))

    def spent(extra: float, probe: float) -> None:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["spent_usd"] = reported + extra
        manifest.write_text(json.dumps(data), encoding="utf-8")
        (live / "probe-judges.json").write_text(json.dumps({"probe_judge_usd": probe}), "utf-8")

    ledger = Ledger(tmp_path / "ledger.jsonl", 150.0)
    spent(1.0, 0.5)
    assert eval_live.ledger_judging(ledger, live) == pytest.approx((1.0, 0.5))
    assert eval_live.ledger_judging(ledger, live) == (0.0, 0.0)  # nothing new
    spent(1.75, 0.5)
    assert eval_live.ledger_judging(ledger, live) == pytest.approx((0.75, 0.0))
    rows = F.read_ledger(tmp_path / "ledger.jsonl")
    assert F.spend_of(rows, live.name) == pytest.approx({"eval_judging": 1.75,
                                                         "probe_judging": 0.5})  # fmt: skip


def test_resume_inputs_read_the_folder_and_refuse_a_partial_one(run_dir: Path, tmp_path: Path):
    live = tmp_path / "course-ta-both-20261117T000000Z"
    live.mkdir()
    with pytest.raises(ValueError, match="not an eval_live folder"):
        eval_live.resume_inputs(live)
    (live / "course-ta.yaml").write_text("name: course-ta\n", "utf-8")
    (live / "cases.jsonl").write_text('{"id": "a"}\n{"id": "b"}\n', "utf-8")  # after drift
    (live / "drift.json").write_text(json.dumps({"retired": ["c"]}), "utf-8")
    task, n, split = eval_live.resume_inputs(live)
    assert task == live / "course-ta.yaml" and n == 2 and split == "both"
    assert eval_live.merge_probe_summary({"probe_judge_calls": 3, "probe_judge_usd": 0.5},
                                         2, 0.25) == {"probe_judge_calls": 5,
                                                      "probe_judge_usd": 0.75}  # fmt: skip


def test_a_resumed_run_never_logs_heldout_scored_again():
    assert eval_live.logs_heldout_score("both", replay=False, resume=True) is False
    assert eval_live.logs_heldout_score("both", replay=False, resume=False) is True


def test_report_compares_only_the_pairs_asked_for(run_dir: Path, tmp_path: Path):
    import full_run_report

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", "utf-8")
    out = tmp_path / "r.md"
    args = ["--run", f"a={run_dir}", "--run", f"b={run_dir}", "--run", f"c={run_dir}",
            "--compare", "b:c", "--ledger", str(ledger), "--write", str(out)]  # fmt: skip
    assert full_run_report.main(args) == 0
    text = out.read_text(encoding="utf-8")
    assert "b minus c" in text and "a minus" not in text
    with pytest.raises(SystemExit):
        full_run_report.main([*args[:-2], "--compare", "a:zz"])


def test_the_calibration_cell_is_our_certified_calibration_not_evalkits_status():
    # D-83: the heldout-3 report showed EvalKit's "uncalibrated" for a run whose calibration
    # (certified on another run folder) the gate accepts
    from course_assistant.eval.fullrun import calibration_label

    both = {"groundedness": "calibrated", "integrity": "calibrated"}
    assert calibration_label(both) == "certified"
    assert calibration_label({**both, "integrity": "uncalibrated"}) == "uncalibrated: integrity"
    assert calibration_label({}) == "unknown"
    panel = {**both, "integrity_labeller": "model-panel:a+b", "groundedness_labeller": "owner"}
    assert calibration_label(panel) == "certified (model panel)"
    assert calibration_label({"integrity_labeller": "owner"}) == "unknown"


def test_calibration_takes_the_run_folder_or_its_evalkit_folder_like_the_gate(tmp_path: Path):
    # E2.5 verifier: given the run folder, the report printed "uncalibrated" without a word,
    # while release_gate.py resolves it to the one EvalKit run inside
    import full_run_report

    inner = tmp_path / "evalkit" / "r-1"
    inner.mkdir(parents=True)
    (inner / "run.json").write_text("{}", encoding="utf-8")
    assert full_run_report.calibration_folder(tmp_path) == inner
    assert full_run_report.calibration_folder(inner) == inner
    with pytest.raises(SystemExit):
        full_run_report.calibration_folder(tmp_path / "missing")
