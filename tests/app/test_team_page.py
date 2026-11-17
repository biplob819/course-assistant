"""B2.3 teaching-team page through Streamlit's AppTest, over the same fakes as test_team.py."""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from course_assistant.app import team_wiring
from course_assistant.app.chat import STATE_KEY as LEARNER_STATE_KEY
from course_assistant.app.team import STATE_KEY
from course_assistant.escalation import TEAM_LABEL
from tests.answer.test_ask import CORPUS, S01, happy
from tests.app.rig import KINDS
from tests.app.team_rig import NOW, STAFF_CODE, WEEK, TeamRig
from tests.app.test_team import digest_rows
from tests.demo.test_demo import CODE

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "src" / "course_assistant" / "app" / "team_page.py"
LEARNER_PAGE = ROOT / "src" / "course_assistant" / "app" / "learner_page.py"
REPLY = "Pricing is covered next term; the reading list has a chapter on it."
pytestmark = pytest.mark.app


@pytest.fixture(autouse=True)
def keep_main() -> Iterator[None]:
    """AppTest installs the page script as `sys.modules["__main__"]`; put it back so a later
    test that spawns processes does not re-run the page in every child."""
    main = sys.modules["__main__"]
    yield
    sys.modules["__main__"] = main


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Any]:
    def make(**kw: Any) -> TeamRig:
        rig = TeamRig(tmp_path, **kw)
        team_wiring.set_factory(lambda: rig.team)
        return rig

    yield make
    team_wiring.set_factory(None)


def page() -> AppTest:
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    return at


def labelled(widgets: Any, label: str) -> Any:
    found = [w for w in widgets if w.label == label]
    assert found, f"no widget labelled {label!r}: {[w.label for w in widgets]}"
    return found[0]


def unlock(at: AppTest, code: str = STAFF_CODE) -> AppTest:
    labelled(at.text_input, "Teaching-team passkey").input(code)
    labelled(at.button, "Unlock").click()
    at.run()
    return at


def section(at: AppTest, name: str) -> AppTest:
    labelled(at.radio, "Section").set_value(name)
    at.run()
    return at


def texts(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [e.value for e in at.error] + [i.value for i in at.info]
    parts += [w.value for w in at.warning] + [s.value for s in at.success]
    parts += [x.value for x in at.text] + [h.value for h in [*at.title, *at.header]]
    return "\n".join(str(p) for p in parts)


@pytest.mark.safety
def test_page_draws_only_the_staff_passkey_form_and_refuses_the_learner_passkey(make_rig):
    rig = make_rig(owner=KINDS["not_covered"])
    rig.escalated()
    at = page()
    assert not at.exception
    assert [w.label for w in at.text_input] == ["Teaching-team passkey"]
    assert len(at.radio) == 0 and len(at.expander) == 0 and len(at.text_area) == 0
    assert "pricing" not in texts(at)
    at = unlock(at, CODE)  # the learner passkey
    assert [e.value for e in at.error] == ["Wrong passkey."]
    assert len(at.radio) == 0 and "view_queue" not in [e.action for e in rig.audits()]
    assert at.text_input[0].value == "" and CODE not in repr(at.session_state)
    assert LEARNER_STATE_KEY not in at.session_state  # never the learner page's state


def test_page_queue_shows_the_row_and_the_reply_box_sends_a_reply(make_rig):
    rig = make_rig(owner=KINDS["not_covered"])
    qid = rig.escalated()
    at = unlock(page())
    assert labelled(at.radio, "Section").value == "Queue"
    body = texts(at)
    assert "Is there a session on pricing models?" in [x.value for x in at.text]
    assert "Not covered by the course materials" in body and "Waiting for a reply" in body
    assert "The draft names the user first." in [x.value for x in at.text]
    chip = labelled(at.expander, "Source 1")
    unit = next(u for u in CORPUS.units if u.file_id == S01)
    assert [x.value for x in chip.text] == [unit.label, unit.text]
    labelled(at.text_area, "Reply to the learner").input(REPLY)
    labelled(at.button, "Send reply").click()
    at.run()
    assert "Reply sent." in texts(at) and not at.exception
    assert rig.store().get_escalation(qid).reply_text == REPLY  # type: ignore[union-attr]
    turn = rig.learners.chat.history(rig.learners.state)[0].turns[0]
    assert turn.reply == REPLY and turn.reply_label == TEAM_LABEL
    assert "Nothing here." in texts(at)  # nothing left waiting
    labelled(at.selectbox, "Show").select("All")
    at.run()
    assert REPLY in [x.value for x in at.text]
    labelled(at.button, "Flag a gap in the materials").click()
    at.run()
    assert rig.store().get_escalation(qid).status == "material_gap"  # type: ignore[union-attr]


def test_page_digest_tab_shows_rows_or_no_digest(make_rig):
    rig = make_rig()
    rig.store().put_digest(WEEK, digest_rows(), k=5, at=NOW)
    at = section(unlock(page()), "Digest")
    assert labelled(at.selectbox, "Week").value == WEEK
    lines = [x.value for x in at.text]
    assert "Session 1 | Naming the user problem | 6 learners | 1 flagged gaps" in lines
    labelled(at.selectbox, "Week").select("2026-W46")
    at.run()
    assert "No digest for this week." in texts(at)
    assert [e.action for e in rig.audits()].count("view_digest") == 2
    assert not at.exception


def test_page_materials_are_read_only_with_the_corpus_version(make_rig):
    rig = make_rig()
    at = section(unlock(page()), "Materials")
    lines = [x.value for x in at.text]
    assert f"Live corpus version: {CORPUS.corpus_version}" in lines
    assert any(x.startswith("m-s01-slides | Session 1 | slides | course/m-s01-slides.pdf")
               for x in lines)  # fmt: skip
    assert "1 live units have no manifest entry" in texts(at)
    # nothing to change: no input, no upload, no action button but sign out
    assert len(at.text_input) == 0 and len(at.text_area) == 0
    assert [b.label for b in at.button] == ["Sign out"]
    assert "view_manifest" in [e.action for e in rig.audits()]


def test_page_pause_switch_needs_its_confirm_box(make_rig):
    rig = make_rig()
    at = section(unlock(page()), "Pause switches")
    button = labelled(at.button, "Pause Session 1")
    assert button.disabled
    labelled(at.checkbox, "Yes, pause Session 1").check()
    at.run()
    button = labelled(at.button, "Pause Session 1")
    assert not button.disabled
    button.click()
    at.run()
    assert rig.store().is_paused("s01") and not rig.store().is_paused(None)
    assert "**Session 1**: ‖ Paused" in texts(at)
    assert labelled(at.button, "Resume Session 1").disabled  # a fresh confirm for each change
    events = [(e.action, e.target) for e in rig.audits() if e.action in ("pause", "resume")]
    assert events == [("pause", "s01")]
    assert not at.exception


def test_page_ops_tile_shows_the_numbers(make_rig):
    rig = make_rig(owner=happy())
    learner = rig.learners.ready()
    rig.learners.chat.ask(learner, "What does the course say about naming the user's problem?")
    at = section(unlock(page()), "Ops")
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Questions"] == "1"
    assert metrics["Sent to the teaching team"] == "0%"
    assert metrics["Answer time, p95"].endswith(" s")
    assert set(metrics) == {"Questions", "Sent to the teaching team", "Answer time, p95",
                            "Spend to date (USD)"}  # fmt: skip
    assert "Answer from the course materials: 1" in texts(at)
    assert [e.action for e in rig.audits()][-1] == "view_ops"


def test_page_sign_out_locks_the_page(make_rig):
    rig = make_rig()
    at = unlock(page())
    labelled(at.sidebar.button, "Sign out").click()
    at.run()
    assert [w.label for w in at.text_input] == ["Teaching-team passkey"]
    assert rig.team.locked_reason(at.session_state[STATE_KEY]) is not None
    assert [e.action for e in rig.audits()][-1] == "staff_sign_out"


def test_page_widgets_all_have_visible_labels(make_rig):
    rig = make_rig(owner=KINDS["not_covered"])
    rig.escalated()
    at = page()
    assert at.text_input[0].help
    at = unlock(at)
    seen = 0
    for name in ("Queue", "Digest", "Materials", "Pause switches", "Ops"):
        at = section(at, name)
        widgets = [*at.text_input, *at.text_area, *at.button, *at.checkbox, *at.selectbox,
                   *at.radio]  # fmt: skip
        for w in widgets:
            seen += 1
            assert isinstance(w.label, str) and len(w.label.strip()) >= 4, w
            if "label_visibility" in {f.name for f in w.proto.DESCRIPTOR.fields}:
                assert w.proto.label_visibility.value in (0, 1), w.label
        for w in at.text_area:
            assert w.help
    assert seen > 10 and not at.exception


def test_page_source_uses_no_raw_html_and_no_hidden_labels():
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword):
            assert node.arg not in {"unsafe_allow_html", "label_visibility"}, node.arg
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"html", "components", "iframe", "write_stream"}, node.attr
    src = PAGE.read_text(encoding="utf-8")
    assert "<" not in src.replace("<=", "").replace("->", ""), "no markup in the page"


@pytest.mark.safety
def test_the_pages_never_share_a_gate_or_a_state_key():
    team_src = PAGE.read_text(encoding="utf-8")
    learner_src = LEARNER_PAGE.read_text(encoding="utf-8")
    assert "app.team" not in learner_src and "team_wiring" not in learner_src
    assert "app.chat" not in team_src and "app import wiring" not in team_src
    assert STATE_KEY != LEARNER_STATE_KEY
    wiring_src = (ROOT / "src" / "course_assistant" / "app" / "team_wiring.py").read_text(
        encoding="utf-8"
    )
    assert "PasskeyGate(staff_hash(" in wiring_src and "PasskeyGate(env_hash(" not in wiring_src


MARKDOWN_CALLS = {"markdown", "caption", "write", "success", "info", "warning", "error",
                  "title", "header", "subheader", "expander", "button", "checkbox",
                  "selectbox", "text_input", "text_area", "radio", "toast", "metric",
                  "form_submit_button"}  # fmt: skip
OPEN_TEXT = {"question", "draft", "reply", "text", "label", "chip", "topic", "path", "licence",
             "cohort", "manifest_id", "approved_by", "kind"}  # fmt: skip


@pytest.mark.safety
def test_no_learner_model_or_course_text_reaches_a_markdown_call():
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    literal_calls = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)):  # fmt: skip
            continue
        if node.func.value.id == "st" and node.func.attr == "text":
            literal_calls += 1
            continue
        if node.func.attr not in MARKDOWN_CALLS:
            continue
        names = set()
        for arg in [*node.args, *(k.value for k in node.keywords if k.arg != "key")]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
        assert not names & OPEN_TEXT, (node.func.attr, ast.unparse(node))
    assert literal_calls >= 8


HOSTILE_QUESTION = "Title\n===\n1. one https://evil.example/q $y$ :smile: <b>x</b> **b**"
HOSTILE_REPLY = "Heading\n---\n2. two www.evil.example/r $z$ :material/home: [l](http://e)"
MARKERS = ("evil.example", "$y$", "$z$", ":smile:", ":material/home:", "===", "**b**", "<b>",
           "[l]")  # fmt: skip


@pytest.mark.safety
def test_every_learner_and_team_text_reaches_the_page_literally(make_rig):
    rig = make_rig(owner=KINDS["not_covered"])
    qid = rig.escalated(HOSTILE_QUESTION)
    rig.team.reply(rig.unlock(), qid, HOSTILE_REPLY)
    at = unlock(page())
    labelled(at.selectbox, "Show").select("All")
    at.run()
    literal = [x.value for x in at.text]
    assert HOSTILE_QUESTION in literal and HOSTILE_REPLY in literal
    parsed = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parsed += [e.label for e in at.expander] + [s.value for s in at.success]
    parsed += [e.value for e in at.error] + [i.value for i in at.info]
    parsed += [h.value for h in [*at.title, *at.header, *at.subheader]]
    parsed += [w.label for w in [*at.button, *at.checkbox, *at.selectbox, *at.text_area]]
    for value in parsed:
        for marker in MARKERS:
            assert marker not in str(value), (marker, value)
    assert not at.exception
