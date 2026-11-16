"""B2.5 learner chat logic: CHT-01 to CHT-06 (the page itself is in test_learner_page.py)."""

from __future__ import annotations

import copy
import html
import inspect
import logging
import pickle
import re
from pathlib import Path
from typing import Any

import pytest

from course_assistant.answer import golden
from course_assistant.answer.ask import Assistant
from course_assistant.app import chat as chat_mod
from course_assistant.app.chat import (
    ALL_SESSIONS,
    STATUS_LABELS,
    Card,
    ChatState,
    LearnerChat,
    OwnKey,
    card_for,
    plain,
    scope_for,
    session_labels,
)
from course_assistant.demo._errors import GuardRefused
from course_assistant.demo.passkey import NEEDED, hash_passcode
from course_assistant.escalation import TEAM_LABEL, reply, team_queue
from course_assistant.privacy import NOTICE, NOTICE_VERSION
from course_assistant.schemas.request import Citation
from tests.answer.test_ask import CORPUS, QUESTION, S01, S03, data
from tests.app.rig import KINDS, OWN_KEY, ChatRig, queues
from tests.demo.test_demo import DAY, FAST, OTHER_CODE

REPLY = "Yes, slide 3 is the one to revisit before the quiz."


def unit(file_id: str) -> Any:
    return next(u for u in CORPUS.units if u.file_id == file_id)


def answer_calls(client: Any) -> list[dict[str, Any]]:
    return [kw for step, kw in client.calls if step == "answer"]


def session_filter(call: dict[str, Any]) -> list[Any]:
    flt = call["tools"][0]["filters"]
    parts = flt["filters"] if flt["type"] == "and" else [flt]
    return [f["value"] for f in parts if f["key"] == "session"]


# ======================= CHT-01 session picker and new conversation =======================


def test_session_options_start_with_all_sessions_and_scope_reaches_the_answer_filter(tmp_path):
    rig = ChatRig(tmp_path, owner=queues("answered", "answered"))
    st = rig.ready()
    labels = session_labels(CORPUS)
    assert labels == [ALL_SESSIONS, "Session 1", "Session 2", "Session 3"]
    assert rig.chat.session_options(st) == labels
    assert st.scope is None and rig.chat.scope_label(st) == ALL_SESSIONS  # the default
    rig.chat.ask(st, QUESTION)
    assert session_filter(answer_calls(rig.owner_client)[0]) == []  # no session filter
    rig.chat.choose_scope(st, "Session 1")
    assert st.scope == "s01" and rig.chat.scope_label(st) == "Session 1"
    card = rig.chat.ask(st, QUESTION)
    assert card.status == "answered"
    assert session_filter(answer_calls(rig.owner_client)[1]) == ["s01"]
    assert scope_for(ALL_SESSIONS, CORPUS) is None and scope_for("Session 3", CORPUS) == "s03"


def test_new_conversation_closes_the_open_one_and_clears_the_window(tmp_path):
    rig = ChatRig(tmp_path, owner=queues("answered", "answered", "answered"))
    st = rig.ready()
    rig.chat.ask(st, QUESTION)
    first = st.conversation_id
    rig.chat.ask(st, "And what comes after that?")
    assert st.conversation_id == first and len(st.turns) == 2
    route_calls = [kw for step, kw in rig.owner_client.calls if step == "route"]
    assert data(route_calls[0])["window"] == [] and data(route_calls[1])["window"] != []
    rig.chat.new_conversation(st)
    assert st.conversation_id != first and st.turns == []
    closed = rig.store().get_conversation(first)
    assert closed is not None and closed.closed_at is not None
    rig.chat.ask(st, QUESTION)
    route_calls = [kw for step, kw in rig.owner_client.calls if step == "route"]
    assert data(route_calls[2])["window"] == []  # an empty window again
    assert {c.conversation_id for c in rig.store().conversations_for(st.pid)} >= {
        first, st.conversation_id}


def test_changing_the_scope_starts_a_new_conversation(tmp_path):
    rig = ChatRig(tmp_path, owner=queues("answered", "answered"))
    st = rig.ready()
    rig.chat.ask(st, QUESTION)
    first = st.conversation_id
    rig.chat.choose_scope(st, ALL_SESSIONS)  # the same scope: nothing changes
    assert st.conversation_id == first and len(st.turns) == 1
    rig.chat.choose_scope(st, "Session 1")
    assert st.conversation_id is None and st.turns == []
    rig.chat.ask(st, QUESTION)
    assert st.conversation_id != first
    conv = rig.store().get_conversation(st.conversation_id)
    assert conv is not None and conv.session_scope == "s01"


@pytest.mark.safety
@pytest.mark.parametrize("label", ["Session 9", "s01", "", "all sessions", "Session 1 "])
def test_an_unknown_scope_label_is_refused_and_nothing_is_asked(tmp_path, label):
    rig = ChatRig(tmp_path)
    st = rig.ready()
    with pytest.raises(GuardRefused):
        rig.chat.choose_scope(st, label)
    with pytest.raises(ValueError):
        scope_for(label, CORPUS)
    assert st.scope is None and rig.owner_client.calls == []


# ======================= CHT-02 answer card =======================


def test_an_answered_card_has_chips_with_the_exact_unit_text_ai_label_and_check_note(tmp_path):
    rig = ChatRig(tmp_path)
    st = rig.ready()
    card = rig.chat.ask(st, QUESTION)
    u = unit(S01)
    assert card.status == "answered" and card.status_label == STATUS_LABELS["answered"][1]
    assert card.ai_label == chat_mod.AI_LABEL and "AI" in card.ai_label
    assert card.check_note == chat_mod.CHECK_NOTE and "check the cited" in card.check_note.lower()
    assert [c.label for c in card.chips] == [u.label]
    chip = card.chips[0]
    assert (chip.file_id, chip.unit_index, chip.session, chip.text) == (
        u.file_id, u.unit_index, u.session, u.text)  # the exact corpus text (D-3)
    assert chip.found
    assert st.turns[-1].card == card and st.turns[-1].question == QUESTION


@pytest.mark.golden
def test_every_golden_replay_answer_renders_the_card_its_status_calls_for():
    fx = golden.load()
    seen = set()
    for case in fx.cases:
        answer = golden.replay_case(fx, case)
        assert answer == case.expected
        card = card_for(answer, fx.corpus)
        seen.add(card.status)
        assert card.text and card.status_label and card.icon
        if case.paused:
            assert card.status == "paused" and not card.can_escalate
            continue
        assert card.status == answer.status
        if answer.status in ("answered", "hint_only"):
            assert card.ai_label and card.check_note
            assert [(c.file_id, c.unit_index) for c in card.chips] == [
                (c.file_id, c.unit_index) for c in answer.citations]
            for chip in card.chips:
                u = next(x for x in fx.corpus.units if x.file_id == chip.file_id)
                assert chip.found and chip.text == u.text and chip.label == u.label
        else:
            assert card.chips == () and card.ai_label is None and card.check_note is None
        assert card.can_escalate == (answer.status == "not_covered"
                                     and answer.text == Assistant.NOT_COVERED)  # fmt: skip
    assert seen >= {"answered", "not_covered", "hint_only", "handed_over", "error", "paused"}


def test_a_citation_missing_from_the_corpus_says_so_instead_of_guessing(tmp_path):
    rig = ChatRig(tmp_path)
    st = rig.ready()
    card = rig.chat.ask(st, QUESTION)
    gone = Citation(file_id="file-s09-slides-1", unit_index=99, label="Session 9 slides, slide 1",
                    session="s09")  # fmt: skip
    turn = st.turns[-1].turn
    assert turn is not None
    moved = turn.answer.model_copy(update={"citations": (*turn.answer.citations, gone)})
    again = card_for(moved, CORPUS)
    assert again.chips[0] == card.chips[0]
    missing = again.chips[1]
    assert not missing.found and missing.text == chat_mod.MISSING_UNIT
    assert missing.label == gone.label
    # a file id that exists but under another index is not guessed either
    wrong = Citation(file_id=S01, unit_index=4, label="x", session="s01")
    odd = card_for(turn.answer.model_copy(update={"citations": (wrong,)}), CORPUS)
    assert not odd.chips[0].found


@pytest.mark.safety
def test_a_not_covered_card_never_shows_the_kept_draft_or_its_sources(tmp_path):
    rig = ChatRig(tmp_path, owner=KINDS["not_covered"])
    st = rig.ready()
    card = rig.chat.ask(st, QUESTION)
    turn = st.turns[-1].turn
    assert turn is not None and turn.draft is not None
    assert card.status == "not_covered" and card.text == Assistant.NOT_COVERED
    assert card.chips == () and card.ai_label is None
    assert turn.draft.text not in repr(card) and unit(S01).text not in repr(card)


# ======================= CHT-03 not covered and ask the teaching team =======================


@pytest.mark.parametrize(("kind", "offer"), [("answered", False), ("not_covered", True),
                                             ("graded_work", False), ("admin", False),
                                             ("off_topic", False), ("injection", False),
                                             ("error", False)])  # fmt: skip
def test_only_the_not_covered_text_offers_ask_the_teaching_team(tmp_path, kind, offer):
    rig = ChatRig(tmp_path, owner=KINDS[kind])
    st = rig.ready()
    card = rig.chat.ask(st, QUESTION)
    assert card.can_escalate is offer
    if kind in ("off_topic", "injection"):
        assert card.status == "not_covered" and card.text != Assistant.NOT_COVERED


def test_ask_the_teaching_team_queues_the_turn_with_its_kept_draft(tmp_path):
    rig = ChatRig(tmp_path, owner=KINDS["not_covered"])
    st = rig.ready()
    card = rig.chat.ask(st, QUESTION)
    e = rig.chat.escalate(st, card.question_id)
    assert e.reason == "not_covered" and e.status == "open"
    assert e.draft is not None and e.draft.text == "The draft names the user first."
    assert st.turns[-1].escalated
    items = team_queue(rig.store(), viewer="instructor-1")
    assert [i.question_id for i in items] == [card.question_id]


def test_a_second_tap_returns_the_first_escalation(tmp_path):
    rig = ChatRig(tmp_path, owner=KINDS["not_covered"])
    st = rig.ready()
    card = rig.chat.ask(st, QUESTION)
    first = rig.chat.escalate(st, card.question_id)
    second = rig.chat.escalate(st, card.question_id)
    assert second == first
    assert len(team_queue(rig.store(), viewer="instructor-1")) == 1


def test_a_handed_over_turn_is_queued_without_a_tap(tmp_path):
    rig = ChatRig(tmp_path, owner=KINDS["admin"])
    st = rig.ready()
    card = rig.chat.ask(st, "Can I move my deadline?")
    assert card.status == "handed_over" and card.text == Assistant.HANDED_OVER
    assert card.status_label == "Sent to the teaching team" and not card.can_escalate
    e = rig.store().get_escalation(card.question_id)
    assert e is not None and e.reason == "admin"


@pytest.mark.safety
def test_escalating_a_question_not_on_this_page_or_another_learners_is_refused(tmp_path):
    rig = ChatRig(tmp_path, owner=queues("not_covered", "answered"))
    other = rig.ready(ChatState.new())
    theirs = rig.chat.ask(other, QUESTION)
    st = rig.ready()
    with pytest.raises(GuardRefused):
        rig.chat.escalate(st, theirs.question_id)  # another learner's question
    with pytest.raises(GuardRefused):
        rig.chat.escalate(st, "q-0123456789abcdef")  # no such question
    mine = rig.chat.ask(st, QUESTION)
    with pytest.raises(GuardRefused):
        rig.chat.escalate(st, mine.question_id)  # answered: no not-covered offer to take
    assert team_queue(rig.store(), viewer="instructor-1") == ()


# ======================= CHT-04 notice, history, delete =======================


def test_the_notice_shows_until_accepted_and_asking_is_refused_before(tmp_path):
    rig = ChatRig(tmp_path, owner=queues("answered"))
    st = rig.state
    rig.unlock(st)
    assert rig.chat.needs_notice(st)
    assert rig.chat.notice(st) == NOTICE and NOTICE_VERSION in NOTICE
    with pytest.raises(GuardRefused, match="notice"):
        rig.chat.ask(st, QUESTION)
    assert rig.owner_client.calls == [] and rig.store().conversations_for(st.pid) == []
    consent = rig.chat.accept_notice(st)
    assert consent.purposes == ("answer_questions",)  # improve_course is optional, off
    assert not rig.chat.needs_notice(st)
    assert rig.chat.ask(st, QUESTION).status == "answered"
    other = rig.ready(ChatState.new(), improve=True)
    consents = rig.store().consents_for(other.pid)
    assert consents[-1].purposes == ("answer_questions", "improve_course")


def test_history_shows_this_learners_turns_and_teaching_team_replies_labelled(tmp_path):
    rig = ChatRig(tmp_path, owner=queues("answered", "not_covered", "answered"))
    other = rig.ready(ChatState.new())
    rig.chat.ask(other, "Someone else's question about slides?")
    st = rig.ready()
    card = rig.chat.ask(st, QUESTION)
    rig.chat.escalate(st, card.question_id)
    assert card.question_id is not None
    reply(card.question_id, REPLY, instructor="instructor-1", store=rig.store())
    rig.chat.ask(st, "And the second one?")
    past = rig.chat.history(st)
    assert len(past) == 1
    turns = past[0].turns
    assert [t.question for t in turns] == [QUESTION, "And the second one?"]
    assert turns[0].status_label == STATUS_LABELS["not_covered"][1]
    assert turns[0].text is None  # B2.2 keeps no fixed not-covered text: none is guessed
    assert turns[0].handover == "Replied by the teaching team"
    assert turns[0].reply == REPLY and turns[0].reply_label == TEAM_LABEL
    assert turns[1].status_label == STATUS_LABELS["answered"][1] and turns[1].reply is None
    assert "Someone else" not in repr(past)
    assert rig.chat.replies_waiting(st) == 1 and rig.chat.replies_waiting(other) == 0


def test_withdrawing_consent_shows_the_notice_again_and_stops_asking(tmp_path):
    rig = ChatRig(tmp_path, owner=queues("answered"))
    st = rig.ready()
    rig.chat.ask(st, QUESTION)
    assert rig.chat.withdraw(st) == 1
    assert rig.chat.needs_notice(st)
    with pytest.raises(GuardRefused, match="notice"):
        rig.chat.ask(st, QUESTION)
    assert len(rig.chat.history(st)[0].turns) == 1  # withdrawing is not deleting


@pytest.mark.privacy
def test_delete_removes_app_rows_and_trace_lines_and_starts_a_fresh_conversation(tmp_path):
    rig = ChatRig(tmp_path, owner=queues("answered", "answered", "answered"))
    st = rig.ready()
    rig.chat.ask(st, QUESTION)
    first = st.conversation_id
    rig.chat.new_conversation(st)
    rig.chat.ask(st, "Second conversation question?")
    second = st.conversation_id
    assert st.pid in rig.sink.path.read_text(encoding="utf-8")
    req = rig.chat.delete(st, first)
    assert req.conversation_id == first and req.traces_deleted_at is not None
    assert [c.conversation.conversation_id for c in rig.chat.history_raw(st)] == [second]
    assert st.conversation_id == second  # another conversation's delete leaves this one open
    req = rig.chat.delete(st)  # everything
    assert req.conversation_id is None and req.traces_deleted_at is not None
    assert rig.chat.history(st) == ()
    assert st.pid not in rig.sink.path.read_text(encoding="utf-8")
    assert st.conversation_id is None and st.turns == []
    assert rig.chat.ask(st, QUESTION).status == "answered"  # a fresh conversation
    with pytest.raises(GuardRefused):
        rig.chat.delete(st, "c-0123456789abcdef")  # not this learner's conversation


# ======================= CHT-05 accessibility =======================


def test_every_status_has_its_own_text_label_and_icon():
    statuses = ("answered", "not_covered", "hint_only", "handed_over", "error", "paused",
                "refused")  # fmt: skip
    assert set(STATUS_LABELS) == set(statuses)
    icons = [STATUS_LABELS[s][0] for s in statuses]
    labels = [STATUS_LABELS[s][1] for s in statuses]
    assert len(set(labels)) == len(labels) and all(len(x) >= 5 for x in labels)
    assert len(set(icons)) == len(icons) and all(i.strip() for i in icons)
    # an icon is a text character, never a colour name or markup
    assert not any(c in "<>:#" for i in icons for c in i)
    card = card_for(happy_answer(), CORPUS)
    assert card.heading == f"{card.icon} {card.status_label}"


@pytest.mark.safety
@pytest.mark.parametrize("text", ["<script>alert(1)</script>", "**bold** _x_ [link](http://e)",
                                  "![img](http://e/x.png)", "<img src=x onerror=alert(1)>",
                                  "# Heading\n---\n> quote", "`code` ~~s~~ | a | b |"])  # fmt: skip
def test_learner_and_answer_text_cannot_inject_markup(text):
    out = plain(text)
    assert "<" not in out and ">" not in out  # no tag can open
    unescaped = re.sub(r"\\(.)", "", out)  # drop every backslash-escaped character
    assert not any(ch in unescaped for ch in "*_[]()!#`~|-")  # no live Markdown remains
    assert html.unescape(re.sub(r"\\(.)", r"\1", out)) == text  # nothing is lost


def happy_answer() -> Any:
    fx = golden.load()
    case = next(c for c in fx.cases if c.expected_status == "answered")
    assert case.expected is not None
    return case.expected.model_copy(update={"citations": (Citation(
        file_id=S03, unit_index=4, label=unit(S03).label, session="s03"),)})


# ======================= CHT-06 own key and the passkey =======================


def test_an_own_key_is_used_for_the_request_and_holds_nothing_against_the_budget(tmp_path):
    rig = ChatRig(tmp_path, owner_key=False)
    st = rig.ready()
    card = rig.chat.ask(st, QUESTION)
    assert card.status == "refused" and "own OpenAI key" in card.text  # no owner key here
    rig.chat.set_own_key(st, f"  {OWN_KEY}  ")
    assert st.own_key.is_set
    card = rig.chat.ask(st, QUESTION)
    assert card.status == "answered"
    assert rig.keys_seen == [OWN_KEY] and rig.owner_client.calls == []
    assert rig.counter.spent(DAY) == 0
    rig.chat.forget_own_key(st)
    assert not st.own_key.is_set
    for bad in ("", "   ", "sk with spaces", "x" * 201):
        with pytest.raises(GuardRefused):
            rig.chat.set_own_key(st, bad)
        assert not st.own_key.is_set


def test_sign_out_clears_the_key_and_locks_every_method(tmp_path):
    rig = ChatRig(tmp_path)
    st = rig.ready()
    rig.chat.set_own_key(st, OWN_KEY)
    rig.chat.ask(st, QUESTION)
    rig.chat.sign_out(st)
    assert not st.own_key.is_set and st.own_key.reveal() is None and st.turns == []
    assert rig.chat.locked_reason(st) == NEEDED
    for call in (lambda: rig.chat.ask(st, QUESTION), lambda: rig.chat.history(st),
                 lambda: rig.chat.set_own_key(st, OWN_KEY)):  # fmt: skip
        with pytest.raises(GuardRefused, match="passkey"):
            call()
    rig.unlock(st)
    assert not st.own_key.is_set  # signing in again does not bring the key back


def _calls(rig: ChatRig, st: ChatState) -> dict[str, Any]:
    """One call per public LearnerChat method that must refuse without the passkey."""
    return {
        "ask": lambda: rig.chat.ask(st, QUESTION),
        "accept_notice": lambda: rig.chat.accept_notice(st),
        "choose_scope": lambda: rig.chat.choose_scope(st, "Session 1"),
        "delete": lambda: rig.chat.delete(st),
        "escalate": lambda: rig.chat.escalate(st, "q-0123456789abcdef"),
        "forget_own_key": lambda: rig.chat.forget_own_key(st),
        "history": lambda: rig.chat.history(st),
        "history_raw": lambda: rig.chat.history_raw(st),
        "needs_notice": lambda: rig.chat.needs_notice(st),
        "new_conversation": lambda: rig.chat.new_conversation(st),
        "notice": lambda: rig.chat.notice(st),
        "replies_waiting": lambda: rig.chat.replies_waiting(st),
        "scope_label": lambda: rig.chat.scope_label(st),
        "session_options": lambda: rig.chat.session_options(st),
        "set_own_key": lambda: rig.chat.set_own_key(st, OWN_KEY),
        "withdraw": lambda: rig.chat.withdraw(st),
    }


EXEMPT = {"unlock", "sign_out", "locked_reason"}  # the gate itself


@pytest.mark.safety
def test_no_method_answers_or_reads_without_the_passkey_even_with_a_key(tmp_path):
    rig = ChatRig(tmp_path)
    st = rig.state
    st.own_key.set(OWN_KEY)  # a visitor holding a key, but no passkey
    calls = _calls(rig, st)
    public = {n for n, _ in inspect.getmembers(LearnerChat, inspect.isfunction)
              if not n.startswith("_")}  # fmt: skip
    assert public == set(calls) | EXEMPT  # a new method must be added here, and refuse
    for name, call in calls.items():
        with pytest.raises(GuardRefused, match="passkey") as err:
            call()
        assert err.value.reason == NEEDED, name
    assert rig.owner_client.calls == [] and rig.keys_seen == []
    assert rig.store().conversations_for(st.pid) == [] and rig.store().consents_for(st.pid) == []
    assert rig.counter.spent(DAY) == 0 and not rig.sink.path.exists()
    # unlocked, then the owner rotates the passkey: every method refuses again
    rig.ready()
    rig.chat.ask(st, QUESTION)
    rig.secret.value = hash_passcode(OTHER_CODE, iterations=FAST)
    for name, call in _calls(rig, st).items():
        with pytest.raises(GuardRefused, match="passkey"):
            call()
        assert name
    # a wrong passkey never unlocks, own key or not
    other = ChatState.new()
    other.own_key.set(OWN_KEY)
    with pytest.raises(GuardRefused, match="Wrong"):
        rig.chat.unlock(other, "000000")
    with pytest.raises(GuardRefused, match="passkey"):
        rig.chat.ask(other, QUESTION)


@pytest.mark.safety
def test_the_own_key_never_reaches_logs_traces_store_files_or_reprs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    boom: list[str] = []

    def factory(key: str) -> Any:
        if boom:
            raise RuntimeError(f"provider refused key {key}")  # a library echoing the key
        return rig.visitor_responder()

    rig = ChatRig(tmp_path, owner_key=False, visitor=queues("answered", "not_covered"),
                  key_factory=factory)  # fmt: skip
    st = rig.ready()
    rig.chat.set_own_key(st, OWN_KEY)
    cards: list[Card] = [rig.chat.ask(st, QUESTION), rig.chat.ask(st, "What about slide 9?")]
    rig.chat.escalate(st, cards[1].question_id)
    boom.append("on")
    cards.append(rig.chat.ask(st, "One more?"))
    assert cards[2].status == "error" and cards[2].text == Assistant.ERROR_MESSAGE
    assert rig.keys_seen == [OWN_KEY] * 3
    texts = [repr(st), str(st), repr(st.own_key), str(st.own_key), repr(rig.chat),
             repr(rig.chat.history(st)), *(repr(c) for c in cards), caplog.text,
             *(repr(r.args) + str(r.msg) for r in caplog.records)]  # fmt: skip
    rig.chat.delete(st, rig.chat.history_raw(st)[0].conversation.conversation_id)
    rig.chat.sign_out(st)
    for t in texts:
        assert OWN_KEY not in t and "OWNKEY" not in t
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"OWNKEY" not in path.read_bytes(), path.name
    with pytest.raises(TypeError):
        pickle.dumps(OwnKey(OWN_KEY))
    with pytest.raises(TypeError):
        copy.copy(OwnKey(OWN_KEY))
    with pytest.raises(TypeError):
        copy.deepcopy(ChatState.new())  # the state holds an OwnKey, so it is never copied out
    assert st.own_key.reveal() is None


def test_chat_state_is_random_per_browser_session_and_shows_no_text():
    a, b = ChatState.new(), ChatState.new()
    assert a.pid != b.pid and a.token != b.token and len(a.pid) == 16
    assert int(a.pid, 16) >= 0
    assert repr(a) == "ChatState()" and str(a) == "ChatState()"


def test_per_thread_store_gives_each_thread_its_own_connection(tmp_path):
    import threading

    stores = chat_mod.PerThreadStore(tmp_path / "s.sqlite")
    here = stores()
    assert stores() is here
    seen: list[Any] = []
    t = threading.Thread(target=lambda: seen.append(stores()))
    t.start()
    t.join()
    assert seen and seen[0] is not here


def test_sessions_labels_come_from_the_corpus_in_order():
    assert session_labels(CORPUS)[0] == ALL_SESSIONS
    assert [scope_for(x, CORPUS) for x in session_labels(CORPUS)] == [None, "s01", "s02", "s03"]


# ======================= wiring =======================


def _course(tmp_path: Path) -> Path:
    import yaml

    from course_assistant.ingest import CorpusStore
    from tests.answer.test_ask import CONFIG

    cfg = tmp_path / "course" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(yaml.safe_dump(CONFIG.model_dump(mode="json")), encoding="utf-8")
    CorpusStore(cfg.parent / "corpus_store", CONFIG.cohort).save(CORPUS)
    return cfg


def test_wiring_builds_the_chat_from_named_settings_only(tmp_path):
    from course_assistant.app import wiring
    from tests.demo.test_demo import CODE, HASH

    env = {"CA_CONFIG": str(_course(tmp_path)), "CA_APP_DIR": str(tmp_path / "app"),
           "DEMO_PASSKEY_HASH": HASH}  # fmt: skip
    chat = wiring.from_env(env)
    st = ChatState.new()
    assert chat.locked_reason(st) == NEEDED
    chat.unlock(st, CODE)
    assert chat.session_options(st) == session_labels(CORPUS)
    assert chat.demo.owner_responder is None  # no OPENAI_API_KEY: only own keys answer
    rsp: Any = chat.demo.responder_for_key(OWN_KEY)  # built offline; nothing is called
    assert rsp.keywords["client"].api_key == OWN_KEY
    assert OWN_KEY not in repr(chat) and OWN_KEY not in repr(chat.demo)
    closed = wiring.from_env({**env, "DEMO_PASSKEY_HASH": ""})
    assert "closed" in (closed.locked_reason(ChatState.new()) or "")
    with pytest.raises(wiring.WiringError, match="CA_CONFIG"):
        wiring.from_env({})
    with pytest.raises(wiring.WiringError):
        wiring.from_env({"CA_CONFIG": str(tmp_path / "missing.yaml")})
    secrets_only = wiring.from_env({k: v for k, v in env.items() if k != "DEMO_PASSKEY_HASH"},
                                   secrets_store={"DEMO_PASSKEY_HASH": HASH})  # fmt: skip
    secrets_only.unlock(ChatState.new(), CODE)  # the host secret store is read too


def test_controller_prefers_an_installed_factory(tmp_path):
    from course_assistant.app import wiring

    rig = ChatRig(tmp_path)
    wiring.set_factory(lambda: rig.chat)
    try:
        assert wiring.controller() is rig.chat
    finally:
        wiring.set_factory(None)
    assert wiring.controller(lambda: rig.chat) is rig.chat
