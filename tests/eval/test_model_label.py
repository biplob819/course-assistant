"""The model-panel labeller (scripts/model_label.py, OWNER D-76): its labellers are never the
judges, two agreeing votes need no tie-break, a split goes to the tie-break model, and the
tuning log records who labelled. Scripted models only: no API call."""

from __future__ import annotations

from typing import Any

from scripts import calibrate as C
from scripts import model_label as M

from course_assistant.eval.calibrate import _logged_by
from course_assistant.eval.tuning import log_calibration_scored, read_log


class _Scripted(M.Panel):
    def __init__(self, answers: dict[str, dict[str, Any] | None]) -> None:  # no clients
        self.answers, self.asked = answers, []

    def ask(self, model, system, user, schema):  # type: ignore[override]
        self.asked.append(model)
        return self.answers[model]


def test_the_labellers_are_never_the_judges():
    assert not set(M.PANEL) & set(M.JUDGES)
    assert M.TIEBREAK not in M.JUDGES
    assert not {m.split("@")[0] for m in (*M.PANEL, M.TIEBREAK)} & set(M.JUDGES)  # D-79
    assert set(M.JUDGES) == set(C.JUDGES)
    assert all(m in C.MODEL_PANEL for m in (*M.PANEL, M.TIEBREAK))


def test_agreement_needs_no_tiebreak():
    p = _Scripted({m: {"label": "C"} for m in (*M.PANEL, M.TIEBREAK)})
    out = p.vote("rules", "item", M.I_SCHEMA, "k1")
    assert out["label"] == "C" and M.TIEBREAK not in p.asked


def test_a_split_goes_to_the_tiebreak():
    fable, terra = M.PANEL
    p = _Scripted({fable: {"label": "V"}, terra: {"label": "C"}, M.TIEBREAK: {"label": "V"}})
    assert p.vote("rules", "item", M.I_SCHEMA, "k1")["label"] == "V"
    assert p.asked[-1] == M.TIEBREAK


def test_a_missing_vote_goes_to_the_tiebreak_and_extraction_error_is_a_majority():
    fable, terra = M.PANEL
    p = _Scripted({fable: None, terra: {"label": "unsupported", "extraction_error": True},
                   M.TIEBREAK: {"label": "unsupported", "extraction_error": True}})  # fmt: skip
    out = p.vote("rules", "item", M.G_SCHEMA, "k1")
    assert out["label"] == "unsupported" and out["extraction_error"] is True


def test_one_surviving_vote_is_never_a_label():
    fable, terra = M.PANEL
    p = _Scripted({fable: None, terra: {"label": "C"}, M.TIEBREAK: None})
    assert p.vote("rules", "item", M.I_SCHEMA, "k1")["label"] is None


def test_a_three_way_split_with_one_missing_is_unlabelled():
    fable, terra = M.PANEL
    p = _Scripted({fable: {"label": "V"}, terra: {"label": "C"}, M.TIEBREAK: None})
    assert p.vote("rules", "item", M.I_SCHEMA, "k1")["label"] is None


def test_no_vote_at_all_leaves_the_item_unlabelled():
    p = _Scripted({m: None for m in (*M.PANEL, M.TIEBREAK)})
    assert p.vote("rules", "item", M.I_SCHEMA, "k1")["label"] is None


def test_the_log_records_who_labelled(tmp_path):
    log = tmp_path / "log.jsonl"
    kw = {"calibration": "integrity", "digest": "d1", "rubric_hash": "h", "run": "r1",
          "heldout_cases": ["c1"]}  # fmt: skip
    log_calibration_scored(log, **kw, labeller=C.MODEL_PANEL)
    assert read_log(log)[-1]["labeller"] == C.MODEL_PANEL
    assert _logged_by(log, "integrity", "d1", "h", "r1") == C.MODEL_PANEL
    assert _logged_by(log, "integrity", "d2", "h", "r1") is None
