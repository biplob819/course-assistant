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
RULES = ("B1", "B1", "B1", "B2", "B3", "B4", "B7", "B8", "B9", "B10")
CALIBRATED = {"groundedness": "calibrated", "integrity": "calibrated"}


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


def inputs(run: Path, **kw: Any) -> GateInputs:
    base: dict[str, Any] = {"heldout_ids": ids(run), "calibration": CALIBRATED,
                            "fixture_problems": [], "log_problems": [],
                            "coverage_problems": [], "exposure": []}  # fmt: skip
    return GateInputs(**{**base, **kw})


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
    r["completion"]["latency_ms"] = 9000
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
    found = release.regressions({"grounded": worse}, {"grounded": many})
    assert len(found) == 1 and found[0].separated and found[0].diff < 0
    assert not release.regressions({"grounded": many}, {"grounded": many})[0].separated
    slight = {**many, "c0": 0.0}
    assert not release.regressions({"grounded": slight}, {"grounded": many})[0].separated
    better = release.regressions({"grounded": many}, {"grounded": worse})
    assert not better[0].separated  # an improvement is never a regression
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
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({"cases": {i: "x" for i in ids(run)}}), encoding="utf-8")
    monkeypatch.setattr(G, "HELDOUT_MANIFEST", manifest)
    monkeypatch.setattr(G, "HELDOUT_DIR", tmp_path / "no-private-file")
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
    assert [Path(p).name for p in paths] == ["gate.json", "summary.json"]


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
    assert release.apply_removals(store, now=NOW) is True  # removal-only: live with no gate
    assert (release.live_state(store) or {}).get("how") == "removal"
    assert release.live_corpus(store) == smaller
    # a candidate that adds a unit back and removes another: additions wait, removals do not
    gone = next(u for u in smaller.units if u.session == "s02")
    mixed = _sub(E.CORPUS, lambda u: unit_key(u) != unit_key(gone))
    store.save(mixed)
    assert release.apply_removals(store, now=NOW) is False
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
        return 1

    monkeypatch.setattr(eval_live, "main", fake_eval_live)
    for code in (1, 3, 4, 0):
        result = dataclasses.replace(_passing(), exit_code=code)
        monkeypatch.setattr(G, "check_run", lambda *a, result=result, **k: result)
        argv = ["eval", "--config", str(config), "--yes", "--out", str(tmp_path / f"o{code}")]
        assert G.main(argv) == code
        assert (release.live_state(store) is not None) == (code == 0), code
    assert G.main(["eval", "--config", str(config), "--split", "public"]) == 2
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
    assert release.apply_removals(store, now=NOW) is True
    state = release.live_state(store)
    assert release.apply_removals(store, now=NOW) is False  # nothing left to apply
    assert release.live_state(store) == state
    assert release.serving_corpus(store) == first == release.serving_corpus(store)


def test_drift_on_the_unchanged_corpus_changes_nothing_and_repeats_exactly():
    a = release.drift(E.PUBLIC_CASES, E.CORPUS, E.GOLDEN_VERSION)
    b = release.drift(E.PUBLIC_CASES, E.CORPUS, E.GOLDEN_VERSION)
    assert a == b and a.retired == a.converted == ()
    assert [c.model_dump_json() for c in a.cases] == [c.model_dump_json() for c in E.PUBLIC_CASES]


# ============ verifier pass 1 (E2.4): precedence, duplicates, baseline, exposure ============


def test_a_missing_score_in_a_cost_capped_run_exits_2_not_3(held_run, tmp_path):
    """Precedence 2 > 3: an input error in the records written so far wins over the cap."""
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
    v = release.verdict(run, inputs(run))
    assert v.exit_code == 2 and v.errors == [f"record {b9} has no identity score"]
    edit_json(run / "summary.json", lambda d: {**d, "judging_status": "pending"})
    assert release.verdict(run, inputs(run)).exit_code == 2  # and over pending


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
    monkeypatch.setattr(G, "HELDOUT_DIR", tmp_path / "no-private-file")
    for name, code in (("capped", 3), ("pending", 4)):
        run = fresh(held_run, tmp_path, name)
        manifest = tmp_path / f"{name}.json"
        manifest.write_text(json.dumps({"cases": {i: "x" for i in ids(run)}}), encoding="utf-8")
        monkeypatch.setattr(G, "HELDOUT_MANIFEST", manifest)
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
