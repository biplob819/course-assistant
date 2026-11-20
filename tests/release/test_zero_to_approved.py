"""From nothing to an approved course with the package alone (R3.1, OWNER): `init`, a tuning
`eval`, `tuning-log`, a calibration `eval` of both files, `calibrate` export, import and score,
a held-out `eval` that passes the release gate and approves, and `ask` serving that version.

Offline: public sample-course cases (some relabelled as held-out inside a temporary course
folder; never golden/heldout), scripted model outputs, fake EvalKit adapters and judges, and
scripted labels standing in for the owner's. No network, no model call.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import shutil
from pathlib import Path
from typing import Any

import yaml
from evalkit import run as R

from course_assistant import cli
from course_assistant.eval import calibrate as C
from course_assistant.eval import release, system, tuning
from course_assistant.ingest import CorpusStore
from course_assistant.ingest.serving import ServedCorpus
from harness_lib import ROOT
from tests.answer import scripted as s
from tests.answer.test_ask import happy
from tests.eval import offline_run as O
from tests.eval import scripted_eval as E

NOW = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)
DRAFT = "FULL-DRAFT"  # a hint holding this is judged a violation (the calibration run only)
# held-out stand-ins: two or more per gated metric; the tuning sample is drawn after them
HELD_RULES = ("B1", "B1", "B1", "B2", "B3", "B3", "B4", "B7", "B8", "B9", "B9", "B10", "B10")
TUNE_RULES = ("B1", "B1", "B1", "B1", "B2", "B3", "B3", "B4", "B7", "B8", "B9", "B10")
DECISIONS = ("| ID | Date | Decision |\n| --- | --- | --- |\n"
             "| D-1 | 2027-01-01 | Grounding bar 0.5 after the tuning run |\n"
             "| D-2 | 2027-01-02 | The support rules this package ships |\n")  # fmt: skip


def _pick(rules: tuple[str, ...], offset: int) -> list[Any]:
    out, seen = [], {}
    for r in rules:
        k = seen.get(r, 0) + offset
        seen[r] = seen.get(r, 0) + 1
        out.append(E.first(r, multi=False, skip=k) if r == "B1" else E.first(r, skip=k))
    return out


HELD = _pick(HELD_RULES, 0)
TUNE = _pick(TUNE_RULES, 3)


def _jsonl(path: Path, cases: list[Any], split: str) -> Path:
    rows = [json.dumps({**c.model_dump(mode="json"), "split": split}) for c in cases]
    path.write_text("".join(x + "\n" for x in rows), encoding="utf-8")
    return path


def _runtime(cases: list[Any], drafted: set[str], folder_mark: str | None) -> Any:
    scripts = {c.id: E.script_for(c) for c in cases}
    for c in cases:
        if c.id in drafted:
            scripts[c.id] = E.hint(c, f"{DRAFT}: here is the whole submission, section by section.")

    def runtime(config_: Any, corpus: Any, assessments: Any, folder: Path, ledger: Path,
                budget: Any) -> tuple[Any, Any, Any]:  # fmt: skip
        recorder = O.RecordedPerCase(scripts)
        rt = system.Runtime(config=E.CONFIG, corpus=E.CORPUS, assessments=E.ASSESSMENTS,
                            source=recorder, now=lambda: NOW, timer=lambda: 0.0)  # fmt: skip
        panel, _ = O.fake_panel(violation_mark=folder_mark,
                                cache=folder / "probe-verdicts.jsonl")  # fmt: skip
        return rt, panel, recorder

    return runtime


def _csv(text: str, label: Any) -> str:
    rows = list(csv.DictReader(io.StringIO(text)))
    for i, row in enumerate(rows):
        row["label"] = label(i, row)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def test_a_course_goes_from_init_to_an_approved_version_that_ask_serves(tmp_path, monkeypatch,
                                                                       capsys):  # fmt: skip
    monkeypatch.delenv("CI", raising=False)  # outside git: the append-only check is skipped
    course = tmp_path / "my-course"
    out = tmp_path / "runs"

    # 1. init, then fill in the files (the corpus stands in for `ingest`, which calls the API)
    assert cli.main(["init", str(course)]) == cli.EXIT_OK
    config = course / "config.yaml"
    assert "vs_REPLACE_WITH_YOUR_STORE_ID" in config.read_text(encoding="utf-8")
    # filled in: the course's cohort, store and models (the scripted ones), bar 0.5
    config.write_text(yaml.safe_dump(E.CONFIG.model_dump(mode="json")), encoding="utf-8")
    shutil.copy(ROOT / "sample_course" / "manifest.yaml", course / "manifest.yaml")
    briefs = yaml.safe_load((ROOT / "sample_course" / "assessments.yaml").read_text("utf-8"))
    probed = {c.assessment_id for c in E.PUBLIC_CASES if c.rule == "B3"}
    briefs["assessments"] = [a for a in briefs["assessments"] if a["assessment_id"] in probed]
    (course / "assessments.yaml").write_text(yaml.safe_dump(briefs), encoding="utf-8")
    (course / "decisions.md").write_text(DECISIONS, encoding="utf-8")
    store = CorpusStore(course / "corpus_store", E.CONFIG.cohort)
    store.save(E.CORPUS)
    held_ids = {c.id for c in HELD}
    heldout = _jsonl(course / "golden-heldout.jsonl", HELD, "heldout")
    sample = _jsonl(course / "golden-tune.jsonl", TUNE, "tuning")
    public = _jsonl(course / "golden-public.jsonl",
                    [c for c in E.PUBLIC_CASES if c.id not in held_ids], "tuning")  # fmt: skip
    solutions = tmp_path / "private-solutions"
    solutions.mkdir()
    for a in briefs["assessments"]:
        (solutions / f"{a['assessment_id']}.md").write_text("A private model solution.\n",
                                                            encoding="utf-8")  # fmt: skip
    base = [
        "--config",
        str(config),
        "--root",
        str(ROOT),
        "--out",
        str(out),
        "--ledger",
        str(out / "ledger.jsonl"),
        "--solutions",
        str(solutions),
        "--yes",
        "--live-judges",
    ]

    def run_eval(cases: list[str], rt: Any, *extra: str, cwd: str = "work") -> tuple[int, Path]:
        monkeypatch.setattr(cli, "_eval_runtime", rt)
        before = set(out.glob("course-ta-*"))
        (tmp_path / cwd).mkdir(exist_ok=True)
        with O._patched(tmp_path / cwd):  # EvalKit keeps its cache in the working folder
            code = cli.main(["eval", "--cases", *cases, *base, *extra])
        [folder] = sorted(set(out.glob("course-ta-*")) - before)
        return code, folder

    # 2. a tuning run: thresholds only, nothing approved
    code, tune_run = run_eval([str(sample)], _runtime(TUNE, set(), None))
    assert code == cli.EXIT_OK and release.live_state(store) is None

    # 3. log the bar chosen on the tuning half and the shipped support rules
    assert cli.main(["tuning-log", "grounding_bar", "--to", "0.5", "--decision", "D-1",
                     "--config", str(config)]) == cli.EXIT_OK  # fmt: skip
    assert cli.main(["tuning-log", "support_rules", "--decision", "D-2",
                     "--config", str(config)]) == cli.EXIT_OK  # fmt: skip

    # 4. a calibration run of both files (some replies draft the brief, for the judges)
    drafted = {next(c.id for c in TUNE if c.rule == "B3"), next(c.id for c in HELD
                                                              if c.rule == "B3")}  # fmt: skip
    code, cal = run_eval([str(sample), str(heldout)], _runtime(TUNE + HELD, drafted, DRAFT))
    assert code in (cli.EXIT_OK, cli.EXIT_GATE)  # thresholds only (drafted replies fail one)
    assert release.live_state(store) is None, "a calibration run never approves"
    cal_dir = next((cal / "evalkit").glob("*/run.json")).parent

    # 5. calibrate: export the blind sheets, import the labels, score into the tuning log
    capsys.readouterr()
    sheets = ["calibrate", "export", str(cal), "--heldout", str(heldout), "--n", "4",
              "--n-relabel", "2"]  # fmt: skip
    assert cli.main(sheets) == cli.EXIT_OK
    g_csv = cal_dir / "calibration" / "sheet.csv"
    filled = tmp_path / "groundedness.csv"
    filled.write_text(_csv(g_csv.read_text("utf-8"), lambda i, r: "U" if i == 0 else "S"),
                      encoding="utf-8")  # fmt: skip
    for extra in ((), ("--relabel",)):
        assert cli.main(["calibrate", "import-groundedness", str(cal), str(filled),
                         *extra]) == cli.EXIT_OK  # fmt: skip
    verdicts = C._judge_verdicts(R.read_records(cal_dir), "integrity")  # pyright: ignore[reportPrivateUsage]
    rows = {r.key: r for r in C._load_rows(cal_dir, "integrity")}  # pyright: ignore[reportPrivateUsage]

    def agree(i: int, row: dict[str, str]) -> str:
        return "V" if verdicts[rows[row["key"]].case_id]["gpt-6.1-sol"] == "violation" else "C"

    d = C.probe_dir(cal_dir)
    for name, extra in (("sheet.csv", ()), ("relabel.csv", ("--relabel",))):
        path = tmp_path / f"integrity-{name}"
        path.write_text(_csv((d / name).read_text("utf-8"), agree), encoding="utf-8")
        assert cli.main(["calibrate", "import-integrity", str(cal), str(path),
                         *extra]) == cli.EXIT_OK  # fmt: skip
    assert cli.main(["calibrate", "score", str(cal), "--heldout", str(heldout), "--labeller",
                     "owner", "--config", str(config)]) == cli.EXIT_OK  # fmt: skip
    certified = C.certified_calibration(cal_dir, log=course / "tuning-log.jsonl")
    assert certified["groundedness"] == certified["integrity"] == "calibrated"
    assert cli.main([*sheets, "--force"]) == cli.EXIT_CONFIG, "labelled sheets are never redone"

    # 6. the held-out run passes the release gate and approves
    # (from a fresh working folder: no completion of the calibration run is reused)
    code, held_run = run_eval([str(heldout)], _runtime(HELD, set(), DRAFT), "--public",
                              str(public), "--calibration", str(cal), cwd="held")  # fmt: skip
    verdict = json.loads((held_run / "gate.json").read_text(encoding="utf-8"))
    assert code == cli.EXIT_OK, verdict
    state = release.live_state(store) or {}
    assert state["corpus_version"] == E.CORPUS.corpus_version and state["how"] == "gate"
    assert ServedCorpus(store).read()[0].corpus_version == E.CORPUS.corpus_version
    log = tuning.read_log(course / "tuning-log.jsonl")
    assert [e["kind"] for e in log].count("calibration_scored") == 2
    assert not tuning.chain_problems(course / "tuning-log.jsonl")

    # 7. ask serves the approved version, with no --ungated
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-key")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_openai_client", lambda: s.StepClient(**happy()))
    capsys.readouterr()
    assert cli.main(["ask", "What is on slide 3?", "--config", str(config)]) == cli.EXIT_OK
    assert "status: answered" in capsys.readouterr().out
    for c in HELD:  # nothing printed or logged holds held-out text
        assert c.question not in (course / "tuning-log.jsonl").read_text(encoding="utf-8")
    assert tune_run.is_dir()


def test_tuning_log_refuses_uncited_or_discontinuous_changes(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("x: 1\n", encoding="utf-8")
    argv = ["--config", str(config)]
    assert cli.main(["tuning-log", "grounding_bar", "--to", "0.5", "--decision", "D-1",
                     *argv]) == cli.EXIT_CONFIG  # fmt: skip
    assert "decisions" in capsys.readouterr().err
    (tmp_path / "decisions.md").write_text(DECISIONS, encoding="utf-8")
    assert cli.main(["tuning-log", "grounding_bar", "--to", "0.5", "--decision", "D-9",
                     *argv]) == cli.EXIT_CONFIG  # fmt: skip
    assert "not a row of decisions.md" in capsys.readouterr().err
    assert cli.main(["tuning-log", "grounding_bar", "--decision", "D-1", *argv]) == 2
    assert cli.main(["tuning-log", "grounding_bar", "--to", "1.5", "--decision", "D-1",
                     *argv]) == cli.EXIT_CONFIG  # fmt: skip
    assert cli.main(["tuning-log", "grounding_bar", "--to", "0.5", "--decision", "D-1",
                     *argv]) == cli.EXIT_OK  # fmt: skip
    assert "None -> 0.5" in capsys.readouterr().out
    assert cli.main(["tuning-log", "grounding_bar", "--to", "0.5", "--decision", "D-1",
                     *argv]) == cli.EXIT_CONFIG, "a change must change the value"  # fmt: skip
    assert cli.main(["tuning-log", "heldout_refreshed", "--decision", "D-1",
                     *argv]) == cli.EXIT_CONFIG  # fmt: skip
    assert "Refreshes held-out" in capsys.readouterr().err
    with (tmp_path / "decisions.md").open("a", encoding="utf-8") as fh:
        fh.write("| D-3 | 2027-02-01 | Refreshes held-out: a fresh set after the bar change |\n")
    assert cli.main(["tuning-log", "heldout_refreshed", "--decision", "D-3", *argv]) == 0
    log = tmp_path / "tuning-log.jsonl"
    assert [e["kind"] for e in tuning.read_log(log)] == ["grounding_bar", "heldout_refreshed"]
    assert not tuning.chain_problems(log)


def test_calibrate_refuses_a_non_heldout_file_and_a_missing_run(tmp_path, capsys):
    tune = _jsonl(tmp_path / "tune.jsonl", TUNE, "tuning")
    assert cli.main(["calibrate", "export", str(tmp_path / "none"), "--heldout",
                     str(tune)]) == cli.EXIT_CONFIG  # fmt: skip
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text("{}", encoding="utf-8")
    assert cli.main(["calibrate", "export", str(run), "--heldout", str(tune)]) == 2
    assert "not a held-out file" in capsys.readouterr().err
    held = _jsonl(tmp_path / "held.jsonl", HELD, "heldout")
    assert cli.main(["calibrate", "score", str(run), "--heldout", str(held), "--labeller",
                     "owner"]) == cli.EXIT_CONFIG  # fmt: skip
    assert "--config" in capsys.readouterr().err
