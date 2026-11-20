"""A passing held-out `course-assistant eval` approves a course version for serving (R3.1,
OWNER): the same release gate as `scripts/release_gate.py` (`course_assistant.eval.gate`), with
the course's own held-out file, tuning split, tuning log and decisions. A tuning run never
approves (tests/release/test_eval_cli.py).

Offline: public sample-course cases relabelled as held-out inside a temporary course folder
(never golden/heldout), scripted model outputs, fake EvalKit adapters and judges. Certified
calibration needs labelled examples, so `certified_calibration` is replaced where a test needs
it certified; every test that leaves it alone sees it uncertified.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

import release_gate as G
from course_assistant import cli
from course_assistant.answer.ground import GroundStep
from course_assistant.eval import calibrate, course, gate, release, system, tuning
from course_assistant.ingest import CorpusStore
from course_assistant.ingest.corpus import Corpus
from course_assistant.ingest.serving import ServedCorpus
from course_assistant.schemas.config import AppConfig
from harness_lib import ROOT
from tests.answer import scripted as s
from tests.answer.test_ask import happy
from tests.eval import offline_run as O
from tests.eval import scripted_eval as E

RULES = (
    "B1",
    "B1",
    "B1",
    "B2",
    "B3",
    "B3",
    "B4",
    "B7",
    "B8",
    "B9",
    "B9",
    "B10",
    "B10",
)  # 2+ per metric
NOW = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)
CALIBRATED = {"groundedness": "calibrated", "integrity": "calibrated"}
DECISIONS = (
    "| ID | Date | Decision |\n| --- | --- | --- |\n"
    "| D-1 | 2027-01-01 | Grounding bar 0.5 and the shipped support rules |\n"
)


def _cases() -> list[Any]:
    out, seen = [], {}
    for r in RULES:
        k = seen.get(r, 0)
        seen[r] = k + 1
        out.append(E.first(r, multi=False, skip=k) if r == "B1" else E.first(r, skip=k))
    return out


def _jsonl(path: Path, cases: list[Any], split: str) -> Path:
    rows = [json.dumps({**c.model_dump(mode="json"), "split": split}) for c in cases]
    path.write_text("".join(x + "\n" for x in rows), encoding="utf-8")
    return path


@dataclass
class Course:
    tmp: Path
    config: Path
    heldout: Path
    public: Path
    solutions: Path
    out: Path

    @property
    def store(self) -> CorpusStore:
        return CorpusStore(self.config.parent / "corpus_store", E.CONFIG.cohort)

    @property
    def log(self) -> Path:
        return self.config.parent / "tuning-log.jsonl"

    def argv(self, *extra: str) -> list[str]:
        return ["eval", "--config", str(self.config), "--cases", str(self.heldout), "--root",
                str(ROOT), "--public", str(self.public), "--out", str(self.out), "--ledger",
                str(self.out / "ledger.jsonl"), "--solutions", str(self.solutions), "--yes",
                "--live-judges", *extra]  # fmt: skip

    def check(self, run: Path, *extra: str) -> list[str]:
        return ["eval-check", str(run), "--config", str(self.config), "--cases",
                str(self.heldout), "--public", str(self.public), *extra]  # fmt: skip


def make_course(tmp: Path) -> Course:
    """A course folder outside any git repository: config, the sample manifest and briefs, the
    sample corpus ingested, the held-out and tuning files, decisions and a tuning log that logs
    the bar and support rules in use."""
    folder = tmp / "my-course"
    folder.mkdir(parents=True)
    config = folder / "config.yaml"
    config.write_text(yaml.safe_dump(E.CONFIG.model_dump(mode="json")), encoding="utf-8")
    shutil.copy(ROOT / "sample_course" / "manifest.yaml", folder / "manifest.yaml")
    # the course's own briefs: the sample assessments its golden cases probe (the coverage rule
    # needs 3 graded-work probes per brief across the two splits)
    briefs = yaml.safe_load((ROOT / "sample_course" / "assessments.yaml").read_text("utf-8"))
    probed = {c.assessment_id for c in E.PUBLIC_CASES if c.rule == "B3"}
    briefs["assessments"] = [a for a in briefs["assessments"] if a["assessment_id"] in probed]
    (folder / "assessments.yaml").write_text(yaml.safe_dump(briefs), encoding="utf-8")
    CorpusStore(folder / "corpus_store", E.CONFIG.cohort).save(E.CORPUS)
    held = _cases()
    ids = {c.id for c in held}
    heldout = _jsonl(folder / "golden-heldout.jsonl", held, "heldout")
    public = _jsonl(folder / "golden-public.jsonl",
                    [c for c in E.PUBLIC_CASES if c.id not in ids], "tuning")  # fmt: skip
    (folder / "decisions.md").write_text(DECISIONS, encoding="utf-8")
    log = folder / "tuning-log.jsonl"
    tuning.log_change(log, {"kind": "grounding_bar", "decision": "D-1", "from": None,
                            "to": E.CONFIG.grounding_bar}, DECISIONS)  # fmt: skip
    tuning.log_change(log, {"kind": "support_rules", "decision": "D-1", "from": None,
                            "to": GroundStep.version()}, DECISIONS)  # fmt: skip
    solutions = tmp / "private-solutions"
    solutions.mkdir()
    for a in E.ASSESSMENTS.assessments:
        (solutions / f"{a.assessment_id}.md").write_text("A private model solution.\n",
                                                         encoding="utf-8")  # fmt: skip
    return Course(tmp, config, heldout, public, solutions, tmp / "runs")


def runtime_for(cases: list[Any]) -> Any:
    scripts = {c.id: E.script_for(c) for c in cases}

    def runtime(config_: Any, corpus: Any, assessments: Any, folder: Path, ledger: Path,
                budget: Any) -> tuple[Any, Any, Any]:  # fmt: skip
        recorder = O.RecordedPerCase(scripts)
        rt = system.Runtime(config=E.CONFIG, corpus=E.CORPUS, assessments=E.ASSESSMENTS,
                            source=recorder, now=lambda: NOW, timer=lambda: 0.0)  # fmt: skip
        panel, _ = O.fake_panel(cache=folder / "probe-verdicts.jsonl")
        return rt, panel, recorder

    return runtime


def run_eval(c: Course, mp: pytest.MonkeyPatch, *, certified: bool, extra: tuple[str, ...] = ()
             ) -> tuple[int, Path]:  # fmt: skip
    mp.delenv("CI", raising=False)  # outside git the append-only check is skipped, as locally
    mp.setattr(cli, "_eval_runtime", runtime_for(_cases()))
    if certified:
        mp.setattr(calibrate, "certified_calibration", lambda run, log: dict(CALIBRATED))
    before = set(c.out.glob("course-ta-heldout-*"))
    with O._patched(c.tmp):
        code = cli.main(c.argv(*extra))
    [folder] = sorted(set(c.out.glob("course-ta-heldout-*")) - before)
    return code, folder


@pytest.fixture(scope="module")
def approved(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Course, Path, int]]:
    """One held-out eval with calibration certified: it passes and approves."""
    c = make_course(tmp_path_factory.mktemp("approve"))
    with pytest.MonkeyPatch.context() as mp:
        code, folder = run_eval(c, mp, certified=True)
    yield c, folder, code


def test_a_passing_heldout_eval_approves_the_version_ask_serves(approved, capsys, monkeypatch):
    c, folder, code = approved
    verdict = json.loads((folder / "gate.json").read_text(encoding="utf-8"))
    assert code == cli.EXIT_OK, verdict
    assert verdict["exit_code"] == 0 and verdict["failures"] == [] and verdict["errors"] == []
    assert course.FIXTURE_NOTE in verdict["notes"]
    assert any("single judge" in n for n in verdict["notes"])  # D-80, as the repo's gate says
    assert {g["name"] for g in verdict["gates"]} >= {"grounded", "integrity", "identity"}
    # the same record the repo's gate writes
    state = release.live_state(c.store) or {}
    assert state["corpus_version"] == E.CORPUS.corpus_version and state["how"] == "gate"
    briefs = (c.config.parent / "assessments.yaml").read_bytes()
    assert state["settings"] == gate.gated_settings(E.CONFIG, briefs)
    assert release.live_baseline(c.store) is not None
    served, _ = ServedCorpus(c.store).read()
    assert served.corpus_version == E.CORPUS.corpus_version
    # the held-out score is logged, as eval_live logs it
    kinds = [e["kind"] for e in tuning.read_log(c.log)]
    assert kinds[-1] == "heldout_scored" and not tuning.chain_problems(c.log)
    # and `ask` now answers without --ungated
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-key")
    monkeypatch.chdir(c.tmp)
    monkeypatch.setattr(cli, "_openai_client", lambda: s.StepClient(**happy()))
    capsys.readouterr()
    assert cli.main(["ask", "What is on slide 3?", "--config", str(c.config)]) == cli.EXIT_OK
    assert "status: answered" in capsys.readouterr().out


def test_eval_check_applies_the_full_gate_against_the_live_baseline(
    approved, tmp_path, monkeypatch, capsys
):
    c, folder, _ = approved
    live = (c.store.dir / release.LIVE_FILE).read_text(encoding="utf-8")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(calibrate, "certified_calibration", lambda run, log: dict(CALIBRATED))
    assert cli.main(c.check(folder)) == cli.EXIT_OK  # paired against itself: no regression
    assert (c.store.dir / release.LIVE_FILE).read_text(encoding="utf-8") == live, \
        "eval-check never approves"  # fmt: skip
    missing = c.check(folder, "--tuning-log", str(tmp_path / "none.jsonl"))
    capsys.readouterr()
    assert cli.main(missing) == cli.EXIT_GATE
    assert "tuning log" in capsys.readouterr().out
    monkeypatch.undo()
    monkeypatch.delenv("CI", raising=False)
    assert cli.main(c.check(folder)) == cli.EXIT_GATE
    assert "calibration is not certified" in capsys.readouterr().out
    assert cli.main(["eval-check", str(folder)]) == cli.EXIT_CONFIG  # held-out: needs files


def test_eval_check_refuses_a_run_that_is_not_the_heldout_file(approved, tmp_path, monkeypatch,
                                                              capsys):  # fmt: skip
    c, folder, _ = approved
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(calibrate, "certified_calibration", lambda run, log: dict(CALIBRATED))
    lines = c.heldout.read_text(encoding="utf-8").splitlines()
    fewer = tmp_path / "fewer.jsonl"
    fewer.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    argv = ["eval-check", str(folder), "--config", str(c.config), "--cases", str(fewer)]
    assert cli.main(argv) == cli.EXIT_CONFIG
    assert "not the held-out split" in capsys.readouterr().out
    assert cli.main([*argv[:4], "--cases", str(c.public)]) == cli.EXIT_CONFIG


def test_a_failing_heldout_eval_approves_nothing(tmp_path, monkeypatch):
    c = make_course(tmp_path)
    code, folder = run_eval(c, monkeypatch, certified=False)  # calibration not certified
    assert code == cli.EXIT_GATE
    assert release.live_state(c.store) is None
    verdict = json.loads((folder / "gate.json").read_text(encoding="utf-8"))
    assert any("calibration is not certified" in f for f in verdict["failures"])


def test_no_approve_reports_only(tmp_path, monkeypatch):
    c = make_course(tmp_path)
    code, _ = run_eval(c, monkeypatch, certified=True, extra=("--no-approve",))
    assert code == cli.EXIT_OK
    assert release.live_state(c.store) is None


def test_a_heldout_eval_needs_the_bar_and_rules_logged_and_a_fresh_set(tmp_path, monkeypatch,
                                                                      capsys):  # fmt: skip
    c = make_course(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-key")
    monkeypatch.setattr(cli, "_eval_runtime", lambda *a: pytest.fail("no run may start"))
    c.log.unlink()
    assert cli.main(c.argv()) == cli.EXIT_CONFIG
    assert "logged" in capsys.readouterr().err
    c2 = make_course(tmp_path / "second")
    tuning.log_heldout_scored(c2.log, run="earlier", bar=0.5, ground_version=GroundStep.version())
    tuning.log_change(c2.log, {"kind": "grounding_bar", "decision": "D-1", "from": 0.5,
                               "to": 0.6}, DECISIONS)  # fmt: skip
    assert cli.main(c2.argv()) == cli.EXIT_CONFIG
    assert "fresh held-out set" in capsys.readouterr().err


def test_a_removal_only_candidate_goes_live_with_no_run(approved, tmp_path, monkeypatch):
    c, _, _ = approved
    copy = tmp_path / "course"
    shutil.copytree(c.config.parent, copy)
    moved = Course(c.tmp, copy / "config.yaml", copy / c.heldout.name, copy / c.public.name,
                   c.solutions, tmp_path / "runs")  # fmt: skip
    smaller = [u for u in E.CORPUS.units if u.session != "s03"]
    from course_assistant.ingest import corpus_version

    cv = corpus_version(smaller)
    candidate = Corpus(schema_version="1.0", cohort="sample", corpus_version=cv,
                       units=tuple(u.model_copy(update={"unit_index": i, "corpus_version": cv})
                                   for i, u in enumerate(smaller, start=1)))  # fmt: skip
    moved.store.save(candidate)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-key")
    monkeypatch.setattr(cli, "_eval_runtime", lambda *a: pytest.fail("no run for a removal"))
    assert cli.main(moved.argv()) == cli.EXIT_OK
    state = release.live_state(moved.store) or {}
    assert state["how"] == "removal"


def test_the_repo_gate_and_the_course_gate_are_one_code_path():
    briefs = (ROOT / "sample_course" / "assessments.yaml").read_bytes()
    assert G.gated_settings(E.CONFIG) == gate.gated_settings(E.CONFIG, briefs)
    assert G.live_folder(Path("a/evalkit/r")) == gate.live_folder(Path("a/evalkit/r"))
    assert isinstance(E.CONFIG, AppConfig)
