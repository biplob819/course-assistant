"""`course-assistant ask` (R3.1, OWNER on OSS-04): one question through the real answer path.

Offline: the OpenAI client is the scripted per-step fake (tests/answer/scripted.py), the course
is a temporary folder with a corpus store, and a sentinel key value must never be printed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from course_assistant import cli
from course_assistant.demo.guard import GuardConfig
from course_assistant.llm import Ledger
from harness_lib import ROOT
from tests.answer import scripted as s
from tests.answer.test_ask import CORPUS, DRAFT, QUESTION, S01, happy
from tests.serving.helpers import Course

SENTINEL = "SENTINEL-secret-value-do-not-print-0123456789"
BRIEFS = ROOT / "sample_course" / "assessments.yaml"


class Clients:
    """Hands out one scripted client per run and records how many were made."""

    def __init__(self, **queues: list[Any]) -> None:
        self.queues = queues
        self.made: list[s.StepClient] = []

    def __call__(self) -> s.StepClient:
        client = s.StepClient(**self.queues)
        self.made.append(client)
        return client


@pytest.fixture
def course(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Course:
    c = Course(tmp_path)
    shutil.copy(BRIEFS, c.config.parent / "assessments.yaml")
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    monkeypatch.chdir(tmp_path)
    return c


def _ask(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, clients: Clients,
         *argv: str) -> tuple[int, str, str]:  # fmt: skip
    monkeypatch.setattr(cli, "_openai_client", clients)
    capsys.readouterr()
    code = cli.main(["ask", *argv])
    cap = capsys.readouterr()
    assert SENTINEL not in cap.out and SENTINEL not in cap.err, "a key value was printed"
    return code, cap.out, cap.err


def test_ask_answers_one_question_with_its_citation_status_and_cost(course, capsys, monkeypatch):
    clients = Clients(**happy())
    code, out, _ = _ask(capsys, monkeypatch, clients, QUESTION, "--config", str(course.config))
    assert code == cli.EXIT_OK, out
    assert "status: answered" in out
    assert DRAFT in out
    label = next(u.label for u in CORPUS.units if u.file_id == S01)
    assert f"[1] {label}" in out
    assert "cost: $" in out
    assert clients.made[0].left() == {}, "every scripted step was used"
    ledger = Path("runs") / "ledger.jsonl"
    assert ledger.is_file() and QUESTION not in ledger.read_text(encoding="utf-8")


def test_ask_json_is_one_object(course, capsys, monkeypatch):
    code, out, _ = _ask(capsys, monkeypatch, Clients(**happy()), QUESTION, "--config",
                        str(course.config), "--json")  # fmt: skip
    assert code == cli.EXIT_OK
    data = json.loads(out)
    assert data["status"] == "answered" and data["text"] == DRAFT
    assert [c["file_id"] for c in data["citations"]] == [S01]
    assert data["cost_usd"] > 0


def test_ask_passes_the_session_scope(course, capsys, monkeypatch):
    clients = Clients(**happy())
    code, _, _ = _ask(capsys, monkeypatch, clients, QUESTION, "--config", str(course.config),
                      "--session", "s01")  # fmt: skip
    assert code == cli.EXIT_OK
    code, _, err = _ask(capsys, monkeypatch, Clients(), QUESTION, "--config", str(course.config),
                        "--session", "s09")  # fmt: skip
    assert code == cli.EXIT_CONFIG and "session" in err


def test_ask_refuses_without_a_key_and_makes_no_client(course, capsys, monkeypatch):
    for value in (None, "  "):
        if value is None:
            monkeypatch.delenv("OPENAI_API_KEY")
        else:
            monkeypatch.setenv("OPENAI_API_KEY", value)
        clients = Clients()
        code, _, err = _ask(capsys, monkeypatch, clients, QUESTION, "--config", str(course.config))
        assert code == cli.EXIT_CONFIG
        assert "OPENAI_API_KEY is not set" in err
        assert clients.made == []


def test_ask_serves_only_the_gate_approved_corpus_unless_ungated(tmp_path, capsys, monkeypatch):
    course = Course(tmp_path, live=False)
    shutil.copy(BRIEFS, course.config.parent / "assessments.yaml")
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    monkeypatch.chdir(tmp_path)
    clients = Clients()
    code, _, err = _ask(capsys, monkeypatch, clients, QUESTION, "--config", str(course.config))
    assert code == cli.EXIT_CONFIG and "--ungated" in err and clients.made == []
    code, out, _ = _ask(capsys, monkeypatch, Clients(**happy()), QUESTION, "--config",
                        str(course.config), "--ungated")  # fmt: skip
    assert code == cli.EXIT_OK and "not gate-approved" in out


def test_ask_refuses_when_the_monthly_budget_cannot_cover_one_question(course, capsys, monkeypatch):
    budget = 150.0  # the test config's monthly_budget_usd
    ledger = Ledger(Path("runs") / "ledger.jsonl", budget)
    ledger.append(purpose="answer", model=s.SOL, tokens_in=0, tokens_cached=0, tokens_out=0,
                  file_search_calls=0, cost_usd=budget - 0.1, run_id="earlier")  # fmt: skip
    clients = Clients()
    code, _, err = _ask(capsys, monkeypatch, clients, QUESTION, "--config", str(course.config))
    assert code == cli.EXIT_COST_CAP
    assert "budget" in err and clients.made == []


def test_ask_holds_each_question_to_the_demo_per_request_cap():
    assert GuardConfig().request_hold_usd == cli.QUESTION_HOLD_USD


def test_ask_missing_corpus_or_config_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    code, _, err = _ask(capsys, monkeypatch, Clients(), QUESTION, "--config",
                        str(tmp_path / "missing.yaml"))  # fmt: skip
    assert code == cli.EXIT_CONFIG and err
    course = Course(tmp_path / "c")
    shutil.copy(BRIEFS, course.config.parent / "assessments.yaml")
    shutil.rmtree(course.store.dir)
    code, _, err = _ask(capsys, monkeypatch, Clients(), QUESTION, "--config", str(course.config),
                        "--ungated")  # fmt: skip
    assert code == cli.EXIT_CONFIG and "ingest" in err
