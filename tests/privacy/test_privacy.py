"""B2.2: notice, consent, history and delete (US9, NFR2, DPDP)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from course_assistant import schemas as S
from course_assistant.llm.trace import JsonlSink
from course_assistant.privacy import (
    NOTICE,
    NOTICE_VERSION,
    PrivacyError,
    allowed,
    delete,
    give,
    history,
    needs_notice,
    retry_trace_deletion,
    withdraw,
)
from course_assistant.store import RetentionPolicy, Store

T0 = dt.datetime(2026, 11, 20, 9, 0, tzinfo=dt.UTC)
A, B = "0123456789abcdef", "fedcba9876543210"
SECRET = "my father is in hospital, can I get an extension"


def at(minutes: int) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


def conv(cid: str, pid: str, minute: int = 0) -> S.Conversation:
    return S.Conversation(conversation_id=cid, learner_pid=pid, session_scope="s02",
                          started_at=at(minute))  # fmt: skip


def question(qid: str, cid: str, pid: str, turn: int, minute: int) -> S.Question:
    return S.Question(question_id=qid, conversation_id=cid, turn_index=turn, learner_pid=pid,
                      session_scope="s02", text=f"{SECRET} {qid}",
                      asked_at=at(minute))  # fmt: skip


def answer(qid: str, minute: int) -> S.Answer:
    return S.Answer(question_id=qid, status="handed_over", text="Passed to the team.",
                    citations=(), model_snapshot="gpt-6.1-sol", rules_hash="abcdef123456",
                    corpus_version="v1", top_search_score=0.1, cost_usd=0.01, latency_ms=900,
                    created_at=at(minute))  # fmt: skip


def esc(qid: str, minute: int) -> S.Escalation:
    draft = S.Draft(text="TEAM-ONLY DRAFT")
    return S.Escalation(question_id=qid, reason="personal", status="open", draft=draft,
                        created_at=at(minute))  # fmt: skip


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "app.sqlite")
    yield s
    s.close()


def ids(pid: str) -> tuple[str, str]:
    n = "a" if pid == A else "b"
    return n, f"c-{n * 12}"


def fill(store: Store) -> None:
    """Two learners, two conversations each, two turns per conversation (stored out of order)."""
    for pid in (A, B):
        n, _ = ids(pid)
        for c in (1, 2):
            cid = f"c-{n * 11}{c}"
            store.put_conversation(conv(cid, pid, minute=10 * c))
            for turn in (2, 1):
                qid = f"q-{n * 10}{c}{turn}"
                store.put_question(question(qid, cid, pid, turn, minute=10 * c + turn))
                store.put_answer(answer(qid, minute=10 * c + turn))
                store.put_escalation(esc(qid, minute=10 * c + turn))


def trace_lines(pid: str, n: str) -> list[dict]:
    lines = []
    for c in (1, 2):
        cid = f"c-{n * 11}{c}"
        for turn in (1, 2):
            qid = f"q-{n * 10}{c}{turn}"
            lines.append({"learner_pid": pid, "conversation_id": cid, "question_id": qid,
                          "step": "answer", "tokens_in": 5})  # fmt: skip
    return lines


def traced(tmp_path) -> JsonlSink:
    sink = JsonlSink(tmp_path / "trace.jsonl")
    for pid, n in ((A, "a"), (B, "b")):
        for rec in trace_lines(pid, n):
            sink.emit(rec)
    return sink


def read(sink: JsonlSink) -> list[dict]:
    return [json.loads(x) for x in sink.path.read_text(encoding="utf-8").splitlines() if x]


# ---------- PRV-01 ----------


@pytest.mark.privacy
def test_notice_until_consent_for_the_current_version(store):
    assert needs_notice(A, store)
    give(A, ("answer_questions",), store, at=T0)
    assert not needs_notice(A, store)
    assert needs_notice(B, store)


@pytest.mark.privacy
def test_a_new_notice_version_asks_again(store):
    give(A, ("answer_questions",), store, at=T0, version="notice-0")
    assert needs_notice(A, store)
    assert not needs_notice(A, store, version="notice-0")
    give(A, ("answer_questions",), store, at=at(1))
    assert not needs_notice(A, store)


@pytest.mark.privacy
def test_notice_states_what_why_how_long_where_and_how_to_delete():
    text = NOTICE.lower()
    assert NOTICE_VERSION in NOTICE
    for phrase in (
        "what we store", "why", "how long", "30 days", "1 year", "openai", "store=false",
        "abuse monitoring", "delete", "withdraw", "one conversation", "7 days",
        "answer_questions", "improve_course", "never your name or email",
    ):  # fmt: skip
        assert phrase in text, phrase


@pytest.mark.privacy
def test_needs_notice_fails_closed_on_a_broken_store(tmp_path):
    s = Store(tmp_path / "app.sqlite")
    give(A, ("answer_questions",), s, at=T0)
    s.close()
    assert needs_notice(A, s)  # closed connection: show the notice, never skip it


def test_needs_notice_refuses_a_pid_that_is_not_a_pseudonym(store):
    with pytest.raises(PrivacyError) as err:
        needs_notice("someone@example.com", store)
    assert "example" not in str(err.value)


# ---------- PRV-02 ----------


@pytest.mark.privacy
def test_consent_is_per_purpose(store):
    give(A, ("answer_questions",), store, at=T0)
    assert allowed(A, "answer_questions", store)
    assert not allowed(A, "improve_course", store)
    assert not allowed(B, "answer_questions", store)


@pytest.mark.privacy
def test_withdrawal_stops_processing_at_once_and_is_separate_from_deletion(store):
    fill(store)
    give(A, ("answer_questions", "improve_course"), store, at=T0)
    withdraw(A, store, at=at(5))
    assert not allowed(A, "answer_questions", store)
    assert not allowed(A, "improve_course", store)
    assert needs_notice(A, store)
    assert store.questions_for(A)  # history is still there: deletion is its own action
    kept = store.consents_for(A)
    assert [c.withdrawn_at for c in kept] == [at(5)]  # the proof of consent is kept


@pytest.mark.privacy
def test_a_later_narrower_consent_replaces_the_earlier_one(store):
    give(A, ("answer_questions", "improve_course"), store, at=T0)
    give(A, ("answer_questions",), store, at=at(1))
    assert allowed(A, "answer_questions", store)
    assert not allowed(A, "improve_course", store)


@pytest.mark.privacy
def test_consent_for_an_old_notice_version_does_not_allow_processing(store):
    give(A, ("answer_questions",), store, at=T0, version="notice-0")
    assert not allowed(A, "answer_questions", store)


@pytest.mark.privacy
def test_withdrawal_at_the_same_instant_still_closes_the_consent(store):
    give(A, ("answer_questions",), store, at=T0)
    withdraw(A, store, at=T0)
    assert not allowed(A, "answer_questions", store)
    give(A, ("answer_questions",), store, at=at(1))
    withdraw(A, store, at=T0)  # a clock behind the stored consent still withdraws it
    assert not allowed(A, "answer_questions", store)


@pytest.mark.privacy
def test_give_and_withdraw_are_audited_without_text(store):
    give(A, ("answer_questions",), store, at=T0)
    withdraw(A, store, at=at(1))
    withdraw(B, store, at=at(2))  # a withdrawal with nothing to withdraw is still recorded
    events = [(e.actor, e.action, e.target) for e in store.audit_events()]
    assert events == [(A, "give_consent", A), (A, "withdraw_consent", A),
                      (B, "withdraw_consent", B)]  # fmt: skip


@pytest.mark.privacy
def test_allowed_fails_closed(tmp_path, store):
    give(A, ("answer_questions",), store, at=T0)
    assert not allowed(A, "something_else", store)  # type: ignore[arg-type]
    assert not allowed("not-a-pid", "answer_questions", store)
    s = Store(tmp_path / "other.sqlite")
    give(A, ("answer_questions",), s, at=T0)
    s.close()
    assert not allowed(A, "answer_questions", s)


def test_give_refuses_bad_purposes_without_echoing_them(store):
    for purposes in ((), ("answer_questions", "answer_questions"), (SECRET,)):
        with pytest.raises(PrivacyError) as err:
            give(A, purposes, store, at=T0)  # type: ignore[arg-type]
        assert "father" not in str(err.value)
        assert err.value.__cause__ is None and err.value.__context__ is None
    assert store.consents_for(A) == [] and store.audit_events() == []


def test_consent_and_its_audit_are_one_transaction(store, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(store, "_audit", broken)
    with pytest.raises(PrivacyError) as err:
        give(A, ("answer_questions",), store, at=T0)
    assert "father" not in str(err.value) and err.value.__context__ is None
    assert store.consents_for(A) == []


# ---------- PRV-03 ----------


@pytest.mark.privacy
def test_history_is_only_the_learners_own_in_order(store):
    fill(store)
    store.reply("q-aaaaaaaaaa12", "Speak to the programme office.", "instructor-1", at=at(30))
    h = history(A, store)
    assert [c.conversation.conversation_id for c in h] == ["c-aaaaaaaaaaa1", "c-aaaaaaaaaaa2"]
    turns = [[t.question.question_id for t in c.turns] for c in h]
    assert turns == [["q-aaaaaaaaaa11", "q-aaaaaaaaaa12"], ["q-aaaaaaaaaa21", "q-aaaaaaaaaa22"]]
    first = h[0].turns
    assert first[0].answer is not None and first[0].answer.question_id == "q-aaaaaaaaaa11"
    assert first[0].reply is None and first[0].handover == "open"
    assert first[1].reply is not None and first[1].reply.text == "Speak to the programme office."
    assert first[1].handover == "replied"
    dumped = json.dumps([c.model_dump(mode="json") for c in h])
    assert B not in dumped and "q-bbbb" not in dumped


@pytest.mark.privacy
def test_history_never_shows_the_team_only_draft(store):
    fill(store)
    dumped = json.dumps([c.model_dump(mode="json") for c in history(A, store)])
    assert "TEAM-ONLY DRAFT" not in dumped


def test_history_of_a_new_learner_is_empty(store):
    fill(store)
    assert history("00000000000000ff", store) == ()


def test_history_errors_carry_no_text(store, monkeypatch):
    fill(store)

    def broken(*_a, **_k):
        raise ValueError(SECRET)

    monkeypatch.setattr(store, "questions_for", broken)
    with pytest.raises(PrivacyError) as err:
        history(A, store)
    assert "father" not in str(err.value) and err.value.__context__ is None


# ---------- PRV-04 ----------


@pytest.mark.privacy
def test_delete_removes_app_rows_and_trace_lines_and_records_completion(store, tmp_path):
    fill(store)
    sink = traced(tmp_path)
    req = delete(A, store=store, traces=[sink], at=at(40))
    assert req.complete and req.traces_deleted_at == at(40)
    assert store.conversations_for(A) == [] and store.questions_for(A) == []
    assert store.get_answer("q-aaaaaaaaaa11") is None
    assert store.get_escalation("q-aaaaaaaaaa11") is None
    left = read(sink)
    assert left == trace_lines(B, "b")  # B untouched, every line of A gone
    assert history(B, store)[0].turns[0].question.learner_pid == B
    assert [d.complete for d in store.deletions()] == [True]


@pytest.mark.privacy
def test_delete_one_conversation_only(store, tmp_path):
    fill(store)
    sink = traced(tmp_path)
    req = delete(A, "c-aaaaaaaaaaa1", store=store, traces=[sink], at=at(40))
    assert req.complete and req.conversation_id == "c-aaaaaaaaaaa1"
    assert [c.conversation_id for c in store.conversations_for(A)] == ["c-aaaaaaaaaaa2"]
    left = read(sink)
    assert {r.get("conversation_id") for r in left if r.get("learner_pid") == A} == {
        "c-aaaaaaaaaaa2"
    }
    assert [r for r in left if r.get("learner_pid") == B] == trace_lines(B, "b")


@pytest.mark.privacy
def test_trace_lines_never_hold_the_deleted_text(store, tmp_path):
    fill(store)
    sink = traced(tmp_path)
    delete(A, store=store, traces=[sink], at=at(40))
    raw = sink.path.read_text(encoding="utf-8")
    assert A not in raw and "q-aaaa" not in raw and "c-aaaa" not in raw and raw.strip()


class NoDelete:
    """A sink that cannot delete (e.g. a hosted tracer before its delete API is wired)."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def emit(self, record: dict) -> None:
        self.records.append(record)


class Broken(JsonlSink):
    def delete_pid(self, *_a, **_k) -> int:
        raise OSError(SECRET)


@pytest.mark.privacy
def test_a_sink_that_cannot_delete_leaves_the_request_open_and_purge_flags_it(store, tmp_path):
    fill(store)
    req = delete(A, store=store, traces=[traced(tmp_path), NoDelete()], at=at(40))
    assert store.conversations_for(A) == []  # app rows go at once regardless
    assert req.app_deleted_at == at(40) and req.traces_deleted_at is None
    policy = RetentionPolicy(pilot_end=dt.date(2027, 3, 31))
    assert store.purge(at(40) + dt.timedelta(days=6), policy).overdue_deletions == 0
    assert store.purge(at(40) + dt.timedelta(days=7, minutes=1), policy).overdue_deletions == 1


@pytest.mark.privacy
def test_a_failing_trace_delete_is_retried_later_and_carries_no_text(store, tmp_path):
    fill(store)
    good = traced(tmp_path)
    req = delete(A, store=store, traces=[Broken(tmp_path / "x.jsonl")], at=at(40))
    assert not req.complete
    assert retry_trace_deletion(store, [good], at=at(60)) == 1
    assert [d.traces_deleted_at for d in store.deletions()] == [at(60)]
    assert read(good) == trace_lines(B, "b")
    policy = RetentionPolicy(pilot_end=dt.date(2027, 3, 31))
    assert store.purge(at(40) + dt.timedelta(days=8), policy).overdue_deletions == 0


@pytest.mark.privacy
def test_a_later_conversation_delete_never_closes_an_earlier_open_whole_delete(store, tmp_path):
    fill(store)
    sink = traced(tmp_path)
    delete(A, "c-aaaaaaaaaaa1", store=store, traces=[NoDelete()], at=at(40))
    delete(A, "c-aaaaaaaaaaa2", store=store, traces=[sink], at=at(41))
    # the second delete also cleared the first conversation's traces, so both are complete
    assert all(d.complete for d in store.deletions())
    assert not [r for r in read(sink) if r.get("learner_pid") == A]


@pytest.mark.privacy
def test_deleting_one_learner_leaves_the_other_untouched(store, tmp_path):
    fill(store)
    sink = traced(tmp_path)
    before = history(B, store)
    give(B, ("answer_questions",), store, at=T0)
    delete(A, store=store, traces=[sink], at=at(40))
    assert history(B, store) == before
    assert allowed(B, "answer_questions", store)


def test_delete_requires_an_explicit_list_of_trace_sinks(store):
    with pytest.raises(TypeError):
        delete(A, store=store)  # type: ignore[call-arg]


def test_delete_refuses_bad_ids_without_echoing_them(store, tmp_path):
    fill(store)
    for pid, cid in (("someone@example.com", None), (A, "c-someone@example.com")):
        with pytest.raises(PrivacyError) as err:
            delete(pid, cid, store=store, traces=[], at=at(40))
        assert "example" not in str(err.value) and err.value.__context__ is None
    assert store.deletions() == []


def test_a_store_failure_in_delete_is_one_error_without_text(store, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(store, "delete_history", broken)
    with pytest.raises(PrivacyError) as err:
        delete(A, store=store, traces=[], at=at(40))
    assert "father" not in str(err.value) and err.value.__context__ is None


def test_jsonl_delete_keeps_unrelated_and_drops_unreadable_lines_naming_the_learner(tmp_path):
    sink = JsonlSink(tmp_path / "t.jsonl")
    sink.emit({"learner_pid": B, "tokens_in": 1})
    with sink.path.open("a", encoding="utf-8") as fh:
        fh.write(f"garbled {A} line\n")
        fh.write("garbled other line\n")
    assert sink.delete_pid(A) == 1
    lines = sink.path.read_text(encoding="utf-8").splitlines()
    assert lines == [json.dumps({"learner_pid": B, "tokens_in": 1}, sort_keys=True),
                     "garbled other line"]  # fmt: skip


def test_jsonl_delete_on_a_missing_file_deletes_nothing(tmp_path):
    assert JsonlSink(tmp_path / "none.jsonl").delete_pid(A) == 0
    assert not (tmp_path / "none.jsonl").exists()
