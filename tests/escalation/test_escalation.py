"""B2.1 escalation queue: ESC-01 to ESC-04."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from course_assistant.answer import golden
from course_assistant.answer.answer import AnswerStep
from course_assistant.answer.ask import Assistant, Turn
from course_assistant.answer.replay import Replay
from course_assistant.escalation import (
    MAX_REPLY_CHARS,
    TEAM_LABEL,
    EscalationError,
    close,
    escalate,
    escalate_turn,
    flag_gap,
    learner_replies,
    queue_handover,
    reply,
    reply_times,
    reply_waiting,
    team_queue,
)
from course_assistant.llm import JsonlSink
from course_assistant.privacy import delete, history
from course_assistant.store.db import Store
from tests.answer import scripted as s
from tests.answer.test_ask import NOW, OTHER, PID, QUESTION, S01, Clock, Sink, happy, make

SECRET = "my father is in hospital, can I get an extension"
REPLY = "Yes: email the programme office and we will move your deadline by a week."
TEAM = "instructor-1"
LATER = NOW + dt.timedelta(hours=3)
ESC_STATUSES = ("open", "replied", "material_gap", "closed")

# scripted outputs for each kind of turn (A2.5's fakes through the real `respond`)
KINDS: dict[str, dict[str, list[Any]]] = {
    "answered": happy(),
    "not_covered": {"route": [s.route("course")],
                    "answer": [s.cited("The draft names the user first.", S01, score=0.2)]},
    "graded_work": {"route": [s.route("graded_work", "x", "s01-a1")],
                    "answer": [s.cited("d", S01)],
                    "hint": [s.cited("Revisit Session 1 on naming the problem.", S01)],
                    "integrity": [s.integrity("clear")]},
    "admin": {"route": [s.route("admin")], "answer": [s.cited("d", S01)]},
    "personal": {"route": [s.route("personal")], "answer": [s.cited("d", S01)]},
    "off_topic": {"route": [s.route("off_topic")], "answer": [s.cited("d", S01)]},
    "injection": {"route": [s.route("injection")], "answer": [s.cited("d", S01)]},
    "error": {"route": s.down(), "answer": [s.cited("d", S01)]},
}  # fmt: skip


def turn_of(kind: str, store: Store, n: int = 1, pid: str = PID, question: str = QUESTION,
            clock: Clock | None = None, sink: Sink | None = None) -> Turn:  # fmt: skip
    a, _, _, _ = make(store, clock or Clock(), **KINDS[kind])
    counter = iter(range(1, 1_000))
    a.new_id = lambda: f"{n:08x}{next(counter):08x}"  # conversation ids unique per turn
    if sink is not None:
        a.sink = sink
    return a.turn(question, pid, request_id=f"r-{n:016x}")


def escalated(kind: str, store: Store, n: int = 1, **kw: Any) -> tuple[Turn, Any]:
    t = turn_of(kind, store, n, **kw)
    return t, escalate_turn(t, store=store, at=NOW + dt.timedelta(minutes=1))


def code(fn: Any, *args: Any, **kw: Any) -> str:
    with pytest.raises(EscalationError) as err:
        fn(*args, **kw)
    return err.value.code


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


# ---------- ESC-01: escalate ----------

# the spec table: which reasons each stored answer accepts by question id (`escalate`)
FITS = {
    "answered": {"learner_request"},
    "not_covered": {"not_covered", "learner_request"},
    "graded_work": {"graded_work", "learner_request"},
    "admin": {"learner_request"},  # admin and personal need the turn's route
    "personal": {"learner_request"},
    "off_topic": set(),
    "injection": set(),
    "error": {"learner_request"},
}
REASONS = ("not_covered", "graded_work", "admin", "personal", "learner_request")


@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("reason", REASONS)
def test_each_reason_is_accepted_only_for_the_answer_it_fits(kind, reason, store):
    t = turn_of(kind, store)
    qid = t.answer.question_id
    if reason in FITS[kind]:
        e = escalate(qid, reason, learner_pid=PID, store=store, at=NOW)
        assert e.reason == reason and e.status == "open" and e.created_at == NOW
        assert store.get_escalation(qid) == e
    else:
        assert code(escalate, qid, reason, learner_pid=PID, store=store, at=NOW) == "refused"
        assert store.get_escalation(qid) is None


@pytest.mark.parametrize(("kind", "reason"), [("answered", "learner_request"),
                                              ("not_covered", "not_covered"),
                                              ("graded_work", "graded_work"),
                                              ("admin", "admin"), ("personal", "personal"),
                                              ("error", "learner_request")])  # fmt: skip
def test_a_turn_escalates_with_its_default_reason(kind, reason, store):
    t, e = escalated(kind, store)
    assert t.route == ("course" if kind in ("answered", "not_covered") else
                       None if kind == "error" else kind)  # fmt: skip
    assert e.reason == reason and e.question_id == t.answer.question_id


@pytest.mark.parametrize("kind", ["off_topic", "injection"])
def test_off_topic_and_injection_turns_are_never_escalated(kind, store):
    t = turn_of(kind, store)
    assert code(escalate_turn, t, store=store) == "refused"
    for reason in REASONS:
        assert code(escalate_turn, t, store=store, reason=reason) == "refused"
    assert store.escalations() == []


def test_a_handed_over_turn_is_queued_at_once_with_its_route_as_the_reason(store):
    admin = turn_of("admin", store, 1)
    personal = turn_of("personal", store, 2)
    answered = turn_of("answered", store, 3)
    a = queue_handover(admin, store=store, at=NOW)
    p = queue_handover(personal, store=store, at=NOW)
    assert a is not None and a.reason == "admin" and a.draft is None and a.status == "open"
    assert p is not None and p.reason == "personal" and p.draft is None
    assert queue_handover(answered, store=store, at=NOW) is None
    assert {e.question_id for e in store.escalations()} == {admin.answer.question_id,
                                                           personal.answer.question_id}  # fmt: skip
    # the reason must match the route: an admin handover is not "personal"
    assert code(escalate_turn, admin, store=store, reason="personal") == "refused"


def test_a_not_covered_turn_carries_its_kept_draft_with_citations_and_hits(store):
    t, e = escalated("not_covered", store)
    assert t.answer.text == Assistant.NOT_COVERED and t.draft is not None
    assert e.reason == "not_covered" and e.draft is not None
    assert e.draft.text == "The draft names the user first."
    assert e.draft.citations == t.draft.citations and len(e.draft.citations) == 1
    assert e.draft.hits == t.draft.hits and e.draft.hits[0].score == pytest.approx(0.2)
    assert e.draft.citations[0].file_id == S01


def test_a_shown_answer_or_hint_carries_its_own_text_and_sources(store):
    _, ans = escalated("answered", store, 1)
    t, hint = escalated("graded_work", store, 2)
    assert ans.draft is not None and ans.draft.text == happy()["answer"][0].output_text
    assert ans.draft.citations and ans.draft.hits
    assert hint.draft is not None and hint.draft.text == t.answer.text
    assert hint.draft.citations == t.answer.citations


def test_a_later_tap_on_a_stored_not_covered_turn_has_no_draft(store):
    t = turn_of("not_covered", store)
    e = escalate(t.answer.question_id, "not_covered", learner_pid=PID, store=store, at=NOW)
    assert e.draft is None  # the kept draft lives only in the Turn


def test_a_second_escalation_returns_the_first_unchanged(store):
    t, first = escalated("not_covered", store)
    reply(t.answer.question_id, REPLY, instructor=TEAM, store=store, at=LATER)
    again = escalate_turn(t, store=store, at=LATER + dt.timedelta(hours=1))
    same = escalate(t.answer.question_id, "learner_request", learner_pid=PID, store=store)
    assert again == same == store.get_escalation(t.answer.question_id)
    assert again.status == "replied" and again.reply_text == REPLY
    assert again.created_at == first.created_at and len(store.escalations()) == 1


def test_an_unanswered_unknown_or_paused_question_is_refused(store):
    from course_assistant.schemas import Conversation, Question

    store.put_conversation(Conversation(conversation_id="c-00000000000000aa", learner_pid=PID,
                                        session_scope=None, started_at=NOW))  # fmt: skip
    store.put_question(Question(question_id="q-00000000000000aa",
                                conversation_id="c-00000000000000aa", turn_index=1,
                                learner_pid=PID, session_scope=None, text=QUESTION,
                                asked_at=NOW))  # fmt: skip
    assert code(escalate, "q-00000000000000aa", "learner_request", learner_pid=PID,
                store=store) == "refused"  # fmt: skip
    assert code(escalate, "q-00000000000000bb", "learner_request", learner_pid=PID,
                store=store) == "not_found"  # fmt: skip
    assert code(escalate, "not an id", "learner_request", learner_pid=PID, store=store) == "invalid"
    assert code(escalate, "q-00000000000000aa", "urgent", learner_pid=PID, store=store) == "invalid"
    store.set_pause("all", True, TEAM, at=NOW)
    paused = turn_of("answered", store)
    assert paused.paused and code(escalate_turn, paused, store=store) == "invalid"
    assert queue_handover(paused, store=store) is None


def test_escalation_is_never_before_the_question(store):
    t = turn_of("admin", store)
    e = escalate_turn(t, store=store, at=NOW - dt.timedelta(hours=1))
    assert e.created_at == NOW
    with pytest.raises(EscalationError):
        escalate_turn(t, store=store, at=dt.datetime(2026, 11, 20, 10, 0))  # naive


@pytest.mark.privacy
def test_another_learners_question_and_leaked_drafts_are_refused(store):
    t = turn_of("not_covered", store, 1)
    # another learner cannot escalate (or learn of) this learner's question
    assert code(escalate, t.answer.question_id, "not_covered", learner_pid=OTHER,
                store=store) == "not_found"  # fmt: skip
    # a turn that does not match the stored one cannot attach a draft to it
    forged = t.model_copy(update={"answer": t.answer.model_copy(update={"text": "x"})})
    assert code(escalate_turn, forged, store=store) == "refused"
    # a leaked course draft is never attached, though the question still reaches the team
    a, _, _, _ = make(store, Clock(), **happy(f"Here it is: {AnswerStep.CANARY}."))
    lt = a.turn(QUESTION, PID, request_id=f"r-{2:016x}")
    assert lt.leak and lt.draft is not None
    e = escalate_turn(lt, store=store, at=NOW)
    assert e.reason == "not_covered" and e.draft is None
    assert AnswerStep.CANARY not in json.dumps(e.model_dump(mode="json"))


@pytest.mark.golden
def test_golden_replay_turns_queue_by_the_spec_table():
    fx = golden.load()
    seen: dict[str, int] = {}
    for case in fx.cases:
        store = Store(":memory:")
        cid = golden.seed(store, fx, case)
        if case.paused:
            store.set_pause("all", True, TEAM, at=fx.now - dt.timedelta(minutes=1))
        a = Assistant(fx.config, fx.corpus, fx.assessments, store, Replay(case.recordings),
                      now=lambda: fx.now, timer=lambda: 0.0,
                      new_id=golden.ids_for(case.request_id))  # fmt: skip
        t = a.turn(case.question, case.learner_pid, case.session, cid,
                   request_id=case.request_id)  # fmt: skip
        assert t.answer == case.expected
        st = t.answer.status
        if t.paused:
            assert code(escalate_turn, t, store=store) == "invalid"
            outcome = "paused"
        elif st == "not_covered" and t.answer.text != Assistant.NOT_COVERED:
            assert code(escalate_turn, t, store=store) == "refused"  # off-topic, injection
            outcome = "refused"
        else:
            e = escalate_turn(t, store=store, at=fx.now)
            want = {"handed_over": t.route, "not_covered": "not_covered",
                    "hint_only": "graded_work" if t.route == "graded_work" else
                    "learner_request"}.get(st, "learner_request")  # fmt: skip
            assert e.reason == want and e.status == "open"
            if st == "handed_over":
                assert queue_handover(t, store=store) == e and e.draft is None
            if st == "not_covered" and t.draft is not None and not t.leak:
                assert e.draft is not None and e.draft.text == t.draft.text
            if t.leak:
                assert e.draft is None
            outcome = e.reason
        seen[outcome] = seen.get(outcome, 0) + 1
        store.close()
    assert set(seen) >= {"admin", "personal", "not_covered", "learner_request", "refused",
                         "paused", "graded_work"}  # fmt: skip


# ---------- ESC-02: reply ----------


def test_reply_sets_text_role_time_and_status(store):
    t, e = escalated("admin", store)
    r = reply(t.answer.question_id, REPLY, instructor=TEAM, store=store, at=LATER)
    assert r.status == "replied" and r.reply_text == REPLY and r.replied_by == TEAM
    assert r.replied_at == LATER and r.created_at == e.created_at
    assert store.get_escalation(t.answer.question_id) == r
    (ev,) = [x for x in store.audit_events() if x.action == "reply"]
    assert ev.actor == TEAM and ev.target == t.answer.question_id and ev.at == LATER


def test_the_learner_sees_the_reply_labelled_from_the_teaching_team(store):
    t, _ = escalated("personal", store)
    reply(t.answer.question_id, REPLY, instructor="ta-2", store=store, at=LATER)
    (r,) = learner_replies(PID, store)
    assert r.label == TEAM_LABEL == "From the teaching team"
    assert r.text == REPLY and r.replied_by == "ta-2" and r.replied_at == LATER
    assert r.question_id == t.answer.question_id and r.conversation_id == t.conversation_id


def test_a_second_reply_or_a_reply_to_a_closed_escalation_is_refused(store):
    t, _ = escalated("admin", store, 1)
    qid = t.answer.question_id
    reply(qid, REPLY, instructor=TEAM, store=store, at=LATER)
    assert code(reply, qid, "Changed my mind.", instructor="ta-1", store=store) == "refused"
    assert store.get_escalation(qid).reply_text == REPLY  # type: ignore[union-attr]
    t2, _ = escalated("personal", store, 2)
    close(t2.answer.question_id, instructor=TEAM, store=store, at=LATER)
    assert code(reply, t2.answer.question_id, REPLY, instructor=TEAM, store=store) == "refused"
    assert [x.action for x in store.audit_events()] == ["reply", "close"]  # refusals not audited


@pytest.mark.parametrize(
    ("text", "who"),
    [
        ("", TEAM),
        ("   \n", TEAM),
        ("x" * (MAX_REPLY_CHARS + 1), TEAM),
        (REPLY, "Dr Smith"),
        (REPLY, PID),
        (REPLY, None),
    ],
)
def test_a_reply_needs_text_within_the_limit_and_a_role_id(text, who, store):
    t, _ = escalated("admin", store)
    assert code(reply, t.answer.question_id, text, instructor=who, store=store) == "invalid"
    assert store.get_escalation(t.answer.question_id).status == "open"  # type: ignore[union-attr]
    ok = reply(t.answer.question_id, "y" * MAX_REPLY_CHARS, instructor=TEAM, store=store)
    assert ok.status == "replied"


def test_a_reply_to_a_missing_escalation_is_not_found(store):
    t = turn_of("answered", store)
    assert code(reply, t.answer.question_id, REPLY, instructor=TEAM, store=store) == "not_found"
    assert store.audit_events() == []


@pytest.mark.privacy
def test_reply_audit_holds_no_text_and_errors_carry_no_text(store, monkeypatch):
    t = turn_of("personal", store, question=SECRET)
    escalate_turn(t, store=store, at=NOW)
    reply(t.answer.question_id, REPLY, instructor=TEAM, store=store, at=LATER)
    dumped = json.dumps([e.model_dump(mode="json") for e in store.audit_events()])
    assert "father" not in dumped and "programme office" not in dumped
    for ev in store.audit_events():
        assert set(ev.model_dump()) == {"at", "actor", "action", "target"}
    # a reply refused by validation never echoes its text
    with pytest.raises(EscalationError) as caught:
        reply(t.answer.question_id, SECRET + "x" * 4_000, instructor=TEAM, store=store)
    err = caught.value
    assert "father" not in str(err) and err.__context__ is None and err.__cause__ is None

    def broken(*_a: Any, **_k: Any) -> Any:
        raise ValueError(SECRET)

    monkeypatch.setattr(store, "update_escalation", broken)
    with pytest.raises(EscalationError) as caught:
        flag_gap(t.answer.question_id, instructor=TEAM, store=store)
    err = caught.value
    assert err.code == "failed" and "father" not in str(err) and err.__context__ is None


def test_concurrent_replies_never_overwrite_each_other(tmp_path: Path):
    import threading

    path = tmp_path / "app.db"
    store = Store(path)
    t, _ = escalated("admin", store)
    qid = t.answer.question_id
    results: list[str] = []

    def send(i: int) -> None:
        own = Store(path)
        try:
            reply(qid, f"Reply number {i}.", instructor=f"ta-{i}", store=own, at=LATER)
            results.append("ok")
        except EscalationError as exc:
            results.append(exc.code)
        finally:
            own.close()

    threads = [threading.Thread(target=send, args=(i,)) for i in range(1, 7)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == ["ok"] + ["refused"] * 5
    final = store.get_escalation(qid)
    assert final is not None and final.reply_text is not None and final.replied_by is not None
    assert final.reply_text == f"Reply number {final.replied_by.removeprefix('ta-')}."
    assert [x.action for x in store.audit_events()] == ["reply"]


# ---------- ESC-03: reply waiting in history ----------


def test_history_shows_the_handover_status_and_reply_for_that_turn(store):
    t1, _ = escalated("admin", store, 1)
    t2 = turn_of("answered", store, 2)
    reply(t1.answer.question_id, REPLY, instructor=TEAM, store=store, at=LATER)
    turns = {tt.question.question_id: tt for c in history(PID, store) for tt in c.turns}
    shown = turns[t1.answer.question_id]
    assert shown.handover == "replied" and shown.reply is not None
    assert shown.reply.text == REPLY and shown.reply.replied_by == TEAM
    assert turns[t2.answer.question_id].handover is None
    assert turns[t2.answer.question_id].reply is None


def test_reply_waiting_counts_replied_turns(store):
    t1, _ = escalated("admin", store, 1)
    t2, _ = escalated("personal", store, 2)
    escalated("not_covered", store, 3)
    assert reply_waiting(PID, store) == 0
    reply(t1.answer.question_id, REPLY, instructor=TEAM, store=store, at=LATER)
    assert reply_waiting(PID, store) == 1
    reply(t2.answer.question_id, "Second.", instructor=TEAM, store=store,
          at=LATER + dt.timedelta(minutes=5))  # fmt: skip
    close(t2.answer.question_id, instructor=TEAM, store=store, at=LATER)
    assert reply_waiting(PID, store) == 2  # a closed escalation's reply still shows
    assert [r.text for r in learner_replies(PID, store)] == ["Second.", REPLY]  # newest first
    assert reply_waiting(OTHER, store) == 0
    assert code(reply_waiting, "not a pid", store) == "invalid"


@pytest.mark.privacy
def test_deleting_history_removes_escalations_and_replies(tmp_path: Path):
    store = Store(tmp_path / "app.db")
    clock = Clock()
    t1, _ = escalated("admin", store, 1, clock=clock)
    clock.at = NOW + dt.timedelta(hours=3)  # idle: the next turn starts a new conversation
    t2, _ = escalated("personal", store, 2, clock=clock)
    t3, _ = escalated("admin", store, 3, pid=OTHER)
    assert t1.conversation_id != t2.conversation_id
    for t in (t1, t2, t3):
        reply(t.answer.question_id, REPLY, instructor=TEAM, store=store, at=LATER)
    sink = JsonlSink(tmp_path / "traces.jsonl")
    delete(PID, t1.conversation_id, store=store, traces=[sink], at=LATER)  # one conversation
    assert store.get_escalation(t1.answer.question_id) is None
    assert store.get_escalation(t2.answer.question_id) is not None
    delete(PID, store=store, traces=[sink], at=LATER)  # the whole history
    assert store.get_escalation(t2.answer.question_id) is None
    assert reply_waiting(PID, store) == 0 and learner_replies(PID, store) == ()
    assert [i.question_id for i in team_queue(store, viewer=TEAM)] == [t3.answer.question_id]
    raw = (tmp_path / "app.db").read_bytes()
    assert b"programme office" in raw  # the other learner's reply stays
    store.close()


@pytest.mark.privacy
def test_replies_are_only_shown_to_their_own_learner_and_no_email_is_held(store):
    t, _ = escalated("admin", store, 1)
    escalated("personal", store, 2, pid=OTHER)
    reply(t.answer.question_id, REPLY, instructor=TEAM, store=store, at=LATER)
    assert learner_replies(OTHER, store) == ()
    assert [r.question_id for r in learner_replies(PID, store)] == [t.answer.question_id]
    # nothing in the queue's records can hold an email address or a name
    import course_assistant.escalation.queue as q

    for model in (q.LearnerReply, q.QueueItem, q.ReplyTimes):
        assert not {f for f in model.model_fields if "email" in f or "name" in f}
    source = Path(q.__file__).read_text(encoding="utf-8")
    assert "smtp" not in source.lower() and "send_mail" not in source


# ---------- ESC-04: material gap, statuses, timestamps ----------

# (action, from status) -> resulting status, or None when refused
TRANSITIONS = {
    ("reply", "open"): "replied", ("reply", "replied"): None,
    ("reply", "material_gap"): "material_gap", ("reply", "closed"): None,
    ("flag_gap", "open"): "material_gap", ("flag_gap", "replied"): "material_gap",
    ("flag_gap", "material_gap"): None, ("flag_gap", "closed"): None,
    ("close", "open"): "closed", ("close", "replied"): "closed",
    ("close", "material_gap"): "closed", ("close", "closed"): None,
}  # fmt: skip
SETUP = {"open": [], "replied": ["reply"], "material_gap": ["flag_gap"], "closed": ["close"]}


def act(action: str, qid: str, store: Store, at: dt.datetime = LATER) -> Any:
    if action == "reply":
        return reply(qid, REPLY, instructor=TEAM, store=store, at=at)
    fn = flag_gap if action == "flag_gap" else close
    return fn(qid, instructor=TEAM, store=store, at=at)


@pytest.mark.parametrize(("action", "start"), list(TRANSITIONS))
def test_status_transitions_follow_the_table(action, start, store):
    t, _ = escalated("admin", store)
    qid = t.answer.question_id
    for step in SETUP[start]:
        act(step, qid, store)
    before = store.get_escalation(qid)
    audits = len(store.audit_events())
    want = TRANSITIONS[(action, start)]
    if want is None:
        assert code(act, action, qid, store) == "refused"
        assert store.get_escalation(qid) == before and len(store.audit_events()) == audits
    else:
        got = act(action, qid, store)
        assert got.status == want and store.get_escalation(qid) == got
        ev = store.audit_events()[-1]
        assert (ev.action, ev.actor, ev.target) == (action, TEAM, qid)


def test_flag_gap_keeps_the_reply_and_reply_never_lands_before_creation(store):
    t, e = escalated("admin", store, 1)
    qid = t.answer.question_id
    r = reply(qid, REPLY, instructor=TEAM, store=store, at=e.created_at - dt.timedelta(hours=2))
    assert r.replied_at == e.created_at  # a clock behind the stored time is clamped
    g = flag_gap(qid, instructor=TEAM, store=store, at=LATER)
    assert g.status == "material_gap" and g.reply_text == REPLY and g.replied_at == r.replied_at
    t2, _ = escalated("personal", store, 2)
    flag_gap(t2.answer.question_id, instructor=TEAM, store=store, at=LATER)
    r2 = reply(t2.answer.question_id, REPLY, instructor=TEAM, store=store, at=LATER)
    assert r2.status == "material_gap" and r2.reply_text == REPLY  # the flag is kept
    assert reply_waiting(PID, store) == 2


def test_reply_times_give_count_median_and_p90(store):
    assert reply_times(store).model_dump() == {"replied": 0, "median_seconds": None,
                                               "p90_seconds": None}  # fmt: skip
    minutes = [10, 20, 30, 40, 50, 60, 70, 80, 90, 600]
    for i, m in enumerate(minutes, start=1):
        t, e = escalated("admin", store, i)
        reply(t.answer.question_id, REPLY, instructor=TEAM, store=store,
              at=e.created_at + dt.timedelta(minutes=m))  # fmt: skip
    escalated("personal", store, 99)  # open: not counted
    rt = reply_times(store)
    assert rt.replied == 10 and rt.median_seconds == 55 * 60 and rt.p90_seconds == 90 * 60


def test_the_team_queue_shows_question_reason_draft_and_is_audited(store):
    t1, e1 = escalated("not_covered", store, 1)
    t2, _ = escalated("admin", store, 2)
    items = team_queue(store, viewer="ta-3", at=LATER)
    assert [i.question_id for i in items] == [t1.answer.question_id, t2.answer.question_id]
    assert items[0].question == QUESTION and items[0].reason == "not_covered"
    assert items[0].draft == e1.draft and items[0].status == "open"
    assert items[1].reason == "admin" and items[1].draft is None
    (ev,) = store.audit_events()
    assert (ev.action, ev.actor, ev.target, ev.at) == ("view_queue", "ta-3", None, LATER)
    reply(t2.answer.question_id, REPLY, instructor=TEAM, store=store, at=LATER)
    assert [i.question_id for i in team_queue(store, viewer=TEAM, status="open")] == [
        t1.answer.question_id
    ]
    assert code(team_queue, store, viewer="Dr Smith") == "invalid"
    assert code(team_queue, store, viewer=TEAM, status="urgent") == "invalid"


@pytest.mark.privacy
def test_no_escalation_action_writes_a_trace_and_audits_hold_no_text(store):
    sink = Sink()
    t = turn_of("personal", store, question=SECRET, sink=sink)
    n = len(sink.records)
    assert n and not any("father" in json.dumps(r) for r in sink.records)
    qid = t.answer.question_id
    queue_handover(t, store=store, at=NOW)
    team_queue(store, viewer=TEAM, at=LATER)
    reply(qid, REPLY, instructor=TEAM, store=store, at=LATER)
    flag_gap(qid, instructor=TEAM, store=store, at=LATER)
    close(qid, instructor=TEAM, store=store, at=LATER)
    learner_replies(PID, store)
    reply_times(store)
    assert len(sink.records) == n  # nothing in this node writes a trace (D-14)
    assert [e.action for e in store.audit_events()] == ["view_queue", "reply", "flag_gap",
                                                        "close"]  # fmt: skip
    dumped = json.dumps([e.model_dump(mode="json") for e in store.audit_events()])
    assert "father" not in dumped and "programme office" not in dumped
    assert all(e.actor == TEAM for e in store.audit_events())


# ---------- verifier pass 1 regressions ----------


def test_a_second_reply_after_a_gap_flag_is_refused(store):
    _, e = escalated("not_covered", store)
    qid = e.question_id
    reply(qid, "First reply A", instructor="instructor-1", store=store,
          at=NOW + dt.timedelta(hours=1))  # fmt: skip
    flag_gap(qid, instructor="instructor-1", store=store, at=NOW + dt.timedelta(hours=2))
    assert code(reply, qid, "Second reply B", instructor="instructor-1", store=store,
                at=NOW + dt.timedelta(hours=5)) == "refused"  # fmt: skip
    kept = next(r for r in store.escalations() if r.question_id == qid)
    assert kept.reply_text == "First reply A"


@pytest.mark.privacy
def test_a_not_covered_draft_that_leaks_is_never_attached(store):
    leaky = {
        "route": [s.route("course")],
        "answer": [s.cited("Your id is 3f9a0c1d7e2b4a68, see slide 3.", S01, score=0.2)],
    }
    KINDS["leaky_not_covered"] = leaky
    try:
        _, e = escalated("leaky_not_covered", store)
    finally:
        del KINDS["leaky_not_covered"]
    assert e.draft is None


@pytest.mark.parametrize("text", ["​", " ​⁠ ", "﻿"])
def test_a_reply_of_only_invisible_characters_is_refused(store, text):
    _, e = escalated("not_covered", store)
    assert code(reply, e.question_id, text, instructor="instructor-1", store=store,
                at=NOW + dt.timedelta(hours=1)) == "invalid"  # fmt: skip
