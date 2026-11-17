"""E2.4: the release gate (`make eval`, Tech Spec §6.3 to §6.5, FR11, Flow C).

Offline only: a scripted course-ta run of public cases, relabelled as held-out inside its
temporary run folder (tests never read golden/heldout), fake judges, temporary corpus stores,
no network. The live gate on real held-out outputs is pending (RUN-STATE).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from evalkit import run as R

import eval_live
import release_gate as G
from course_assistant.eval import release
from course_assistant.eval.release import GateInputs, ReleaseError, Verdict
from course_assistant.ingest import CorpusStore
from course_assistant.ingest.corpus import Corpus, unit_key
from course_assistant.ingest.corpus import corpus_version as version_of
from tests.eval import offline_run as O
from tests.eval import scripted_eval as E

ROOT = Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.UTC)
# two or more cases per gated metric, so every regression comparison can run (MIN_PAIRED)
RULES = ("B1", "B1", "B1", "B2", "B3", "B3", "B4", "B7", "B8", "B9", "B9", "B10", "B10")
CALIBRATED = {"groundedness": "calibrated", "integrity": "calibrated"}
RH = {"rules_hash": "rh", "grounding_bar": 0.5}  # the gated settings _passing flips record


def _cases() -> list[Any]:
    out, seen = [], {}
    for r in RULES:
        k = seen.get(r, 0)
        seen[r] = k + 1
        out.append(E.first(r, multi=False, skip=k) if r == "B1" else E.first(r, skip=k))
    return out


@pytest.fixture(scope="module")
def held_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A finished scripted run whose cases stand in for the held-out split."""
    cases = _cases()
    run = O.offline_run(tmp_path_factory.mktemp("gate"), cases,
                        {c.id: E.script_for(c) for c in cases})  # fmt: skip
    O.mark_heldout(run.run_dir, {c.id for c in cases})
    return run.run_dir


def fresh(held_run: Path, tmp_path: Path, name: str = "run") -> Path:
    out = tmp_path / name
    shutil.copytree(held_run, out)
    return out


def ids(run: Path) -> frozenset[str]:
    return frozenset(R.read_cases(run))


def rows_of(run: Path) -> dict[str, dict[str, Any]]:
    """The run's cases as dataset rows, as they are now (the cases the run should hold)."""
    return {c.id: {"id": c.id, "question": c.question, "label": c.reference_label,
                   "meta": c.meta, "context": list(c.context)}
            for c in R.read_cases(run).values()}  # fmt: skip


def inputs(run: Path, **kw: Any) -> GateInputs:
    base: dict[str, Any] = {"heldout_ids": ids(run), "calibration": CALIBRATED,
                            "fixture_problems": [], "log_problems": [],
                            "coverage_problems": [], "exposure": [],
                            "expected_cases": rows_of(run)}  # fmt: skip
    return GateInputs(**{**base, **kw})


def private(tmp_path: Path, run: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in private held-out folder: the run's public cases relabelled held-out, with a
    MANIFEST written by E2.1's tool (tests never read golden/heldout)."""
    import golden_tool as gt

    folder = tmp_path / "private-heldout"
    folder.mkdir(exist_ok=True)
    lines = [json.dumps({**E.BY_ID[i].model_dump(mode="json"), "split": "heldout"},
                        sort_keys=True) for i in sorted(ids(run))]  # fmt: skip
    (folder / "cases.jsonl").write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    manifest = folder / "MANIFEST.json"
    manifest.write_text(json.dumps(gt.manifest_for(lines)), encoding="utf-8")
    monkeypatch.setattr(G, "HELDOUT_DIR", folder)
    monkeypatch.setattr(G, "HELDOUT_MANIFEST", manifest)
    monkeypatch.setattr(G, "exposure", lambda held: None)  # public text is public by design
    return folder


def edit_records(run: Path, fn: Any) -> None:
    path = run / "records.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    path.write_text("".join(json.dumps(fn(r)) + "\n" for r in rows), encoding="utf-8")


def edit_json(path: Path, fn: Any) -> None:
    path.write_text(json.dumps(fn(json.loads(path.read_text(encoding="utf-8")))),
                    encoding="utf-8")  # fmt: skip


def rule_of(run: Path, case_id: str) -> str:
    return str(R.read_cases(run)[case_id].meta["rule"])


def first_id(run: Path, rule: str) -> str:
    return next(i for i, c in sorted(R.read_cases(run).items()) if c.meta["rule"] == rule)


# ================== GAT-01: make eval applies §6.5 with EvalKit's exit codes ==================


def test_a_passing_heldout_run_exits_0_with_every_gate_and_no_case_text(held_run, tmp_path):
    run = fresh(held_run, tmp_path)
    v = release.verdict(run, inputs(run))
    assert v.exit_code == 0, (v.failures, v.errors)
    assert {g.name for g in v.gates} == {"grounded", "citation_validity", "locator_correct",
                                         "handover", "integrity", "injection", "leak",
                                         "identity", "latency_p95_ms"}  # fmt: skip
    assert all(g.passed for g in v.gates)
    assert "no live baseline: first release, nothing to regress against" in v.notes
    assert v.corpus_version == E.CORPUS.corpus_version
    text = v.to_json()
    for case in R.read_cases(run).values():
        assert case.question not in text  # ids and numbers only
    assert release.verdict(run, inputs(run)).to_json() == text  # deterministic


def _fail_b3(r: dict[str, Any]) -> dict[str, Any]:
    if "integrity" in r["task_scores"] and r["task_scores"]["integrity"]["details"].get(
            "applicable"):  # fmt: skip
        r["task_scores"]["integrity"]["value"] = 0.0
    return r


def _ungrounded_b1(run: Path) -> Any:
    b1 = first_id(run, "B1")

    def fn(r: dict[str, Any]) -> dict[str, Any]:
        if r["case_id"] == b1:
            r["scores"]["fully_grounded"] = {k: False for k in r["scores"]["fully_grounded"]}
        return r

    return fn


def _slow(r: dict[str, Any]) -> dict[str, Any]:
    r["completion"]["latency_ms"] = 12000  # over NFR5 (11 s at p95, OWNER D-65)
    return r


@pytest.mark.safety
def test_each_planted_failure_exits_1_and_is_named(held_run, tmp_path):
    cases = [
        ("integrity", lambda run: edit_records(run, _fail_b3), {}, "gate integrity"),
        ("grounded", lambda run: edit_records(run, _ungrounded_b1(run)), {}, "gate grounded"),
        ("latency", lambda run: edit_records(run, _slow), {}, "gate latency_p95_ms"),
        ("single", lambda run: edit_json(run / "summary.json",
                                         lambda d: {**d, "judge_mode": "single"}), {},
         "judge_mode is not dual"),
        ("uncalibrated", None, {"calibration": {**CALIBRATED, "integrity": "uncalibrated"}},
         "integrity calibration is not certified"),
        ("fixture", None, {"fixture_problems": ["summary.json differs"]}, "regression fixture"),
        ("log", None, {"log_problems": ["ground version drifted"]}, "tuning log"),
        ("coverage", None, {"coverage_problems": ["session s04 has 0 B1 cases (needs 15)"]},
         "coverage: session s04"),
        ("exposure", None, {"exposure": ["ta-b1-007"]}, "exposed in tracked files (D-54)"),
    ]  # fmt: skip
    for name, plant, kw, message in cases:
        run = fresh(held_run, tmp_path, name)
        if plant is not None:
            plant(run)
        v = release.verdict(run, inputs(run, **kw))
        assert v.exit_code == 1, name
        assert any(message in f for f in v.failures), (name, v.failures)
    run = fresh(held_run, tmp_path, "skipped")
    v = release.verdict(run, inputs(run, exposure=None))
    assert v.exit_code == 0
    assert "held-out exposure guard skipped (private held-out file absent)" in v.notes


@pytest.mark.safety
def test_a_run_that_is_not_exactly_the_manifest_heldout_set_exits_2(held_run, tmp_path):
    """Membership comes from the MANIFEST ids (minus drift-retired ones), never meta.split."""
    run = fresh(held_run, tmp_path)
    all_ids = ids(run)
    some = sorted(all_ids)[0]
    assert release.verdict(run, inputs(run, heldout_ids=all_ids - {some})).exit_code == 2
    assert release.verdict(run, inputs(run, heldout_ids=all_ids | {"ta-b1-999"})).exit_code == 2
    retired = release.verdict(run, inputs(run, heldout_ids=all_ids | {"ta-b1-999"},
                                          retired=frozenset({"ta-b1-999"})))  # fmt: skip
    assert retired.exit_code == 0 and "1 held-out case(s) retired by drift" in retired.notes
    public = fresh(held_run, tmp_path, "public")  # a run whose meta says tuning, not in MANIFEST
    edit_json_lines = public / "cases.jsonl"
    rows = [json.loads(x) for x in edit_json_lines.read_text("utf-8").splitlines() if x.strip()]
    for r in rows:
        r["meta"]["split"] = "tuning"
    edit_json_lines.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert release.verdict(public, inputs(public, heldout_ids=frozenset())).exit_code == 2


def test_a_missing_score_exits_2_and_is_never_defaulted(held_run, tmp_path):
    run = fresh(held_run, tmp_path)
    b9 = first_id(run, "B9")

    def drop(r: dict[str, Any]) -> dict[str, Any]:
        if r["case_id"] == b9:
            del r["task_scores"]["identity"]
        return r

    edit_records(run, drop)
    v = release.verdict(run, inputs(run))
    assert v.exit_code == 2 and v.errors == [f"record {b9} has no identity score"]


def test_cost_cap_exits_3_pending_exits_4_and_precedence_is_2_3_4_1(held_run, tmp_path):
    run = fresh(held_run, tmp_path, "cap")
    lines = (run / "records.jsonl").read_text("utf-8").splitlines()
    (run / "records.jsonl").write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    assert release.verdict(run, inputs(run)).exit_code == 3
    nosummary = fresh(held_run, tmp_path, "nosummary")
    (nosummary / "summary.json").unlink()
    assert release.verdict(nosummary, inputs(nosummary)).exit_code == 3
    pending = fresh(held_run, tmp_path, "pending")
    edit_json(pending / "summary.json", lambda d: {**d, "judging_status": "pending"})
    assert release.verdict(pending, inputs(pending)).exit_code == 4
    # precedence: a failing gate under pending judging is 4; under a cost-cap stop 3; an input
    # error wins over everything
    edit_records(pending, _fail_b3)
    assert release.verdict(pending, inputs(pending)).exit_code == 4
    edit_records(run, _fail_b3)
    edit_json(run / "summary.json", lambda d: {**d, "judging_status": "pending"})
    assert release.verdict(run, inputs(run)).exit_code == 3
    assert release.verdict(run, inputs(run, heldout_ids=frozenset())).exit_code == 2
    assert release.verdict(tmp_path / "nowhere", inputs(run)).exit_code == 2


def test_regression_against_the_live_baseline_is_paired_with_holm(held_run, tmp_path):
    run = fresh(held_run, tmp_path)
    v = release.verdict(run, inputs(run))
    many = {f"c{i}": 1.0 for i in range(40)}
    worse = {**many, **{f"c{i}": 0.0 for i in range(12)}}

    def every(grounded: dict[str, float]) -> dict[str, dict[str, float]]:
        return {m: (grounded if m == "grounded" else many) for m in release.GATED_METRICS}

    def grounded_of(found: list[Any]) -> Any:
        return next(r for r in found if r.metric == "grounded")

    found = release.regressions(every(worse), every(many))
    assert grounded_of(found).separated and grounded_of(found).diff < 0
    assert [r.metric for r in found if r.separated] == ["grounded"]
    assert not grounded_of(release.regressions(every(many), every(many))).separated
    slight = {**many, "c0": 0.0}
    assert not grounded_of(release.regressions(every(slight), every(many))).separated
    better = release.regressions(every(many), every(worse))
    assert not grounded_of(better).separated  # an improvement is never a regression
    # wired into the verdict: a baseline the run falls below is a failure
    baseline = {m: dict(vals) for m, vals in v.values.items()}
    assert release.verdict(run, inputs(run, baseline=baseline)).exit_code == 0
    worse_run = fresh(held_run, tmp_path, "worse")
    edit_records(worse_run, _fail_b3)
    big = {m: {**vals} for m, vals in v.values.items()}
    rv = release.verdict(worse_run, inputs(worse_run, baseline=big))
    assert rv.exit_code == 1 and rv.regressions
    snap = release.verdict(run, inputs(run, baseline=baseline, baseline_snapshots=["old-snap"]))
    assert any("provider drift" in n for n in snap.notes)


@pytest.mark.privacy
def test_heldout_exposure_guard_reports_ids_only(tmp_path):
    q = "What does the course say about attention heads in long documents?"
    found = release.heldout_exposure({"ta-b1-500": q, "ta-b1-501": "too short"},
                                     ["some file", f"x = {q!r}", "too short"])  # fmt: skip
    assert found == ["ta-b1-500"]
    assert release.heldout_exposure({"ta-b1-500": q}, ["unrelated"]) == []
    assert G.exposure(None) is None  # no private file: skipped, never a pass claimed


def _store(tmp_path: Path, corpus: Corpus = E.CORPUS) -> tuple[Path, CorpusStore]:
    """An AppConfig YAML and its corpus store with `corpus` ingested (no API)."""
    config = tmp_path / "course" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump(E.CONFIG.model_dump(mode="json")), encoding="utf-8")
    store = CorpusStore(config.parent / "corpus_store", E.CONFIG.cohort)
    store.save(corpus)
    return config, store


@pytest.mark.contract
def test_release_gate_check_reads_certified_calibration_the_fixture_and_writes_gate_json(
        held_run, tmp_path, monkeypatch):  # fmt: skip
    run = fresh(held_run, tmp_path)
    config, _ = _store(tmp_path)
    private(tmp_path, run, monkeypatch)
    seen: list[Path] = []
    real = G.calibration_status
    monkeypatch.setattr(G, "calibration_status", lambda d: seen.append(d) or real(d))
    out = tmp_path / "gate.json"
    code = G.main(["check", str(run), "--config", str(config), "--out", str(out)])
    assert seen == [run]  # certified_calibration of the run (no --calibration given)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert code == data["exit_code"] == 1
    assert any("groundedness calibration is not certified" in f for f in data["failures"])
    assert any(f.startswith("coverage: assessment") for f in data["failures"])
    assert not any(f.startswith("regression fixture") for f in data["failures"])  # it replays
    assert "held-out exposure guard skipped (private held-out file absent)" in data["notes"]
    other = fresh(held_run, tmp_path, "calibration-run")
    G.main(["check", str(run), "--config", str(config), "--out", str(out),
            "--calibration", str(other)])  # fmt: skip
    assert seen[-1] == other
    monkeypatch.setattr(G, "HELDOUT_MANIFEST", tmp_path / "missing.json")
    assert G.main(["check", str(run), "--config", str(config), "--out", str(out)]) == 2


@pytest.mark.contract
def test_make_targets_call_the_release_gate_script_with_json():
    def dry(*args: str) -> str:
        return subprocess.run(["make", "-n", *args], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout  # fmt: skip

    assert "scripts/release_gate.py eval --config" in dry("eval", "YES=--yes")
    assert "--split heldout --yes --json" in dry("eval", "YES=--yes", "JSON=1")
    assert "scripts/release_gate.py check runs/x" in dry("eval-check", "RUN=runs/x")
    assert dry("eval-offline", "JSON=1").strip().endswith("release_gate.py offline --json")
    assert "--json" not in dry("eval-offline")


# ================= GAT-02: CI on manifest or rules change, weekly, artifacts =================


WORKFLOW = ROOT / ".github" / "workflows" / "eval-gate.yml"


@pytest.mark.contract
def test_eval_gate_workflow_runs_on_manifest_and_rules_changes_and_weekly():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = wf[True]  # YAML reads the bare key `on` as true
    for event in ("push", "pull_request"):
        paths = on[event]["paths"]
        for needed in ("**/manifest.yaml", "src/course_assistant/answer/**",
                       "docs/measurements/tuning-log.jsonl", "golden/**",
                       "src/course_assistant/eval/**", "src/course_assistant/llm/**",
                       "uv.lock"):  # fmt: skip
            assert needed in paths, (event, needed)
    assert on["schedule"][0]["cron"].split()[-1] == "1"  # weekly
    assert "workflow_dispatch" in on
    jobs = wf["jobs"]
    offline_text = yaml.safe_dump(jobs["offline"])
    assert "secrets." not in offline_text  # the offline part never needs a secret
    steps = jobs["offline"]["steps"]
    assert any(
        s.get("run") == "uv run python scripts/release_gate.py offline --json" for s in steps
    )  # the script's own exit code, not make's 2
    upload = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert upload and upload[0]["with"]["path"] == "runs/gate/"


@pytest.mark.safety
def test_live_job_skips_every_step_without_the_secrets_and_uploads_no_run_folder():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    live = wf["jobs"]["live"]
    assert "env" not in live  # secrets reach only the steps that need them
    check, *rest = live["steps"]
    assert set(check["env"]) == {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HELDOUT_BUNDLE"}
    needs = {s.get("name", s.get("uses", "")): set(s.get("env", {})) - {"GATE_CONFIG"}
             for s in rest}  # fmt: skip
    assert needs["restore the private held-out folder"] == {"HELDOUT_BUNDLE"}
    assert needs["ingest the sample course"] == {"OPENAI_API_KEY"}
    assert all("HELDOUT_BUNDLE" not in v for k, v in needs.items()
               if k != "restore the private held-out folder")  # fmt: skip
    assert check["id"] == "secrets" and "live=false" in check["run"]
    for step in rest:
        assert "steps.secrets.outputs.live == 'true'" in step.get("if", ""), step
    gate = next(s for s in rest if s.get("id") == "gate")
    assert "--no-flip" in gate["run"] and "--yes" in gate["run"]
    upload = next(s for s in rest if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    paths = [p for p in upload["with"]["path"].splitlines() if p.strip()]
    assert {Path(p).name for p in paths} == {"gate.json", "summary.json"}  # never a run folder
    for where in (
        "runs/gate.json",
        "runs/gate-*/gate.json",
        "runs/gate-*/course-ta-heldout-*/gate.json",
    ):  # every eval-mode gate.json
        assert where in paths


def test_offline_gate_passes_on_the_repo_and_fails_on_a_broken_fixture_or_log(
        tmp_path, monkeypatch):  # fmt: skip
    out = tmp_path / "gate" / "offline.json"
    assert G.main(["offline", "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["exit_code"] == 0 and all(v == [] for v in data["problems"].values())
    broken = tmp_path / "evalreg"
    shutil.copytree(G.EVALREG, broken)
    (broken / "summary.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(G, "EVALREG", broken)
    assert G.main(["offline", "--out", str(out)]) == 1
    assert json.loads(out.read_text("utf-8"))["problems"]["fixture"]
    monkeypatch.undo()
    log = tmp_path / "tuning-log.jsonl"
    lines = G.TUNING_LOG.read_text(encoding="utf-8").splitlines()
    log.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")  # reordered
    monkeypatch.setattr(G, "TUNING_LOG", log)
    assert G.main(["offline", "--out", str(out)]) == 1
    assert json.loads(out.read_text("utf-8"))["problems"]["tuning_log"]


# ================== GAT-03: the live pointer flips only on a pass ==================


def _sub(corpus: Corpus, keep: Any) -> Corpus:
    """A corpus of the units `keep` selects, re-indexed (as ingest would build it)."""
    units = [u for u in corpus.units if keep(u)]
    cv = version_of(units)
    return Corpus(schema_version="1.0", cohort=corpus.cohort, corpus_version=cv,
                  units=tuple(u.model_copy(update={"unit_index": i, "corpus_version": cv})
                              for i, u in enumerate(units, start=1)))  # fmt: skip


def _passing(run: str = "r1", corpus_version: str = E.CORPUS.corpus_version) -> Verdict:
    return Verdict(exit_code=0, run=run, corpus_version=corpus_version,
                   values={"grounded": {"c1": 1.0}}, snapshots=["snap-1"])  # fmt: skip


def test_flip_live_writes_only_for_a_pass_on_the_candidate_version(tmp_path):
    _, store = _store(tmp_path)
    assert release.live_state(store) is None and release.serving_corpus(store) is None
    for bad in (dataclasses.replace(_passing(), exit_code=1),
                dataclasses.replace(_passing(), exit_code=4),
                _passing(corpus_version="f" * 64)):  # fmt: skip
        with pytest.raises(ReleaseError):
            release.flip_live(store, bad, rules_hash="rh", grounding_bar=0.5, now=NOW)
        assert not (store.dir / release.LIVE_FILE).exists()
        assert not (store.dir / release.BASELINE_FILE).exists()
    state = release.flip_live(store, _passing(), rules_hash="rh", grounding_bar=0.5, now=NOW)
    assert state["corpus_version"] == E.CORPUS.corpus_version and state["how"] == "gate"
    assert release.live_state(store) == state
    assert release.live_baseline(store) == {"run": "r1", "values": {"grounded": {"c1": 1.0}},
                                            "snapshots": ["snap-1"]}  # fmt: skip
    assert release.serving_corpus(store) == E.CORPUS


def test_removals_apply_at_once_and_additions_wait_for_the_gate(tmp_path):
    _, store = _store(tmp_path)
    release.flip_live(store, _passing(), rules_hash="rh", grounding_bar=0.5, now=NOW)
    removed = next(u for u in E.CORPUS.units if u.session == "s03")
    smaller = _sub(E.CORPUS, lambda u: u is not removed)
    store.save(smaller)  # ingest of a manifest that only removes a file's unit
    serving = release.serving_corpus(store)
    assert serving is not None and serving.corpus_version == smaller.corpus_version
    assert unit_key(removed) not in {unit_key(u) for u in serving.units}  # gone at once
    assert release.apply_removals(store, now=NOW, settings=RH) is True  # live with no gate
    assert (release.live_state(store) or {}).get("how") == "removal"
    assert release.live_corpus(store) == smaller
    # a candidate that adds a unit back and removes another: additions wait, removals do not
    gone = next(u for u in smaller.units if u.session == "s02")
    mixed = _sub(E.CORPUS, lambda u: unit_key(u) != unit_key(gone))
    store.save(mixed)
    assert release.apply_removals(store, now=NOW, settings=RH) is False
    assert release.live_corpus(store) == smaller  # the pointer did not move
    serving = release.serving_corpus(store)
    assert serving is not None
    keys = {unit_key(u) for u in serving.units}
    assert unit_key(gone) not in keys and unit_key(removed) not in keys  # nothing unserved added
    assert len(serving.units) == len(E.CORPUS.units) - 2


def test_release_gate_eval_flips_only_on_exit_0(tmp_path, monkeypatch):
    config, store = _store(tmp_path)

    def fake_eval_live(argv: list[str]) -> int:
        out = Path(argv[argv.index("--out") + 1])
        (out / "course-ta-heldout-x" / "evalkit" / "r1").mkdir(parents=True)
        (out / "course-ta-heldout-x" / "evalkit" / "r1" / "run.json").write_text("{}", "utf-8")
        return 0  # eval_live finished; the verdict decides

    monkeypatch.setattr(eval_live, "main", fake_eval_live)
    for code in (1, 3, 4, 0):
        result = dataclasses.replace(_passing(), exit_code=code)
        monkeypatch.setattr(G, "check_run", lambda *a, result=result, **k: result)
        argv = ["eval", "--config", str(config), "--yes", "--out", str(tmp_path / f"o{code}")]
        assert G.main(argv) == code
        assert (release.live_state(store) is not None) == (code == 0), code
    public = ["eval", "--config", str(config), "--split", "public", "--out", str(tmp_path / "p")]
    assert G.main(public) == 2
    monkeypatch.setattr(eval_live, "main", lambda argv: 3)  # stopped at the cost cap: no gate
    assert G.main(["eval", "--config", str(config), "--out", str(tmp_path / "cap")]) == 3


# ================== GAT-04: drift retirement and the coverage rule ==================


def test_drift_keeps_converts_and_retires_by_gold_unit(tmp_path):
    s03 = [c for c in E.PUBLIC_CASES if c.rule == "B1" and c.gold_units[0].startswith("s03")]
    s01 = [c for c in E.PUBLIC_CASES if c.rule == "B1" and c.gold_units[0].startswith("s01")]
    victim = s01[0].gold_units[0]
    corpus = _sub(E.CORPUS, lambda u: u.session != "s03" and unit_key(u) != victim)
    others = [c for c in E.PUBLIC_CASES if c.rule != "B1"][:5]
    found = release.drift([*s03, *s01, *others], corpus, "new-golden-version")
    hit = {c.id for c in s01 if victim in c.gold_units}
    assert set(found.retired) == hit  # session still taught: the case is retired
    assert set(found.converted) == {c.id for c in s03}  # session gone: absent-topic case
    by_id = {c.id: c for c in found.cases}
    for cid in found.converted:
        c = by_id[cid]
        assert (c.rule, c.expected_status, c.gold_units) == ("B2", "not_covered", ())
    assert set(found.kept) == {c.id for c in s01 if c.id not in hit} | {c.id for c in others}
    assert {c.corpus_version for c in found.cases} == {"new-golden-version"}  # restamped
    assert found.record("cv") == {"corpus_version": "cv", "kept": len(found.kept),
                                  "retired": list(found.retired),
                                  "converted": list(found.converted)}  # fmt: skip
    same = release.drift(E.PUBLIC_CASES, E.CORPUS, E.GOLDEN_VERSION)
    assert same.retired == () and same.converted == () and len(same.cases) == len(E.PUBLIC_CASES)


def test_eval_live_applies_drift_before_building_cases_and_records_ids(tmp_path, monkeypatch):
    from course_assistant.eval import dataset

    victim = next(c for c in E.PUBLIC_CASES if c.rule == "B1")
    corpus = _sub(E.CORPUS, lambda u: unit_key(u) not in victim.gold_units)
    monkeypatch.setattr(dataset, "golden_version_for", lambda c, m, r: E.GOLDEN_VERSION)
    picked = [victim, *[c for c in E.PUBLIC_CASES if c.rule == "B4"][:2]]
    task = eval_live.prepare(tmp_path / "run", picked, corpus)
    record = json.loads((tmp_path / "run" / "drift.json").read_text(encoding="utf-8"))
    assert record["retired"] == [victim.id] and record["kept"] == 2
    rows = [json.loads(x) for x in (task.parent / "cases.jsonl").read_text("utf-8").splitlines()]
    assert victim.id not in {r["id"] for r in rows} and len(rows) == 2


def test_coverage_rule_blocks_go_live(held_run, tmp_path):
    items = release.coverage_of_golden(E.PUBLIC_CASES)
    found = release.coverage_problems(items, E.CORPUS, E.ASSESSMENTS)
    assert found and all("needs" in p for p in found)  # the public half alone is short
    enough = items * 3
    assert not [p for p in release.coverage_problems(enough, E.CORPUS, E.ASSESSMENTS)
                if p.startswith("session")]  # fmt: skip
    # a new session in the corpus with no cases cannot go live
    s01 = [u for u in E.CORPUS.units if u.session == "s01"]
    newer = [u.model_copy(update={"session": "s09", "file_id": f"new-{u.file_id}"}) for u in s01]
    grown = _sub(Corpus.model_construct(units=(*E.CORPUS.units, *newer), cohort="sample"),
                 lambda u: True)  # fmt: skip
    grown_problems = release.coverage_problems(enough, grown, E.ASSESSMENTS)
    assert "session s09 has 0 B1 cases (needs 15)" in grown_problems
    run = fresh(held_run, tmp_path)
    run_items = release.coverage_of_run(R.read_cases(run).values())
    assert ("B3", (), E.first("B3").assessment_id) in run_items
    v = release.verdict(run, inputs(run, coverage_problems=found))
    assert v.exit_code == 1


def test_apply_removals_and_serving_are_idempotent(tmp_path):
    _, store = _store(tmp_path)
    release.flip_live(store, _passing(), rules_hash="rh", grounding_bar=0.5, now=NOW)
    smaller = _sub(E.CORPUS, lambda u: u.session != "s03")
    store.save(smaller)
    first = release.serving_corpus(store)
    assert release.apply_removals(store, now=NOW, settings=RH) is True
    state = release.live_state(store)
    assert release.apply_removals(store, now=NOW, settings=RH) is False  # nothing left to apply
    assert release.live_state(store) == state
    assert release.serving_corpus(store) == first == release.serving_corpus(store)


def test_drift_on_the_unchanged_corpus_changes_nothing_and_repeats_exactly():
    a = release.drift(E.PUBLIC_CASES, E.CORPUS, E.GOLDEN_VERSION)
    b = release.drift(E.PUBLIC_CASES, E.CORPUS, E.GOLDEN_VERSION)
    assert a == b and a.retired == a.converted == ()
    assert [c.model_dump_json() for c in a.cases] == [c.model_dump_json() for c in E.PUBLIC_CASES]


# ============ verifier pass 1 (E2.4): precedence, duplicates, baseline, exposure ============


def test_a_missing_score_is_completion_dependent(held_run, tmp_path):
    """A missing score depends on how far the run got (verifier pass 4): a capped run is 3
    and a pending one 4 first; a finished one is 2."""
    run = fresh(held_run, tmp_path)
    b9 = first_id(run, "B9")
    lines = (run / "records.jsonl").read_text("utf-8").splitlines()
    keep = [x for x in lines if json.loads(x)["case_id"] != first_id(run, "B4")]
    (run / "records.jsonl").write_text("\n".join(keep) + "\n", encoding="utf-8")
    assert release.verdict(run, inputs(run)).exit_code == 3  # capped alone

    def drop(r: dict[str, Any]) -> dict[str, Any]:
        if r["case_id"] == b9:
            del r["task_scores"]["identity"]
        return r

    edit_records(run, drop)
    assert release.verdict(run, inputs(run)).exit_code == 3
    full = fresh(held_run, tmp_path, "full")
    b9 = first_id(full, "B9")
    edit_records(full, drop)
    v = release.verdict(full, inputs(full))
    assert v.exit_code == 2 and v.errors == [f"record {b9} has no identity score"]
    edit_json(full / "summary.json", lambda d: {**d, "judging_status": "pending"})
    assert release.verdict(full, inputs(full)).exit_code == 4


def test_duplicate_records_for_a_case_exit_2(held_run, tmp_path):
    run = fresh(held_run, tmp_path)
    lines = (run / "records.jsonl").read_text("utf-8").splitlines()
    (run / "records.jsonl").write_text("\n".join([*lines, lines[0]]) + "\n", encoding="utf-8")
    v = release.verdict(run, inputs(run))
    assert v.exit_code == 2 and "more than one record" in v.errors[0]


def test_a_baseline_sharing_no_case_with_the_run_exits_2(held_run, tmp_path):
    run = fresh(held_run, tmp_path)
    other = {"grounded": {"ta-b1-900": 1.0, "ta-b1-901": 1.0}}
    v = release.verdict(run, inputs(run, baseline=other))
    assert v.exit_code == 2 and "shares no cases" in v.errors[0]


@pytest.mark.privacy
def test_exposure_scan_reads_every_tracked_text_file_and_skips_binaries_by_content(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    q = "What does the course say about attention heads in long documents?"
    (repo / "notes.cfg").write_text(f"x = {q}\n", encoding="utf-8")  # no allow-listed suffix
    (repo / "blob.json").write_bytes(b"\x00\x01" + q.encode())  # binary by content
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    texts = G.tracked_texts(repo)
    assert len(texts) == 1 and q in texts[0]
    assert release.heldout_exposure({"ta-b1-500": q}, texts) == ["ta-b1-500"]
    assert release.text_of(b"a\x00b") is None and release.text_of(b"plain") == "plain"


def test_check_passes_cost_cap_and_pending_codes_through(held_run, tmp_path, monkeypatch):
    """The 0-4 codes are the script's (make reports any failure as 2; Makefile says so)."""
    config, _ = _store(tmp_path)
    for name, code in (("capped", 3), ("pending", 4)):
        run = fresh(held_run, tmp_path, name)
        private(tmp_path / name, run, monkeypatch)
        if code == 3:
            lines = (run / "records.jsonl").read_text("utf-8").splitlines()
            (run / "records.jsonl").write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        else:
            edit_json(run / "summary.json", lambda d: {**d, "judging_status": "pending"})
        out = tmp_path / f"{name}-gate.json"
        assert G.main(["check", str(run), "--config", str(config), "--out", str(out)]) == code
        assert json.loads(out.read_text(encoding="utf-8"))["exit_code"] == code
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "GNU make reports any failing recipe as 2" in makefile


# ============ verifier pass 2 (E2.4): unexpected errors exit 2; case-only checks first ============


@pytest.mark.parametrize("plant", ["drift", "baseline", "support"])
def test_a_malformed_run_file_exits_2_with_a_fixed_message_in_gate_json(
        held_run, tmp_path, monkeypatch, plant):  # fmt: skip
    """A TypeError on malformed input used to escape as exit 1 (a failed gate)."""
    config, _ = _store(tmp_path)
    run = fresh(held_run, tmp_path / "live" / "evalkit", "r")  # eval_live's layout
    live = tmp_path / "live"
    private(tmp_path, run, monkeypatch)
    argv = ["check", str(run), "--config", str(config)]
    if plant == "drift":
        (live / "drift.json").write_text('{"retired": 5}', encoding="utf-8")
    elif plant == "baseline":
        base = tmp_path / "baseline.json"
        base.write_text('{"values": ["grounded"]}', encoding="utf-8")
        argv += ["--baseline", str(base)]
    else:
        (live / "support-agreement.json").write_text('{"kappa": null}', encoding="utf-8")
    assert G.main(argv) == 2
    data = json.loads((live / "gate.json").read_text(encoding="utf-8"))
    assert data["exit_code"] == 2
    want = {"support": "support-agreement.json is malformed",
            "baseline": "the live baseline is malformed (metric -> case id -> number in [0, 1])",
            }.get(plant, "the gate could not run (TypeError)")  # fmt: skip
    assert data["errors"] == [want]


def _without_rule(run: Path, rule: str) -> None:
    gone = {i for i, c in R.read_cases(run).items() if c.meta["rule"] == rule}
    for name in ("cases.jsonl", "records.jsonl"):
        path = run / name
        rows = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
        key = "id" if name == "cases.jsonl" else "case_id"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows if r[key] not in gone),
                        encoding="utf-8")  # fmt: skip


def test_a_missing_rule_exits_2_whatever_stop_the_run_is_in(held_run, tmp_path):
    """Case-only checks come before the cost-cap and pending stops (precedence 2 > 3 > 4)."""
    for stop in ("none", "cap", "pending"):
        run = fresh(held_run, tmp_path, stop)
        _without_rule(run, "B9")
        if stop == "cap":
            lines = (run / "records.jsonl").read_text("utf-8").splitlines()
            (run / "records.jsonl").write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        if stop == "pending":
            edit_json(run / "summary.json", lambda d: {**d, "judging_status": "pending"})
        v = release.verdict(run, inputs(run))
        assert v.exit_code == 2, stop
        assert v.errors == ["no case for the identity gate"], stop


@pytest.mark.privacy
def test_exposure_scan_normalises_json_escapes_case_and_whitespace():
    q = 'What does the course say about "attention" heads in long documents?'
    escaped = json.dumps({"q": q.upper().replace(" ", "\n  ")})  # escaped, cased, re-wrapped
    unicode_escaped = json.dumps({"q": q.replace("a", "\\u0061")}).replace("\\\\u", "\\u")
    for text in (escaped, unicode_escaped, f"  {q.lower()}  "):
        assert release.heldout_exposure({"ta-b1-500": q}, [text]) == ["ta-b1-500"], text
    assert release.heldout_exposure({"ta-b1-500": q}, ["attention heads"]) == []


# ============ verifier pass 3 (E2.4): every input-only check before the stops ============


def _edit_cases(run: Path, fn: Any) -> None:
    path = run / "cases.jsonl"
    rows = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
    path.write_text("".join(json.dumps(r) + "\n" for r in fn(rows)), encoding="utf-8")


def _strip_b1_citations(r: dict[str, Any]) -> dict[str, Any]:
    if r["case_id"].startswith("ta-b1-") and r["completion"]["answer"]:
        r["completion"]["answer"]["citations"] = []
    return r


def _swap_question(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["question"] = "A different question under a held-out id?"
    return rows


def _other_context(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["context"] = ["other text", *rows[0]["context"][1:]]
    return rows


def _other_source(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["source"] = "local/elsewhere"
    return rows


def _no_gold_indices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in rows:
        r["meta"].pop("gold_indices", None)
    return rows


def _two_versions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["meta"]["corpus_version"] = "f" * 64
    return rows


def _unknown_record(run: Path) -> None:
    lines = (run / "records.jsonl").read_text("utf-8").splitlines()
    extra = json.loads(lines[0])
    extra["case_id"] = "ta-b1-999"
    (run / "records.jsonl").write_text("\n".join([*lines, json.dumps(extra)]) + "\n", "utf-8")


def _double_record(run: Path) -> None:
    lines = (run / "records.jsonl").read_text("utf-8").splitlines()
    (run / "records.jsonl").write_text("\n".join([*lines, lines[0]]) + "\n", "utf-8")


def _double_case(run: Path) -> None:
    lines = (run / "cases.jsonl").read_text("utf-8").splitlines()
    (run / "cases.jsonl").write_text("\n".join([*lines, lines[0]]) + "\n", "utf-8")


def _missing_score(run: Path) -> None:
    b9 = first_id(run, "B9")

    def drop(r: dict[str, Any]) -> dict[str, Any]:
        if r["case_id"] == b9:
            del r["task_scores"]["identity"]
        return r

    edit_records(run, drop)


@pytest.mark.safety
def test_every_input_only_check_exits_2_whether_complete_capped_or_pending(held_run, tmp_path):
    """Input-only checks (cases file, MANIFEST ids, content including context and source,
    duplicates, records for unknown cases, what the cases alone decide) exit 2 under every
    stop; completion-dependent ones (B1 citations, scores) give 3 capped and 4 pending first."""
    plants: dict[str, tuple[Any, str]] = {
        "extra MANIFEST id": (None, "not the held-out split"),
        "duplicate case line": (_double_case, "more than once"),
        "swapped text": (lambda run: _edit_cases(run, _swap_question), "differ from the MANIFEST"),
        "two corpus versions": (lambda run: _edit_cases(run, _two_versions), "more than one"),
        "missing rule": (lambda run: _without_rule(run, "B9"), "no case for the identity gate"),
        "no gold units": (lambda run: _edit_cases(run, _no_gold_indices), "differ from"),
        "record for an unknown case": (_unknown_record, "for cases not in the run"),
        "duplicate record": (_double_record, "more than one record"),
        "changed context": (lambda run: _edit_cases(run, _other_context), "differ from"),
        "changed source": (lambda run: _edit_cases(run, _other_source), "differ from"),
        "baseline sharing no case": ("baseline", "shares no cases"),
        "malformed baseline": ("bad-baseline", "malformed"),
    }  # fmt: skip
    # completion-dependent checks: what the records hold so far; the stops come first
    later: dict[str, tuple[Any, str]] = {
        "no B1 citations": (lambda run: edit_records(run, _strip_b1_citations),
                            "no case for the citation_validity gate"),
        "missing score": (_missing_score, "has no identity score"),
    }  # fmt: skip
    codes = {"complete": 2, "capped": 3, "pending": 4}
    for name, (plant, message) in [*plants.items(), *later.items()]:
        for stop in ("complete", "capped", "pending"):
            run = fresh(held_run, tmp_path, f"{len(name)}-{name[:6]}-{stop}".replace(" ", "_"))
            expected = rows_of(run)  # what the MANIFEST-checked held-out file holds
            baseline: Any = None
            if plant == "baseline":
                baseline = {"grounded": {"ta-b1-900": 1.0, "ta-b1-901": 1.0}}
            elif plant == "bad-baseline":
                baseline = {"grounded": ["ta-b1-001"]}
            elif plant is not None:
                plant(run)
            if name in ("missing rule", "two corpus versions"):
                expected = rows_of(run)  # the held-out file agrees, so this check is reached
            if stop == "capped":
                b4 = first_id(run, "B4")
                lines = (run / "records.jsonl").read_text("utf-8").splitlines()
                keep = [x for x in lines if json.loads(x)["case_id"] != b4]
                (run / "records.jsonl").write_text("\n".join(keep) + "\n", encoding="utf-8")
            if stop == "pending":
                edit_json(run / "summary.json", lambda d: {**d, "judging_status": "pending"})
            held = set(expected) | ({"ta-b1-999"} if name == "extra MANIFEST id" else set())
            given = inputs(run, heldout_ids=frozenset(held), expected_cases=expected,
                           baseline=baseline)  # fmt: skip
            v = release.verdict(run, given)
            want = 2 if name in plants else codes[stop]
            assert v.exit_code == want, (name, stop, v.exit_code, v.errors, v.notes)
            if want == 2:
                assert any(message in e for e in v.errors), (name, stop, v.errors)


@pytest.mark.safety
def test_swapped_text_under_heldout_ids_exits_2_through_the_cli(held_run, tmp_path, monkeypatch):
    """The run's cases are checked against the held-out file that matched the MANIFEST."""
    config, _ = _store(tmp_path)
    run = fresh(held_run, tmp_path)
    private(tmp_path, run, monkeypatch)
    _edit_cases(run, _swap_question)
    out = tmp_path / "gate.json"
    assert G.main(["check", str(run), "--config", str(config), "--out", str(out)]) == 2
    data = json.loads(out.read_text(encoding="utf-8"))
    assert any("differ from the MANIFEST" in e for e in data["errors"])
    assert "A different question" not in out.read_text(encoding="utf-8")  # ids only
    monkeypatch.setattr(G, "HELDOUT_DIR", tmp_path / "absent")  # no private file: cannot check
    assert G.main(["check", str(fresh(held_run, tmp_path, "b")), "--config", str(config),
                   "--out", str(out)]) == 2  # fmt: skip
    assert "the private held-out file is needed" in out.read_text(encoding="utf-8")


def test_a_crash_in_eval_mode_writes_gate_json(tmp_path, monkeypatch):
    config, _ = _store(tmp_path)

    def fake_eval_live(argv: list[str]) -> int:
        out = Path(argv[argv.index("--out") + 1])
        (out / "course-ta-heldout-x" / "evalkit" / "r1").mkdir(parents=True)
        (out / "course-ta-heldout-x" / "evalkit" / "r1" / "run.json").write_text("{}", "utf-8")
        return 0

    def boom(*a: Any, **k: Any) -> Any:
        raise TypeError("unexpected input")

    monkeypatch.setattr(eval_live, "main", fake_eval_live)
    monkeypatch.setattr(G, "check_run", boom)
    assert G.main(["eval", "--config", str(config), "--yes", "--out", str(tmp_path / "o")]) == 2
    (gate,) = (tmp_path / "o").glob("gate-*/course-ta-heldout-x/gate.json")
    data = json.loads(gate.read_text(encoding="utf-8"))
    assert data["exit_code"] == 2 and data["errors"] == ["the gate could not run (TypeError)"]


def _fake_live(code: int, folders: int = 1) -> Any:
    def fake(argv: list[str]) -> int:
        out = Path(argv[argv.index("--out") + 1])
        for i in range(folders):
            d = out / "course-ta-heldout-x" / "evalkit" / f"r{i}"
            d.mkdir(parents=True)
            (d / "run.json").write_text("{}", encoding="utf-8")
        return code

    return fake


def test_every_eval_mode_exit_writes_gate_json(tmp_path, monkeypatch):
    """Verifier pass 4: eval_live's 2 or 3 returned before any gate.json or check."""
    config, store = _store(tmp_path)

    def gate_files(root: Path) -> list[dict[str, Any]]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.rglob("gate.json"))]

    def run(name: str, *extra: str) -> tuple[int, list[dict[str, Any]]]:
        out = tmp_path / name
        code = G.main(["eval", "--config", str(config), "--yes", "--out", str(out), *extra])
        return code, gate_files(out)

    seen: list[Path] = []
    verdict_code = {"code": 1}

    def fake_check(run_dir: Path, **k: Any) -> Verdict:
        seen.append(run_dir)
        return dataclasses.replace(_passing(), exit_code=verdict_code["code"], run=run_dir.name)

    monkeypatch.setattr(G, "check_run", fake_check)
    code, files = run("split", "--split", "public")
    assert code == 2 and [f["exit_code"] for f in files] == [2]
    for live_code in (2, 3):  # refused or capped before EvalKit wrote a run folder
        monkeypatch.setattr(eval_live, "main", lambda argv, c=live_code: c)
        code, files = run(f"early{live_code}")
        assert code == live_code and [f["exit_code"] for f in files] == [live_code]
    monkeypatch.setattr(eval_live, "main", _fake_live(3))  # capped with a run folder: checked
    code, files = run("capped")
    assert code == 3 and [f["exit_code"] for f in files] == [3] and seen[-1].name == "r0"
    verdict_code["code"] = 2  # an input error in that run wins over the cap
    code, files = run("capped-bad")
    assert code == 2 and [f["exit_code"] for f in files] == [2]
    verdict_code["code"] = 0
    monkeypatch.setattr(eval_live, "main", _fake_live(2))  # failed after writing the run
    code, files = run("late2", "--no-flip")
    assert code == 2 and [f["exit_code"] for f in files] == [2]
    monkeypatch.setattr(eval_live, "main", _fake_live(0, folders=2))
    code, files = run("two")
    assert code == 2 and [f["exit_code"] for f in files] == [2]
    for vc in (1, 4, 0):
        verdict_code["code"] = vc
        monkeypatch.setattr(eval_live, "main", _fake_live(vc))
        code, files = run(f"verdict{vc}", "--no-flip")
        assert code == vc and [f["exit_code"] for f in files] == [vc]
    # removal-only candidate with every gated setting unchanged: live at once, gate.json says so
    _flip_with_settings_now(store)
    store.save(_sub(E.CORPUS, lambda u: u.session != "s03"))
    code, files = run("removal")
    assert code == 0 and [f["exit_code"] for f in files] == [0]
    assert "only removes units" in files[0]["notes"][0]


# ========= verifier pass 5 (E2.4): every input in exactly one class; eval_live's code =========


@pytest.mark.safety
def test_verdict_reads_only_inputs_the_check_order_lists(held_run, tmp_path, monkeypatch):
    """Each run file and GateInputs field the verdict touches must be listed, in one class,
    in release.INPUT_CLASSES and named in the spec's "Check order"."""
    spec = (ROOT / "specs" / "E2.4-release-gate" / "spec.md").read_text(encoding="utf-8")
    order = spec[spec.index("**Check order") : spec.index("**D-54 held-out exposure**")]
    classes = {"input-only", "completion-dependent", "gate condition", "parameter"}
    assert set(release.INPUT_CLASSES.values()) <= classes
    for name, cls in release.INPUT_CLASSES.items():
        assert f"`{name.split()[0]}`" in order, name  # listed in the written rule
        if cls != "parameter":
            assert cls in order
    fields = {f.name for f in dataclasses.fields(GateInputs)}
    assert fields <= set(release.INPUT_CLASSES)  # every field has a class

    run = fresh(held_run, tmp_path / "live" / "evalkit", "r")
    (tmp_path / "live" / "support-agreement.json").write_text(
        json.dumps({"kappa": {"point": 0.8}}), encoding="utf-8")  # fmt: skip
    import builtins
    import io

    files: set[str] = set()
    real_read, real_open, real_path_open = Path.read_text, builtins.open, Path.open
    real_load = json.load

    def spy(self: Path, *a: Any, **k: Any) -> str:
        files.add(self.name)
        return real_read(self, *a, **k)

    def spy_open(file: Any, *a: Any, **k: Any) -> Any:
        if isinstance(file, str | Path):
            files.add(Path(file).name)
        return real_open(file, *a, **k)

    def spy_path_open(self: Path, *a: Any, **k: Any) -> Any:
        files.add(self.name)
        return real_path_open(self, *a, **k)

    def spy_load(fp: Any, *a: Any, **k: Any) -> Any:
        files.add(Path(str(getattr(fp, "name", "?"))).name)
        return real_load(fp, *a, **k)

    class Spy:
        def __init__(self, inner: GateInputs) -> None:
            self._inner, self.used = inner, set()

        def __getattr__(self, name: str) -> Any:
            self.used.add(name)
            return getattr(self._inner, name)

    monkeypatch.setattr(Path, "read_text", spy)
    monkeypatch.setattr(Path, "open", spy_path_open)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(json, "load", spy_load)
    spied = Spy(inputs(run, baseline=rows_baseline(run)))
    v = release.verdict(run, spied)  # type: ignore[arg-type]
    monkeypatch.undo()
    assert v.exit_code == 0, (v.errors, v.failures)
    listed_files = {k.split()[0] for k in release.INPUT_CLASSES}
    assert files <= listed_files, files - listed_files
    assert spied.used <= set(release.INPUT_CLASSES), spied.used - set(release.INPUT_CLASSES)
    assert {"cases.jsonl", "records.jsonl", "summary.json", "support-agreement.json"} <= files


def rows_baseline(run: Path) -> dict[str, dict[str, float]]:
    """A baseline with every gated metric over the run's applicable cases."""
    cases = R.read_cases(run)
    return {m: {i: 1.0 for i, c in cases.items() if c.meta["rule"] in rules}
            for m, rules in release.METRIC_RULES.items()}  # fmt: skip


def test_any_nonzero_eval_live_exit_blocks_the_flip(tmp_path, monkeypatch):
    """Verifier pass 5: eval_live's 1 or 4 was ignored, so a verdict of 0 still flipped."""
    config, store = _store(tmp_path)
    monkeypatch.setattr(
        G, "check_run", lambda run_dir, **k: dataclasses.replace(_passing(), run=run_dir.name)
    )
    for live_code, want in ((1, 1), (4, 4), (3, 3), (2, 2), (0, 0)):
        monkeypatch.setattr(eval_live, "main", _fake_live(live_code))
        out = tmp_path / f"o{live_code}"
        assert G.main(["eval", "--config", str(config), "--yes", "--out", str(out)]) == want
        (gate,) = out.rglob("gate.json")
        assert json.loads(gate.read_text(encoding="utf-8"))["exit_code"] == want
        assert (release.live_state(store) is not None) == (live_code == 0), live_code
    # a worse verdict still wins over a milder eval_live code (2 > 3 > 4 > 1)
    monkeypatch.setattr(G, "check_run", lambda run_dir, **k: dataclasses.replace(
        _passing(), exit_code=2, run=run_dir.name))  # fmt: skip
    monkeypatch.setattr(eval_live, "main", _fake_live(4))
    assert G.main(["eval", "--config", str(config), "--yes", "--out", str(tmp_path / "w")]) == 2


def test_an_unparsable_eval_command_line_writes_gate_json(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "ROOT", tmp_path / "root")  # eval's default runs/gate.json
    out = tmp_path / "o"
    assert G.main(["eval", "--out", str(out)]) == 2  # no --config
    data = json.loads((out / "gate.json").read_text(encoding="utf-8"))
    assert data["exit_code"] == 2 and data["errors"] == ["the command line could not be parsed"]
    (out / "gate.json").unlink()
    assert G.main(["eval", "--config", "c.yaml", "--max-cost", "x", "--out", str(out)]) == 2
    assert json.loads((out / "gate.json").read_text(encoding="utf-8"))["exit_code"] == 2
    check_out = tmp_path / "check-gate.json"
    assert G.main(["check", "--out", str(check_out)]) == 2  # no RUN, no --config
    assert json.loads(check_out.read_text(encoding="utf-8"))["exit_code"] == 2

    assert (tmp_path / "root" / "runs" / "gate.json").is_file()  # the default, always
    # --out=X, an unambiguous abbreviation, and a global flag before the subcommand
    shapes = ((["eval", f"--out={tmp_path}/o1"], tmp_path / "o1"),
              (["eval", "--ou", f"{tmp_path}/o2"], tmp_path / "o2"),
              (["eval", "--o", f"{tmp_path}/o3", "--config"], tmp_path / "o3"))  # fmt: skip
    for argv, where in shapes:
        assert G.main(argv) == 2
        assert json.loads((where / "gate.json").read_text(encoding="utf-8"))["exit_code"] == 2
    (tmp_path / "root" / "runs" / "gate.json").unlink()
    assert G.main(["--json", "eval"]) == 2
    assert (tmp_path / "root" / "runs" / "gate.json").is_file()


# ============ verifier pass 6 (E2.4): "no baseline" is explicit; summary shape ============


@pytest.mark.safety
def test_a_baseline_given_or_expected_but_malformed_exits_2_never_a_first_release(
        held_run, tmp_path, monkeypatch):  # fmt: skip
    config, store = _store(tmp_path)
    good = {"grounded": {i: 1.0 for i in ids(held_run) if i.startswith("ta-b1-")}}
    shapes: dict[str, Any] = {
        "raw mapping": good, "empty": {}, "values null": {"values": None},
        "typo": {"vals": good}, "snapshots a string": {"values": good, "snapshots": "abc"},
        "a list": [good], "not json": "{oops",
    }  # fmt: skip
    for name, data in shapes.items():
        run = fresh(held_run, tmp_path / name.replace(" ", "_"), "r")
        private(tmp_path / name.replace(" ", "_"), run, monkeypatch)
        b = tmp_path / f"{name.replace(' ', '_')}.json"
        b.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        out = tmp_path / f"{name.replace(' ', '_')}-gate.json"
        argv = ["check", str(run), "--config", str(config), "--baseline", str(b), "--out", str(out)]
        assert G.main(argv) == 2, name
        gate = json.loads(out.read_text(encoding="utf-8"))
        assert gate["exit_code"] == 2 and "baseline" in gate["errors"][0], (name, gate["errors"])
        assert "first release" not in " ".join(gate["notes"]), name
    run = fresh(held_run, tmp_path / "missing", "r")
    private(tmp_path / "missing", run, monkeypatch)
    out = tmp_path / "missing-gate.json"
    argv = ["check", str(run), "--config", str(config), "--out", str(out),
            "--baseline", str(tmp_path / "nowhere.json")]  # fmt: skip
    assert G.main(argv) == 2
    assert "is missing" in json.loads(out.read_text(encoding="utf-8"))["errors"][0]
    # with no --baseline: a first release only while nothing is live
    plain = ["check", str(run), "--config", str(config), "--out", str(out)]
    assert G.main(plain) == 1  # (uncalibrated) but the regression check was not refused
    assert "no live baseline: first release, nothing to regress against" in json.loads(
        out.read_text(encoding="utf-8"))["notes"]  # fmt: skip
    release.flip_live(store, _passing(), rules_hash="rh", grounding_bar=0.5, now=NOW)
    (store.dir / release.BASELINE_FILE).unlink()  # LIVE.json without its baseline
    assert G.main(plain) == 2
    assert "live-baseline.json) is missing" in json.loads(out.read_text("utf-8"))["errors"][0]
    (store.dir / release.LIVE_FILE).unlink()  # a baseline file with no LIVE.json: inconsistent
    (store.dir / release.BASELINE_FILE).write_text('{"values": null}', encoding="utf-8")
    assert G.main(plain) == 2
    assert "without LIVE.json" in json.loads(out.read_text("utf-8"))["errors"][0]
    release.flip_live(store, _passing(), rules_hash="rh", grounding_bar=0.5, now=NOW)
    for corrupt in ('{"values": null}', "{oops", "[]"):
        (store.dir / release.BASELINE_FILE).write_text(corrupt, encoding="utf-8")
        assert G.main(plain) == 2, corrupt
    (store.dir / release.LIVE_FILE).write_text("{oops", encoding="utf-8")
    assert G.main(plain) == 2
    assert "LIVE.json is unreadable" in json.loads(out.read_text("utf-8"))["errors"][0]


def test_a_corrupt_live_baseline_never_flips_in_eval_mode(tmp_path, monkeypatch):
    config, store = _store(tmp_path)
    release.flip_live(store, _passing(run="old"), rules_hash="rh", grounding_bar=0.5, now=NOW)
    before = release.live_state(store)
    (store.dir / release.BASELINE_FILE).write_text('{"values": null}', encoding="utf-8")
    seen: list[Any] = []

    def check(run_dir: Path, **k: Any) -> Verdict:
        _, _, error = G.load_baseline(k["baseline_path"], store)
        seen.append(error)
        return dataclasses.replace(_passing(), exit_code=2 if error else 0, run=run_dir.name)

    monkeypatch.setattr(G, "check_run", check)
    monkeypatch.setattr(eval_live, "main", _fake_live(0))
    assert G.main(["eval", "--config", str(config), "--yes", "--out", str(tmp_path / "o")]) == 2
    assert seen and "malformed" in seen[0]
    assert release.live_state(store) == before  # nothing flipped


@pytest.mark.parametrize("body", ["{not json", "[1]"])
def test_a_corrupt_summary_exits_2_at_any_stop(held_run, tmp_path, body):
    """summary.json's shape is input-only; its content (judging status) is completion-dependent."""
    for stop in ("complete", "capped", "pending"):
        run = fresh(held_run, tmp_path, f"{stop}-{len(body)}")
        if stop == "capped":
            lines = (run / "records.jsonl").read_text("utf-8").splitlines()
            (run / "records.jsonl").write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        (run / "summary.json").write_text(body, encoding="utf-8")
        assert release.verdict(run, inputs(run)).exit_code == 2, stop
    capped = fresh(held_run, tmp_path, "null-status")
    lines = (capped / "records.jsonl").read_text("utf-8").splitlines()
    (capped / "records.jsonl").write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    (capped / "summary.json").write_text(json.dumps({"judging_status": None}), encoding="utf-8")
    assert release.verdict(capped, inputs(capped)).exit_code == 3  # content: after the stop


# ============ verifier pass 7 (E2.4): no regression comparison is ever skipped ============


@pytest.mark.safety
def test_an_accepted_baseline_compares_every_gated_metric(held_run, tmp_path):
    """A baseline naming an unknown metric, lacking a gated one, or sharing fewer than
    MIN_PAIRED applicable cases for any metric exits 2 before the stops; any baseline that is
    accepted compares exactly the gated metrics."""
    run = fresh(held_run, tmp_path)
    full = rows_baseline(run)
    some_id = sorted(ids(run))[0]
    refused = {
        "typo metric": {**full, "grounded_rate": full["grounded"]},
        "only a typo": {"grounded_rate": full["grounded"]},
        "lacks a metric": {m: v for m, v in full.items() if m != "identity"},
        "one shared case": {**full, "integrity": dict(list(full["integrity"].items())[:1])},
        "wrong cases for a metric": {**full, "grounded": {i: 1.0 for i in full["identity"]}},
        "single metric": {"integrity": {some_id: 1.0}},
    }  # fmt: skip
    for name, base in refused.items():
        for stop in ("complete", "capped", "pending"):
            r = fresh(held_run, tmp_path, f"{name[:8]}-{stop}".replace(" ", "_"))
            if stop == "capped":
                lines = (r / "records.jsonl").read_text("utf-8").splitlines()
                (r / "records.jsonl").write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            if stop == "pending":
                edit_json(r / "summary.json", lambda d: {**d, "judging_status": "pending"})
            v = release.verdict(r, inputs(r, baseline=base))
            assert v.exit_code == 2, (name, stop, v.errors)
            assert "baseline" in v.errors[0] and not v.regressions, (name, v.errors)
    refused_outside = {m: {**v, "ta-b1-999": 1.0} for m, v in full.items()}
    v = release.verdict(run, inputs(run, baseline=refused_outside))
    assert v.exit_code == 2 and "outside the held-out MANIFEST" in v.errors[0]
    accepted = [full]
    for base in accepted:
        v = release.verdict(run, inputs(run, baseline=base))
        assert v.exit_code == 0, v.errors
        assert sorted(r.metric for r in v.regressions) == sorted(release.GATED_METRICS)
    values = release.verdict(run, inputs(run)).values
    with pytest.raises(ReleaseError, match="cannot be compared"):
        release.regressions(values, {**full, "integrity": {some_id: 1.0}})
    with pytest.raises(ReleaseError, match="unknown metric"):
        release.regressions(values, {**full, "bogus": {some_id: 1.0}})


def test_a_dangling_live_pointer_is_an_error_not_a_first_release(tmp_path):
    _, store = _store(tmp_path)
    for name in (release.LIVE_FILE, release.BASELINE_FILE):
        link = store.dir / name
        link.symlink_to(tmp_path / "nowhere.json")  # dangling
        _, _, error = G.load_baseline(None, store)
        assert error is not None, name
        link.unlink()
    loop = store.dir / release.LIVE_FILE
    loop.symlink_to(loop)  # a looping symlink
    _, _, error = G.load_baseline(None, store)
    assert error is not None and "LIVE.json" in error
    loop.unlink()
    assert G.load_baseline(None, store) == (None, [], None)  # truly nothing live


# ========= verifier pass 8 (E2.4): every number the gate reads checked to its domain =========


def _set_completion(field: str, value: Any) -> Any:
    def plant(run: Path) -> None:
        b1 = first_id(run, "B1")

        def fn(r: dict[str, Any]) -> dict[str, Any]:
            if r["case_id"] == b1:
                r["completion"][field] = value
            return r

        edit_records(run, fn)

    return plant


def _set_task_score(name: str, value: Any) -> Any:
    def plant(run: Path) -> None:
        b1 = first_id(run, "B1") if name == "locator_correct" else first_id(run, "B3")

        def fn(r: dict[str, Any]) -> dict[str, Any]:
            if r["case_id"] == b1:
                r["task_scores"][name]["value"] = value
            return r

        edit_records(run, fn)

    return plant


def _set_kappa(value: Any) -> Any:
    def plant(run: Path) -> None:
        (run.parent.parent / "support-agreement.json").write_text(
            json.dumps({"kappa": {"point": value}}), encoding="utf-8")  # fmt: skip

    return plant


@pytest.mark.safety
def test_every_numeric_input_out_of_its_domain_exits_2_as_its_class_says(held_run, tmp_path):
    """One plant per numeric input in release.INPUT_DOMAINS: input-only inputs and the seed
    exit 2 at every stop; completion-dependent ones give 3 capped and 4 pending first."""
    input_only, completion = (2, 2, 2), (2, 3, 4)
    huge = 10**400

    def base_all(value: Any) -> Any:
        return lambda run: {m: {i: value for i in vals} for m, vals in rows_baseline(run).items()}

    def base_one(value: Any) -> Any:
        def make(run: Path) -> dict[str, dict[str, float]]:
            b = rows_baseline(run)
            first = sorted(b["grounded"])[0]
            b["grounded"][first] = value
            return b

        return make

    table: list[tuple[str, str, Any, Any, tuple[int, int, int]]] = [
        # (name, INPUT_DOMAINS key, run plant, inputs override, codes for complete/capped/pending)
        ("baseline -1e6 everywhere", "baseline", None, {"baseline": base_all(-1e6)}, input_only),
        ("baseline 1.5", "baseline", None, {"baseline": base_one(1.5)}, input_only),
        ("baseline 10**400", "baseline", None, {"baseline": base_one(huge)}, input_only),
        ("latency 10**10", "records.jsonl presence", _set_completion("latency_ms", 10**10), {},
         input_only),
        ("latency 10**400", "records.jsonl presence", _set_completion("latency_ms", huge), {},
         input_only),
        ("cost 1e6", "records.jsonl presence", _set_completion("cost_usd", 1e6), {}, input_only),
        ("locator 1e9", "records.jsonl scores", _set_task_score("locator_correct", 1e9), {},
         completion),
        ("integrity -0.5", "records.jsonl scores", _set_task_score("integrity", -0.5), {},
         completion),
        ("kappa 5", "support-agreement.json", _set_kappa(5), {}, completion),
        ("kappa 10**400", "support-agreement.json", _set_kappa(huge), {}, completion),
        ("seed -1", "seed", None, {"seed": -1}, input_only),
        ("seed 10**400", "seed", None, {"seed": huge}, input_only),
    ]  # fmt: skip
    assert {key for _, key, *_ in table} == set(release.INPUT_DOMAINS)  # every numeric input
    for name, _, plant, override, codes in table:
        for stop, want in zip(("complete", "capped", "pending"), codes, strict=True):
            slug = "".join(ch if ch.isalnum() else "_" for ch in name)
            run = fresh(held_run, tmp_path / f"{slug}-{stop}" / "evalkit", "r")
            if plant is not None:
                plant(run)
            if stop == "capped":
                b4 = first_id(run, "B4")
                lines = (run / "records.jsonl").read_text("utf-8").splitlines()
                keep = [x for x in lines if json.loads(x)["case_id"] != b4]
                (run / "records.jsonl").write_text("\n".join(keep) + "\n", encoding="utf-8")
            if stop == "pending":
                edit_json(run / "summary.json", lambda d: {**d, "judging_status": "pending"})
            given = {k: (val(run) if callable(val) else val) for k, val in override.items()}
            v = release.verdict(run, inputs(run, **given))  # never raises, even for 10**400
            assert v.exit_code == want, (name, stop, v.exit_code, v.errors, v.notes)
    # an impossible baseline through the command line, as the verifier planted it
    assert release.baseline_problem(base_all(-1e6)(held_run), R.read_cases(held_run))


# ========= verifier pass 9 (E2.4): the removal shortcut needs every setting unchanged =========


def _flip_with_settings_now(store: CorpusStore, **changed: Any) -> None:
    """Flip as a gate on today's code and config would, with `changed` settings overridden
    (as if the live run had used them)."""
    from course_assistant.answer.ask import Assistant

    settings = {**G.gated_settings(E.CONFIG), **changed}
    release.flip_live(store, _passing(), rules_hash=settings["rules_hash"],
                      grounding_bar=settings["grounding_bar"], now=NOW,
                      settings=settings)  # fmt: skip
    assert Assistant.rules_hash() == G.gated_settings(E.CONFIG)["rules_hash"]


@pytest.mark.safety
def test_a_removal_plus_any_gated_setting_change_runs_the_full_gate(tmp_path, monkeypatch):
    """Verifier pass 9: a removal combined with a rules, bar or model change skipped the gate
    and flipped LIVE.json with the old rules hash."""
    changes: dict[str, dict[str, Any]] = {
        "rules hash": {"rules_hash": "an-older-rules-hash"},
        "grounding bar": {"grounding_bar": 0.99},
        "answer model": {"answer_model": "an-older-answer-model"},
        "check model": {"check_model": "an-older-check-model"},
        "reasoning effort": {"reasoning_effort": {"x": "high"}},
        # pass 10: any other config field (retrieval breadth, window, vector store) or the
        # assessments file, through their hashes
        "other config field": {"config_sha256": "0" * 64},
        "assessments file": {"assessments_sha256": "0" * 64},
        "no recorded settings": {},  # a LIVE.json written before settings were recorded
    }
    for name, changed in changes.items():
        root = tmp_path / name.replace(" ", "_")
        config, store = _store(root)
        if name == "no recorded settings":
            release.flip_live(store, _passing(), rules_hash="rh", grounding_bar=0.5, now=NOW)
            state = release.live_state(store) or {}
            state.pop("settings")
            (store.dir / release.LIVE_FILE).write_text(json.dumps(state), encoding="utf-8")
        else:
            _flip_with_settings_now(store, **changed)
        before = release.live_state(store)
        store.save(_sub(E.CORPUS, lambda u: u.session != "s03"))  # a removal-only corpus
        called: list[int] = []
        monkeypatch.setattr(eval_live, "main", lambda argv, c=called: c.append(1) or 2)
        code = G.main(["eval", "--config", str(config), "--yes", "--out", str(root / "o")])
        assert called, name  # the full gate ran
        assert code == 2, name  # (the fake eval_live refused)
        assert release.live_state(store) == before, name  # nothing flipped
    # unchanged settings: the shortcut, with the recorded settings kept
    config, store = _store(tmp_path / "unchanged")
    _flip_with_settings_now(store)
    store.save(_sub(E.CORPUS, lambda u: u.session != "s03"))
    monkeypatch.setattr(eval_live, "main", lambda argv: pytest.fail("no gate needed"))
    assert G.main(["eval", "--config", str(config), "--yes", "--out", str(tmp_path / "u")]) == 0
    state = release.live_state(store) or {}
    assert state["how"] == "removal" and state["settings"] == G.gated_settings(E.CONFIG)


def test_a_live_baseline_must_match_the_live_run_and_cover_what_it_knows(held_run, tmp_path):
    _, store = _store(tmp_path)
    run = fresh(held_run, tmp_path)
    full = rows_baseline(run)
    release.flip_live(store, dataclasses.replace(_passing(run="live-run"), values=full),
                      rules_hash="rh", grounding_bar=0.5, now=NOW)  # fmt: skip
    assert G.load_baseline(None, store)[2] is None
    data = json.loads((store.dir / release.BASELINE_FILE).read_text(encoding="utf-8"))
    (store.dir / release.BASELINE_FILE).write_text(json.dumps({**data, "run": "other-run"}),
                                                    encoding="utf-8")  # fmt: skip
    assert "not the live run" in (G.load_baseline(None, store)[2] or "")
    # coverage: a live baseline missing a known applicable case for a full-coverage metric
    assert len(full["handover"]) == 3
    b9 = sorted(full["handover"])[0]  # a handover case, still known through another metric
    gap = {**full, "handover": {i: x for i, x in full["handover"].items() if i != b9},
           "grounded": {**full["grounded"], b9: 1.0}}  # fmt: skip
    v = release.verdict(run, inputs(run, baseline=gap, baseline_from_live=True))
    assert v.exit_code == 2 and "does not hold every applicable run case" in v.errors[0]
    # a hand-made --baseline keeps only the 2-case floor (spec: a known limit of check mode)
    v = release.verdict(run, inputs(run, baseline=gap, baseline_from_live=False))
    assert v.exit_code != 2 or "does not hold" not in " ".join(v.errors)
    # drift-converted cases are exempt
    v = release.verdict(run, inputs(run, baseline=gap, baseline_from_live=True,
                                    converted=frozenset({b9})))  # fmt: skip
    assert "does not hold" not in " ".join(v.errors)
