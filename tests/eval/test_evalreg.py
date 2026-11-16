"""E2.3 CAL-04: the eval regression fixture and gate (`pytest -m evalreg`, node_converge gate 8).

The committed fixture (tests/eval/evalreg/) is a recorded course-ta run of public golden cases
(scripted outputs and fake judges until the live public-split run replaces it). No held-out
case, solution or judge rationale is in it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from course_assistant.eval import dataset
from course_assistant.eval.regression import FILES, record_fixture, regression_problems
from tests.eval import offline_run as O
from tests.eval import scripted_eval as E

FIXTURE = Path(__file__).with_name("evalreg")

pytestmark = pytest.mark.evalreg


def _rows(fixture: Path = FIXTURE) -> list[dict[str, Any]]:
    ids = json.loads((fixture / "fixture.json").read_text(encoding="utf-8"))["case_ids"]
    return dataset.build_rows([E.BY_ID[i] for i in ids], E.CORPUS, E.GOLDEN_VERSION)


def problems(fixture: Path = FIXTURE) -> list[str]:
    return regression_problems(fixture, _rows(fixture), corpus=E.CORPUS, config=E.CONFIG,
                               assessments=E.ASSESSMENTS)  # fmt: skip


def copy(tmp_path: Path) -> Path:
    out = tmp_path / "evalreg"
    shutil.copytree(FIXTURE, out)
    return out


def test_committed_fixture_replays_rescores_and_reaggregates_with_no_regression():
    assert problems() == []
    meta = json.loads((FIXTURE / "fixture.json").read_text(encoding="utf-8"))
    assert set(meta["baseline_kappa"]) == {"dual", "claude-opus-5-5", "gpt-6.1-sol"}
    assert meta["kappa_drop_tolerance"] == 0.05


def _edit_json_lines(path: Path, fn: Any) -> None:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    path.write_text("".join(json.dumps(fn(r)) + "\n" for r in rows), encoding="utf-8")


@pytest.mark.safety
def test_planted_defects_are_each_reported(tmp_path):
    fx = copy(tmp_path / "summary")
    summary = fx / "summary.json"
    summary.write_text(
        summary.read_text(encoding="utf-8").replace('"slices"', '"slices" ', 1), encoding="utf-8"
    )  # one byte
    assert problems(fx) == ["summary.json differs from EvalKit's re-aggregation"]

    fx = copy(tmp_path / "completion")
    first = json.loads((fx / "fixture.json").read_text(encoding="utf-8"))["case_ids"][0]

    def edit_answer(r: dict[str, Any]) -> dict[str, Any]:
        if r["case_id"] == first:
            r["completion"]["model_snapshot"] = "another-snapshot"
        return r

    _edit_json_lines(fx / "records.jsonl", edit_answer)
    assert f"{first}: replayed completion differs from the recording" in problems(fx)

    fx = copy(tmp_path / "score")

    def edit_score(r: dict[str, Any]) -> dict[str, Any]:
        if r["case_id"] == first:
            r["task_scores"]["leak_check"]["details"]["findings"] = ["planted"]
        return r

    _edit_json_lines(fx / "records.jsonl", edit_score)
    assert f"{first}: task score leak_check differs from the recording" in problems(fx)


def _set_baseline(fx: Path, delta: float) -> None:
    meta = json.loads((fx / "fixture.json").read_text(encoding="utf-8"))
    meta["baseline_kappa"] = {k: v + delta for k, v in meta["baseline_kappa"].items()}
    (fx / "fixture.json").write_text(json.dumps(meta), encoding="utf-8")


def test_support_kappa_drop_over_005_fails_and_exactly_005_passes(tmp_path):
    fx = copy(tmp_path / "over")
    _set_baseline(fx, 0.06)  # the current kappa now sits 0.06 below the baseline
    found = problems(fx)
    assert len(found) == 3 and all("held-out kappa fell" in p for p in found)
    fx = copy(tmp_path / "exact")
    _set_baseline(fx, 0.05)
    assert problems(fx) == []


@pytest.mark.contract
def test_a_changed_rubric_or_ground_version_asks_for_a_re_record(tmp_path):
    fx = copy(tmp_path)
    meta = json.loads((fx / "fixture.json").read_text(encoding="utf-8"))
    meta["judge_prompt_hash"] = "0000000000000000"
    meta["ground_version"] = "ground-000000000000"
    (fx / "fixture.json").write_text(json.dumps(meta), encoding="utf-8")
    found = problems(fx)
    assert sum("re-record" in p for p in found) == 2
    (fx / "support.jsonl").unlink()
    assert problems(fx) == ["fixture misses support.jsonl"]


@pytest.mark.golden
def test_the_fixture_builder_reproduces_a_passing_fixture(tmp_path):
    import build_evalreg_fixture

    out = build_evalreg_fixture.build(tmp_path / "rebuilt")
    assert sorted(p.name for p in out.iterdir()) == sorted(FILES)
    assert problems(out) == []
    for name in ("support.jsonl", "fixture.json", "probe-verdicts.jsonl"):
        assert (out / name).read_text("utf-8") == (FIXTURE / name).read_text("utf-8"), name


@pytest.mark.safety
def test_fixture_is_public_only_and_holds_no_solution_or_rationale(tmp_path):
    text = "\n".join((FIXTURE / f).read_text(encoding="utf-8") for f in FILES)
    assert "placeholder model solution" not in text  # never the integrity judge's prompt
    assert E.PROBE_RATIONALE not in text
    cases = [json.loads(x) for x in (FIXTURE / "records.jsonl").read_text("utf-8").splitlines()]
    assert all(E.BY_ID[c["case_id"]].split == "tuning" for c in cases)
    # a run holding a held-out case can never become a fixture
    case = E.first("B4")
    run = O.offline_run(tmp_path / "run", [case], {case.id: E.script_for(case)})
    O.mark_heldout(run.run_dir, {case.id})
    with pytest.raises(ValueError, match="public only"):
        record_fixture(run.run_dir, tmp_path / "fx", recordings=run.recorder.recordings,
                       probe_cache=tmp_path / "none.jsonl", support=[], now=O.NOW)  # fmt: skip
