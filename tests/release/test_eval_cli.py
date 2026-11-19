"""`course-assistant eval` and `eval-check` (R3.1, OWNER on OSS-04/OSS-07): a course team
evaluates its own course from the installed package, with its own golden files by path.

Offline: public sample-course cases written to a temporary file (never golden/heldout), the
sample corpus in a temporary store, scripted model outputs per case, fake EvalKit adapters and
a fake probe panel (tests/eval/offline_run.py). No network.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from course_assistant import cli
from course_assistant.eval import course, system
from course_assistant.ingest import CorpusStore
from harness_lib import ROOT
from tests.eval import offline_run as O
from tests.eval import scripted_eval as E

RULES = ("B1", "B1", "B1", "B2", "B3", "B4", "B7", "B8", "B9", "B10")
NOW = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)


def _cases() -> list[Any]:
    out, seen = [], {}
    for r in RULES:
        k = seen.get(r, 0)
        seen[r] = k + 1
        out.append(E.first(r, multi=False, skip=k) if r == "B1" else E.first(r, skip=k))
    return out


def _course(tmp: Path, cases: list[Any], *, split: str = "tuning") -> tuple[Path, Path]:
    """A course folder outside the repo: config, the sample manifest and briefs, the sample
    corpus ingested, and the course's own cases file."""
    folder = tmp / "my-course"
    folder.mkdir(parents=True)
    config = folder / "config.yaml"
    config.write_text(yaml.safe_dump(E.CONFIG.model_dump(mode="json")), encoding="utf-8")
    shutil.copy(ROOT / "sample_course" / "manifest.yaml", folder / "manifest.yaml")
    shutil.copy(ROOT / "sample_course" / "assessments.yaml", folder / "assessments.yaml")
    CorpusStore(folder / "corpus_store", E.CONFIG.cohort).save(E.CORPUS)
    rows = [json.dumps({**c.model_dump(mode="json"), "split": split}) for c in cases]
    cases_file = folder / "golden.jsonl"
    cases_file.write_text("".join(x + "\n" for x in rows), encoding="utf-8")
    return config, cases_file


def _argv(config: Path, cases_file: Path, out: Path, *extra: str) -> list[str]:
    # the sample manifest's paths are relative to the repo root
    return ["eval", "--config", str(config), "--cases", str(cases_file), "--root", str(ROOT),
            "--out", str(out), "--ledger", str(out / "ledger.jsonl"), *extra]  # fmt: skip


def test_dry_run_builds_the_task_from_the_courses_own_files(tmp_path, capsys, monkeypatch):
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    config, cases_file = _course(tmp_path, _cases())
    out = tmp_path / "runs"
    assert cli.main(_argv(config, cases_file, out, "--dry-run")) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert f"{len(RULES)} public cases" in printed and "estimate: $" in printed
    [folder] = list(out.glob("course-ta-public-*"))
    rows = (folder / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == len(RULES)
    assert "judges: [gpt-6.1-sol]" in (folder / "evalkit.yaml").read_text(encoding="utf-8")
    assert not any(c.question in printed for c in _cases()), "no case text is printed"


def test_load_cases_names_the_split_and_refuses_a_mixed_file(tmp_path):
    cases = _cases()
    _, public = _course(tmp_path / "a", cases)
    _, held = _course(tmp_path / "b", cases, split="heldout")
    assert course.load_cases(public)[1] == "public"
    assert course.load_cases(held)[1] == "heldout"
    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text(public.read_text(encoding="utf-8").splitlines()[0] + "\n"
                     + held.read_text(encoding="utf-8").splitlines()[1] + "\n",
                     encoding="utf-8")  # fmt: skip
    with pytest.raises(course.CourseEvalError, match="mixes"):
        course.load_cases(mixed)
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "x"}\n', encoding="utf-8")
    with pytest.raises(course.CourseEvalError, match="line 1"):
        course.load_cases(bad)


def test_a_live_run_needs_yes_the_keys_and_solutions_for_graded_work(tmp_path, capsys,
                                                                    monkeypatch):  # fmt: skip
    config, cases_file = _course(tmp_path, _cases())
    out = tmp_path / "runs"
    monkeypatch.setattr(cli, "_eval_runtime", lambda *a: pytest.fail("no run may start"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert cli.main(_argv(config, cases_file, out)) == cli.EXIT_CONFIG
    assert "--yes" in capsys.readouterr().err
    assert cli.main(_argv(config, cases_file, out, "--yes")) == cli.EXIT_CONFIG
    assert "OPENAI_API_KEY not set" in capsys.readouterr().err
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-key")
    assert cli.main(_argv(config, cases_file, out, "--yes")) == cli.EXIT_CONFIG
    assert "--solutions" in capsys.readouterr().err
    assert (
        cli.main(
            _argv(
                config,
                cases_file,
                out,
                "--yes",
                "--solutions",
                str(tmp_path / "solutions"),
                "--max-cost",
                "0.01",
            )
        )
        == cli.EXIT_CONFIG
    )
    (tmp_path / "solutions").mkdir()
    assert (
        cli.main(
            _argv(
                config,
                cases_file,
                out,
                "--yes",
                "--solutions",
                str(tmp_path / "solutions"),
                "--max-cost",
                "0.01",
            )
        )
        == cli.EXIT_COST_CAP
    )


def test_missing_corpus_or_cases_exit_2(tmp_path, capsys):
    config, cases_file = _course(tmp_path, _cases())
    out = tmp_path / "runs"
    assert cli.main(_argv(config, tmp_path / "none.jsonl", out, "--dry-run")) == cli.EXIT_CONFIG
    shutil.rmtree(config.parent / "corpus_store")
    assert cli.main(_argv(config, cases_file, out, "--dry-run")) == cli.EXIT_CONFIG
    assert "ingest" in capsys.readouterr().err


@pytest.fixture(scope="module")
def course_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A whole `course-assistant eval` run of a tuning file, offline: scripted system, fake
    judges. A tuning run gets the thresholds only and never approves."""
    tmp = tmp_path_factory.mktemp("course-eval")
    cases = _cases()
    config, cases_file = _course(tmp, cases)
    out = tmp / "runs"
    solutions = tmp / "solutions"
    solutions.mkdir()
    for a in E.ASSESSMENTS.assessments:
        (solutions / f"{a.assessment_id}.md").write_text("A private model solution.\n",
                                                         encoding="utf-8")  # fmt: skip
    scripts = {c.id: E.script_for(c) for c in cases}

    def runtime(config_: Any, corpus: Any, assessments: Any, folder: Path, ledger: Path,
                budget: Any) -> tuple[Any, Any, Any]:  # fmt: skip
        recorder = O.RecordedPerCase(scripts)
        rt = system.Runtime(config=E.CONFIG, corpus=E.CORPUS, assessments=E.ASSESSMENTS,
                            source=recorder, now=lambda: NOW, timer=lambda: 0.0)  # fmt: skip
        panel, _ = O.fake_panel(cache=folder / "probe-verdicts.jsonl")
        return rt, panel, recorder

    saved = cli._eval_runtime
    cli._eval_runtime = runtime  # type: ignore[assignment]
    try:
        with O._patched(tmp):
            code = cli.main(_argv(config, cases_file, out, "--yes", "--solutions",
                                  str(solutions), "--live-judges"))  # fmt: skip
    finally:
        cli._eval_runtime = saved  # type: ignore[assignment]
    assert code == cli.EXIT_OK
    [folder] = list(out.glob("course-ta-public-*"))
    store = CorpusStore(config.parent / "corpus_store", E.CONFIG.cohort)
    assert not (store.dir / "LIVE.json").exists(), "a tuning run never approves"
    return folder


def test_a_full_offline_run_passes_every_threshold_and_writes_gate_json(course_run):
    gate = json.loads((course_run / "gate.json").read_text(encoding="utf-8"))
    assert gate["exit_code"] == 0 and gate["split"] == "public"
    assert "a tuning run never approves a course version" in gate["notes"]
    names = {g["name"] for g in gate["gates"]}
    assert {
        "grounded",
        "citation_validity",
        "locator_correct",
        "handover",
        "integrity",
        "injection",
        "leak",
        "identity",
        "latency_p95_ms",
    } <= names
    assert (course_run / "recordings.jsonl").stat().st_size > 0
    text = (course_run / "gate.json").read_text(encoding="utf-8")
    assert not any(c.question in text for c in _cases())


def _copy(course_run: Path, tmp: Path) -> Path:
    out = tmp / course_run.name
    shutil.copytree(course_run, out)
    return out


def _edit_records(run_dir: Path, fn: Any) -> None:
    path = run_dir / "records.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    path.write_text("".join(json.dumps(fn(r)) + "\n" for r in rows), encoding="utf-8")


def test_eval_check_exit_codes(course_run, tmp_path, capsys):
    folder = _copy(course_run, tmp_path / "ok")
    assert cli.main(["eval-check", str(folder)]) == cli.EXIT_OK
    assert "pass grounded" in capsys.readouterr().out

    slow = _copy(course_run, tmp_path / "slow")
    run_dir = course.evalkit_run_dir(slow)

    def _slow(r: dict[str, Any]) -> dict[str, Any]:
        r["completion"]["latency_ms"] = 12000
        return r

    _edit_records(run_dir, _slow)
    assert cli.main(["eval-check", str(slow)]) == cli.EXIT_GATE
    assert "FAIL latency_p95_ms" in capsys.readouterr().out

    capped = _copy(course_run, tmp_path / "capped")
    run_dir = course.evalkit_run_dir(capped)
    lines = (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
    (run_dir / "records.jsonl").write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    assert cli.main(["eval-check", str(capped)]) == cli.EXIT_COST_CAP

    pending = _copy(course_run, tmp_path / "pending")
    run_dir = course.evalkit_run_dir(pending)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    (run_dir / "summary.json").write_text(json.dumps({**summary, "judging_status": "pending"}),
                                          encoding="utf-8")  # fmt: skip
    assert cli.main(["eval-check", str(pending)]) == cli.EXIT_PENDING
    assert cli.main(["eval-check", str(tmp_path / "nothing")]) == cli.EXIT_CONFIG


def test_a_public_run_is_marked_as_unable_to_clear_a_change(course_run, tmp_path):
    folder = _copy(course_run, tmp_path / "public")
    run_dir = course.evalkit_run_dir(folder)
    path = run_dir / "cases.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for r in rows:
        r["meta"]["split"] = "tuning"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    v = course.course_gate(run_dir)
    assert v.split == "public"
    assert any("not a held-out run" in n for n in v.notes)


def test_eval_live_still_prices_and_prepares_through_the_package():
    import eval_live

    assert eval_live.estimate is course.estimate
    from course_assistant.eval import panel

    assert eval_live.JUDGES == panel.JUDGES
    assert eval_live.evalkit_config() == course.evalkit_config()
