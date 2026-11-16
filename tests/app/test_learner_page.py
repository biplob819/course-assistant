"""B2.5 learner page through Streamlit's AppTest, over the same fakes as test_chat.py."""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from course_assistant.answer.ask import Assistant
from course_assistant.app import chat as chat_mod
from course_assistant.app import wiring
from course_assistant.app.chat import ALL_SESSIONS, STATE_KEY
from tests.answer.test_ask import CORPUS, QUESTION, S01
from tests.app.rig import KINDS, OWN_KEY, ChatRig, queues
from tests.demo.test_demo import CODE

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "src" / "course_assistant" / "app" / "learner_page.py"
pytestmark = pytest.mark.app


@pytest.fixture(autouse=True)
def keep_main() -> Iterator[None]:
    """AppTest installs the page script as `sys.modules["__main__"]`; a later test that
    spawns processes would then re-run the page in every child and hang. Put it back."""
    main = sys.modules["__main__"]
    yield
    sys.modules["__main__"] = main


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Any]:
    rigs: list[ChatRig] = []

    def make(**kw: Any) -> ChatRig:
        rig = ChatRig(tmp_path, **kw)
        wiring.set_factory(lambda: rig.chat)
        rigs.append(rig)
        return rig

    yield make
    wiring.set_factory(None)


def page() -> AppTest:
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    return at


def labelled(widgets: Any, label: str) -> Any:
    found = [w for w in widgets if w.label == label]
    assert found, f"no widget labelled {label!r}: {[w.label for w in widgets]}"
    return found[0]


def unlock(at: AppTest, code: str = CODE) -> AppTest:
    labelled(at.text_input, "Passkey").input(code)
    labelled(at.button, "Unlock").click()
    at.run()
    return at


def accept(at: AppTest, improve: bool = False) -> AppTest:
    if improve:
        at.checkbox[0].check()
    labelled(at.button, "I agree and continue").click()
    at.run()
    return at


def texts(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [e.value for e in at.error] + [i.value for i in at.info]
    parts += [w.value for w in at.warning]
    parts += [s.value for s in at.success] + [x.value for x in at.text]
    return "\n".join(str(p) for p in parts)


def ask(at: AppTest, question: str) -> AppTest:
    at.chat_input[0].set_value(question)
    at.run()
    return at


def test_page_draws_only_the_passkey_form_until_unlocked(make_rig):
    rig = make_rig()
    at = page()
    assert not at.exception
    assert [w.label for w in at.text_input] == ["Passkey"]
    assert len(at.chat_input) == 0 and len(at.selectbox) == 0 and len(at.expander) == 0
    assert "OpenAI" not in texts(at)  # the own-key field is not drawn either
    labelled(at.text_input, "Passkey").input("000000")
    labelled(at.button, "Unlock").click()
    at.run()
    assert [e.value for e in at.error] == ["Wrong passkey."]
    assert len(at.chat_input) == 0 and rig.owner_client.calls == []
    assert at.text_input[0].value == ""  # the passkey is dropped from the widget state
    assert "000000" not in repr(at.session_state)


def test_page_shows_the_notice_then_the_picker_and_new_conversation(make_rig):
    rig = make_rig(owner=queues("answered", "answered"))
    at = unlock(page())
    assert "Before you start" in [h.value for h in at.header]
    assert "notice version" in texts(at) and len(at.chat_input) == 0
    at = accept(at)
    picker = labelled(at.selectbox, "Search in")
    assert picker.value == ALL_SESSIONS and picker.options[0] == ALL_SESSIONS
    assert picker.options == chat_mod.session_labels(CORPUS)
    labelled(at.button, "New conversation")
    at = ask(at, QUESTION)
    state = at.session_state[STATE_KEY]
    first = state.conversation_id
    picker = labelled(at.selectbox, "Search in")
    picker.select("Session 1")
    at.run()
    state = at.session_state[STATE_KEY]
    assert state.scope == "s01" and state.conversation_id is None
    at = ask(at, QUESTION)
    assert at.session_state[STATE_KEY].conversation_id not in (None, first)
    labelled(at.button, "New conversation").click()
    at.run()
    assert at.session_state[STATE_KEY].turns == []
    assert not at.exception
    assert len(rig.owner_client.calls) > 0


def test_page_shows_an_answer_card_with_source_chips_that_open_the_unit_text(make_rig):
    make_rig()
    at = accept(unlock(page()))
    at = ask(at, QUESTION)
    unit = next(u for u in CORPUS.units if u.file_id == S01)
    body = texts(at)
    assert chat_mod.STATUS_LABELS["answered"][1] in body
    assert chat_mod.AI_LABEL in body and chat_mod.CHECK_NOTE in body
    chips = [e for e in at.expander if e.label == "Source 1"]
    assert len(chips) == 1
    assert [x.value for x in chips[0].text] == [unit.label, unit.text]  # shown literally
    assert not at.exception


def test_page_not_covered_reply_offers_ask_the_teaching_team_and_sends_it(make_rig):
    rig = make_rig(owner=KINDS["not_covered"])
    at = accept(unlock(page()))
    at = ask(at, QUESTION)
    assert Assistant.NOT_COVERED in texts(at)
    labelled(at.button, "Ask the teaching team").click()
    at.run()
    assert "Sent to the teaching team" in texts(at)
    assert [b for b in at.button if b.label == "Ask the teaching team"] == []
    qid = at.session_state[STATE_KEY].turns[-1].card.question_id
    assert rig.store().get_escalation(qid) is not None


def test_page_history_and_delete(make_rig):
    rig = make_rig(owner=queues("answered", "answered"))
    at = accept(unlock(page()))
    at = ask(at, QUESTION)
    assert QUESTION in texts(at)
    labelled(at.checkbox, "Yes, delete all my history").check()
    at.run()
    labelled(at.button, "Delete all my history").click()
    at.run()
    assert "Your history is deleted." in texts(at)
    pid = at.session_state[STATE_KEY].pid
    assert rig.store().conversations_for(pid) == []
    assert not at.exception


def test_page_widgets_all_have_visible_labels(make_rig):
    make_rig(owner=KINDS["not_covered"])
    at = accept(unlock(page()))
    at = ask(at, QUESTION)
    widgets = [*at.text_input, *at.button, *at.checkbox, *at.selectbox]
    assert widgets
    for w in widgets:
        assert isinstance(w.label, str) and len(w.label.strip()) >= 4, w
        if "label_visibility" in {f.name for f in w.proto.DESCRIPTOR.fields}:
            assert w.proto.label_visibility.value in (0, 1), w.label  # VISIBLE (or unset)
    for w in at.text_input:
        assert w.help, w.label  # every text field says what it is for
    assert len(at.chat_input) == 1 and at.chat_input[0].placeholder.strip()


def test_page_source_uses_no_raw_html_and_no_hidden_labels():
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    banned_kw = {"unsafe_allow_html", "label_visibility"}
    banned_calls = {"html", "components", "iframe", "write_stream"}
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword):
            assert node.arg not in banned_kw, node.arg
        if isinstance(node, ast.Attribute):
            assert node.attr not in banned_calls, node.attr
    src = PAGE.read_text(encoding="utf-8")
    assert "<" not in src.replace("<=", "").replace("->", ""), "no markup in the page"


# Streamlit calls that parse Markdown (or a Markdown label) and the names that carry learner,
# model, team or course text: those names may only ever reach st.text (CHT-05)
MARKDOWN_CALLS = {
    "markdown",
    "caption",
    "write",
    "success",
    "info",
    "warning",
    "error",
    "title",
    "header",
    "subheader",
    "expander",
    "button",
    "checkbox",
    "selectbox",
    "text_input",
    "chat_message",
    "toast",
    "form_submit_button",
}
OPEN_TEXT = {"question", "text", "reply", "label", "notice", "chip", "shown", "t", "past"}


@pytest.mark.safety
def test_no_learner_or_model_text_reaches_a_markdown_call():
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    literal_calls = 0
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "st"
        ):
            continue
        if node.func.attr == "text":
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
    assert literal_calls >= 8  # question, answer, chip label and text, notice, history, reply


HOSTILE_ANSWER = (
    "See https://evil.example/login or www.evil.example, $x^2$ :smile: :material/home:."
)
HOSTILE_QUESTION = "Title\n===\n1. one https://evil.example/q $y$ :smile: <b>x</b> **b**"
HOSTILE_REPLY = "Heading\n---\n2. two www.evil.example/r $z$ :material/home: [l](http://e)"
MARKERS = (
    "evil.example",
    "$x^2$",
    "$y$",
    "$z$",
    ":smile:",
    ":material/home:",
    "===",
    "**b**",
    "<b>",
    "[l]",
)


@pytest.mark.safety
def test_every_learner_model_and_team_text_reaches_the_page_literally(make_rig):
    from course_assistant.escalation import escalate, reply
    from tests.answer.test_ask import happy

    rig = make_rig(owner=happy(HOSTILE_ANSWER))
    at = accept(unlock(page()))
    at = ask(at, HOSTILE_QUESTION)
    state = at.session_state[STATE_KEY]
    qid = state.turns[-1].card.question_id
    assert state.turns[-1].card.status == "answered"
    escalate(qid, "learner_request", learner_pid=state.pid, store=rig.store())
    reply(qid, HOSTILE_REPLY, instructor="instructor-1", store=rig.store())
    at.run()
    literal = [x.value for x in at.text]
    for value in (HOSTILE_QUESTION, HOSTILE_ANSWER, HOSTILE_REPLY):
        assert value in literal  # in full, unchanged, through st.text
    assert literal.count(HOSTILE_QUESTION) == 2  # the chat and the history
    parsed = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parsed += [e.label for e in at.expander] + [s.value for s in at.success]
    parsed += [e.value for e in at.error] + [i.value for i in at.info]
    parsed += [h.value for h in [*at.title, *at.header, *at.subheader]]
    parsed += [w.label for w in [*at.button, *at.checkbox, *at.selectbox, *at.text_input]]
    for value in parsed:
        for marker in MARKERS:
            assert marker not in str(value), (marker, value)
    assert not at.exception


def test_a_locked_session_drops_its_own_key_on_the_next_run(make_rig):
    from course_assistant.demo.passkey import hash_passcode
    from tests.demo.test_demo import FAST, OTHER_CODE

    rig = make_rig()
    at = accept(unlock(page()))
    labelled(at.sidebar.text_input, "Your own OpenAI API key (optional)").input(OWN_KEY)
    labelled(at.sidebar.button, "Use my key").click()
    at.run()
    state = at.session_state[STATE_KEY]
    assert state.own_key.is_set
    rig.secret.value = hash_passcode(OTHER_CODE, iterations=FAST)  # the owner rotates it
    at.run()
    assert not state.own_key.is_set
    assert [w.label for w in at.text_input] == ["Passkey"]


def test_page_key_field_appears_only_after_the_passkey_and_clears_on_submit(make_rig):
    rig = make_rig(owner_key=False)
    at = page()
    assert all("OpenAI" not in w.label for w in at.text_input)
    at = accept(unlock(at))
    field = labelled(at.sidebar.text_input, "Your own OpenAI API key (optional)")
    assert field.proto.type == field.proto.PASSWORD
    assert "never stored" in field.help
    field.input(OWN_KEY)
    labelled(at.sidebar.button, "Use my key").click()
    at.run()
    state = at.session_state[STATE_KEY]
    assert state.own_key.is_set
    assert all(w.value != OWN_KEY for w in at.text_input)
    assert OWN_KEY not in repr(at.session_state)  # dropped from the widget state
    assert "Using your own key for this session." in texts(at)
    at = ask(at, QUESTION)
    assert rig.keys_seen == [OWN_KEY] and rig.owner_client.calls == []
    assert OWN_KEY not in repr(at._tree)  # nothing drawn holds it
    labelled(at.sidebar.button, "Sign out").click()
    at.run()
    assert not state.own_key.is_set
    assert [w.label for w in at.text_input] == ["Passkey"]
