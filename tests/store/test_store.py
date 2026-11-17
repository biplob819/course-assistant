"""N1.5: the app store, audit log, pause switch and retention."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from course_assistant import schemas as S
from course_assistant.store import RetentionPolicy, Store

T0 = dt.datetime(2026, 11, 20, 9, 0, tzinfo=dt.UTC)
A, B = "0123456789abcdef", "fedcba9876543210"


def conv(cid: str, pid: str = A) -> S.Conversation:
    return S.Conversation(conversation_id=cid, learner_pid=pid, session_scope="s02", started_at=T0)


def question(qid: str, cid: str, pid: str = A, turn: int = 1) -> S.Question:
    return S.Question(question_id=qid, conversation_id=cid, turn_index=turn, learner_pid=pid,
                      session_scope="s02", text="Why both evals?", asked_at=T0)  # fmt: skip


def answer(qid: str) -> S.Answer:
    return S.Answer(question_id=qid, status="not_covered", text="Not covered.", citations=(),
                    model_snapshot="gpt-6.1-sol", rules_hash="abcdef123456", corpus_version="v1",
                    top_search_score=0.1, cost_usd=0.01, latency_ms=900, created_at=T0)  # fmt: skip


def esc(qid: str) -> S.Escalation:
    return S.Escalation(question_id=qid, reason="not_covered", status="open", created_at=T0)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "app.sqlite")
    yield s
    s.close()


def fill(store: Store) -> None:
    for pid, n in ((A, "a"), (B, "b")):
        cid = f"c-{n * 12}"
        store.put_conversation(conv(cid, pid))
        for i in (1, 2):
            qid = f"q-{n * 11}{i}"
            store.put_question(question(qid, cid, pid, i))
            store.put_answer(answer(qid))
            store.put_escalation(esc(qid))


# ---------- DB-01 ----------


def test_every_record_round_trips(store):
    fill(store)
    assert store.get_conversation("c-aaaaaaaaaaaa") == conv("c-aaaaaaaaaaaa")
    assert store.get_answer("q-aaaaaaaaaaa1") == answer("q-aaaaaaaaaaa1")
    assert store.get_escalation("q-aaaaaaaaaaa2") == esc("q-aaaaaaaaaaa2")
    c = S.Consent(learner_pid=A, notice_version="n1", purposes=("answer_questions",), given_at=T0)
    store.put_consent(c)
    assert store.consents_for(A) == [c]


def test_history_is_only_the_learners_own(store):
    fill(store)
    qs = store.questions_for(A)
    assert [q.question_id for q in qs] == ["q-aaaaaaaaaaa1", "q-aaaaaaaaaaa2"]
    assert all(q.learner_pid == A for q in qs)
    assert [c.learner_pid for c in store.conversations_for(B)] == [B]


@pytest.mark.contract
def test_migrations_are_versioned_and_idempotent(tmp_path):
    path = tmp_path / "app.sqlite"
    Store(path).close()
    s = Store(path)
    assert s.schema_version() == len(s.MIGRATIONS)
    s.close()
    con = sqlite3.connect(path)
    assert con.execute("select count(*) from schema_migrations").fetchone()[0] == len(
        Store.MIGRATIONS
    )
    con.close()


def test_a_tampered_row_fails_validation_on_read(store):
    fill(store)
    store._con.execute("update records set json = replace(json, 'not_covered', 'answered') "
                       "where kind = 'answer'")  # fmt: skip
    with pytest.raises(ValueError):
        store.get_answer("q-aaaaaaaaaaa1")


# ---------- DB-02 ----------


def test_teaching_team_actions_and_deletions_are_audited(store):
    fill(store)
    store.reply("q-aaaaaaaaaaa1", "See slide 9.", "instructor-1", at=T0)
    store.delete_history(B, at=T0)
    events = store.audit_events()
    assert [(e.action, e.actor) for e in events] == [
        ("reply", "instructor-1"),
        ("delete_history", B),
    ]
    assert events[0].target == "q-aaaaaaaaaaa1"


@pytest.mark.privacy
def test_audit_rows_hold_no_text(store):
    fill(store)
    store.reply("q-aaaaaaaaaaa1", "Your extension is approved because of your illness.",
                "instructor-1", at=T0)  # fmt: skip
    raw = store._con.execute("select json from records where kind = 'audit'").fetchall()
    assert raw and not any("illness" in r[0] for r in raw)


# ---------- DB-03 ----------


def test_pause_per_session_and_for_all(store):
    assert not store.is_paused("s02")
    store.set_pause("s02", True, "instructor-1", at=T0)
    assert store.is_paused("s02") and not store.is_paused("s01")
    store.set_pause("all", True, "instructor-1", at=T0)
    assert store.is_paused("s01")
    store.set_pause("all", False, "instructor-1", at=T0)
    assert not store.is_paused("s01") and store.is_paused("s02")
    assert [e.action for e in store.audit_events()] == ["pause", "pause", "resume"]


@pytest.mark.safety
def test_pause_read_failure_fails_closed(store):
    store._con.execute("drop table records")
    assert store.is_paused("s02") is True


# ---------- DB-04 ----------


@pytest.mark.privacy
def test_delete_history_removes_every_row_for_the_learner(store):
    fill(store)
    req = store.delete_history(A, at=T0)
    assert req.app_deleted_at == T0 and req.traces_deleted_at is None
    assert store.questions_for(A) == [] and store.conversations_for(A) == []
    assert store.get_answer("q-aaaaaaaaaaa1") is None
    assert store.get_escalation("q-aaaaaaaaaaa1") is None
    assert len(store.questions_for(B)) == 2  # another learner untouched


@pytest.mark.privacy
def test_delete_one_conversation_only(store):
    fill(store)
    store.put_conversation(conv("c-aaaaaaaaaaab"))
    store.put_question(question("q-aaaaaaaaaab1", "c-aaaaaaaaaaab"))
    store.delete_history(A, conversation_id="c-aaaaaaaaaaab", at=T0)
    assert [q.conversation_id for q in store.questions_for(A)] == ["c-aaaaaaaaaaaa"] * 2


def test_delete_is_atomic(store, monkeypatch):
    fill(store)

    def boom(*a, **k):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(store, "_audit", boom)
    with pytest.raises(RuntimeError):
        store.delete_history(A, at=T0)
    assert len(store.questions_for(A)) == 2  # rolled back


def test_purge_applies_each_retention_period(store):
    fill(store)
    store.reply("q-aaaaaaaaaaa1", "ok", "instructor-1", at=T0)
    store.put_consent(S.Consent(learner_pid=A, notice_version="n1",
                                purposes=("answer_questions",), given_at=T0))  # fmt: skip
    policy = RetentionPolicy(pilot_end=dt.date(2026, 12, 31))
    report = store.purge(dt.datetime(2027, 1, 29, tzinfo=dt.UTC), policy)  # 29 days after
    assert report.text_rows_deleted == 0 and store.questions_for(A)
    report = store.purge(dt.datetime(2027, 1, 31, tzinfo=dt.UTC), policy)  # 31 days after
    assert report.text_rows_deleted > 0 and store.questions_for(A) == []
    assert store.consents_for(A) and store.audit_events()  # kept for a year
    store.purge(dt.datetime(2028, 1, 2, tzinfo=dt.UTC), policy)
    assert store.consents_for(A) == []
    assert {e.action for e in store.audit_events()} == {"purge"}  # each purge is audited


@pytest.mark.privacy
def test_purge_flags_deletion_requests_pending_over_7_days(store):
    fill(store)
    store.delete_history(A, at=T0)
    policy = RetentionPolicy(pilot_end=dt.date(2026, 12, 31))
    assert store.purge(T0 + dt.timedelta(days=6), policy).overdue_deletions == 0
    assert store.purge(T0 + dt.timedelta(days=8), policy).overdue_deletions == 1
    store.mark_traces_deleted(A, at=T0 + dt.timedelta(days=8))
    assert store.purge(T0 + dt.timedelta(days=9), policy).overdue_deletions == 0


# ---------- verifier pass 1 regressions ----------


@pytest.mark.privacy
def test_answers_and_escalations_need_a_stored_question(store):
    with pytest.raises(KeyError):
        store.put_answer(answer("q-dddddddddd01"))
    with pytest.raises(KeyError):
        store.put_escalation(esc("q-dddddddddd01"))
    fill(store)
    store.delete_history(A, at=T0)
    with pytest.raises(KeyError):
        store.put_answer(answer("q-aaaaaaaaaaa1"))  # its question is gone
    rows = store._con.execute("select count(*) from records where pid is null and kind in "
                              "('answer', 'escalation')").fetchone()[0]  # fmt: skip
    assert rows == 0


def test_concurrent_first_opens_migrate_once(tmp_path):
    import multiprocessing as mp

    path = tmp_path / "race.sqlite"
    with mp.get_context("spawn").Pool(4) as pool:
        versions = pool.map(_open_and_report, [str(path)] * 4)
    assert versions == [len(Store.MIGRATIONS)] * 4


def _open_and_report(path: str) -> int:
    s = Store(path)
    v = s.schema_version()
    s.close()
    return v


@pytest.mark.safety
def test_an_edited_pid_column_is_refused(store):
    fill(store)
    store._con.execute(f"update records set pid = '{B}' where kind = 'question'")
    with pytest.raises(ValueError):
        store.questions_for(B)


def test_audit_keys_are_never_reused_after_purge(store):
    old = T0 - dt.timedelta(days=400)
    store.audit("instructor-1", "view_queue", None, at=old)
    store.audit("instructor-1", "view_queue", None, at=T0)
    store.audit("instructor-1", "view_digest", None, at=T0)
    store.purge(T0, RetentionPolicy(pilot_end=dt.date(2027, 6, 30)))
    store.audit("instructor-1", "flag_gap", None, at=T0)
    actions = [e.action for e in store.audit_events()]
    assert actions == ["view_queue", "view_digest", "purge", "flag_gap"]


@pytest.mark.privacy
def test_two_deletions_at_the_same_moment_are_both_recorded(store):
    store.put_conversation(conv("c-aaaaaaaaaaaa"))
    store.put_conversation(conv("c-bbbbbbbbbbbb"))
    store.delete_history(A, conversation_id="c-aaaaaaaaaaaa", at=T0)
    store.delete_history(A, conversation_id="c-bbbbbbbbbbbb", at=T0)
    assert sorted(d.conversation_id or "" for d in store.deletions()) == [
        "c-aaaaaaaaaaaa", "c-bbbbbbbbbbbb"]  # fmt: skip


def test_a_year_is_a_calendar_year_across_leap_days(store):
    store.audit("instructor-1", "view_queue", None, at=dt.datetime(2027, 3, 1, tzinfo=dt.UTC))
    policy = RetentionPolicy(pilot_end=dt.date(2030, 1, 1))
    store.purge(dt.datetime(2028, 2, 29, 12, tzinfo=dt.UTC), policy)
    assert "view_queue" in [e.action for e in store.audit_events()]
    store.purge(dt.datetime(2028, 3, 1, 12, tzinfo=dt.UTC), policy)
    assert "view_queue" not in [e.action for e in store.audit_events()]


def test_naive_times_are_refused(store):
    with pytest.raises(ValueError):
        store.purge(dt.datetime(2027, 1, 1), RetentionPolicy(pilot_end=dt.date(2026, 12, 31)))


# ---------- verifier pass 2 regressions ----------


@pytest.mark.privacy
def test_a_deletion_cannot_slip_between_owner_lookup_and_write(tmp_path):
    path = tmp_path / "race.sqlite"
    s1, s2 = Store(path), Store(path, timeout=0.1)
    s1.put_conversation(conv("c-aaaaaaaaaaaa"))
    s1.put_question(question("q-aaaaaaaaaaa1", "c-aaaaaaaaaaaa"))
    real_owner = s1._owner
    blocked = []

    def owner_then_try_delete(qid):
        found = real_owner(qid)
        try:
            s2.delete_history(A, at=T0)
        except sqlite3.OperationalError:
            blocked.append(True)  # the deletion has to wait for the write to finish
        return found

    s1._owner = owner_then_try_delete  # type: ignore[method-assign]
    s1.put_answer(answer("q-aaaaaaaaaaa1"))
    s1._owner = real_owner  # type: ignore[method-assign]
    assert blocked == [True]
    s2.delete_history(A, at=T0)
    assert s1.get_answer("q-aaaaaaaaaaa1") is None  # deleted with its question, no orphan
    s1.close()
    s2.close()


def test_audit_retention_does_not_depend_on_the_callers_timezone(store):
    store.audit("instructor-1", "view_queue", None, at=dt.datetime(2028, 2, 29, 12, tzinfo=dt.UTC))
    policy = RetentionPolicy(pilot_end=dt.date(2030, 1, 1))
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    store.purge(dt.datetime(2029, 3, 1, 5, 0, tzinfo=ist), policy)  # 2029-02-28 23:30 UTC
    assert "view_queue" in [e.action for e in store.audit_events()]


@pytest.mark.privacy
def test_questions_stay_in_their_learners_conversation(store):
    store.put_conversation(conv("c-aaaaaaaaaaaa"))
    with pytest.raises(KeyError):
        store.put_question(question("q-bbbbbbbbbbb1", "c-aaaaaaaaaaaa", pid=B))
    store.put_question(question("q-aaaaaaaaaaa1", "c-aaaaaaaaaaaa"))
    store.put_conversation(conv("c-bbbbbbbbbbbb", B))
    with pytest.raises(KeyError):
        store.put_question(question("q-aaaaaaaaaaa1", "c-bbbbbbbbbbbb", pid=B))


def test_escalation_status_changes_are_audited(store):
    fill(store)
    store.set_escalation_status("q-aaaaaaaaaaa1", "material_gap", "instructor-1", at=T0)
    store.set_escalation_status("q-aaaaaaaaaaa2", "closed", "instructor-1", at=T0)
    assert [e.action for e in store.audit_events()] == ["flag_gap", "close"]


def test_a_partly_migrated_database_still_opens(tmp_path):
    path = tmp_path / "partial.sqlite"
    s = Store(path)
    s._con.execute("delete from schema_migrations where version = 5")
    s.close()
    assert Store(path).schema_version() == len(Store.MIGRATIONS)


@pytest.mark.privacy
def test_purged_text_is_checkpointed_out_of_the_wal(tmp_path):
    path = tmp_path / "wal.sqlite"
    s = Store(path)
    s.put_conversation(conv("c-aaaaaaaaaaaa"))
    q = question("q-aaaaaaaaaaa1", "c-aaaaaaaaaaaa")
    s.put_question(q.model_copy(update={"text": "uniquephrase about my illness"}))
    s.delete_history(A, at=T0)
    wal = path.with_name(path.name + "-wal")
    blob = path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    assert b"uniquephrase" not in blob
    s.close()


# ---------- verifier pass 3 regressions ----------


@pytest.mark.privacy
def test_a_question_cannot_move_to_another_conversation(store):
    store.put_conversation(conv("c-aaaaaaaaaaaa"))
    store.put_conversation(conv("c-aaaaaaaaaaab"))
    store.put_question(question("q-aaaaaaaaaaa1", "c-aaaaaaaaaaaa"))
    store.put_answer(answer("q-aaaaaaaaaaa1"))
    with pytest.raises(KeyError):
        store.put_question(question("q-aaaaaaaaaaa1", "c-aaaaaaaaaaab"))
    store.delete_history(A, conversation_id="c-aaaaaaaaaaaa", at=T0)
    assert store.get_answer("q-aaaaaaaaaaa1") is None


def test_a_conversation_cannot_be_taken_over(store):
    store.put_conversation(conv("c-aaaaaaaaaaaa"))
    with pytest.raises(KeyError):
        store.put_conversation(conv("c-aaaaaaaaaaaa", B))
    assert [c.learner_pid for c in store.conversations_for(A)] == [A]


def test_a_reply_is_not_lost_to_a_status_change(store, monkeypatch):
    fill(store)
    real_get = store.get_escalation
    calls = {"n": 0}

    def get_then_reply(qid):
        calls["n"] += 1
        return real_get(qid)

    monkeypatch.setattr(store, "get_escalation", get_then_reply)
    store.reply("q-aaaaaaaaaaa1", "See slide 9.", "instructor-1", at=T0)
    store.set_escalation_status("q-aaaaaaaaaaa1", "closed", "instructor-1", at=T0)
    e = real_get("q-aaaaaaaaaaa1")
    assert e is not None and e.status == "closed" and e.reply_text == "See slide 9."


def test_checkpoint_with_a_reader_open_is_quick_and_reported(tmp_path):
    import time

    path = tmp_path / "busy.sqlite"
    s = Store(path)
    s.put_conversation(conv("c-aaaaaaaaaaaa"))
    s.put_question(question("q-aaaaaaaaaaa1", "c-aaaaaaaaaaaa"))
    reader = sqlite3.connect(path)
    reader.execute("begin")
    reader.execute("select count(*) from records").fetchone()
    started = time.monotonic()
    s.delete_history(A, at=T0)
    # far under the connection's 30 s timeout; 10 s leaves room for slow CI runners (macOS)
    assert time.monotonic() - started < 10.0
    assert s.last_checkpoint_complete is False
    reader.rollback()
    reader.close()
    report = s.purge(T0, RetentionPolicy(pilot_end=dt.date(2030, 1, 1)))
    assert report.wal_cleared is True
    s.close()


def test_each_audit_event_is_kept_until_its_own_anniversary(store):
    store.audit("instructor-1", "view_queue", None, at=dt.datetime(2028, 2, 29, 12, tzinfo=dt.UTC))
    policy = RetentionPolicy(pilot_end=dt.date(2031, 1, 1))
    store.purge(dt.datetime(2029, 3, 1, 0, 30, tzinfo=dt.UTC), policy)
    assert "view_queue" in [e.action for e in store.audit_events()]
    store.purge(dt.datetime(2029, 3, 1, 12, 0, tzinfo=dt.UTC), policy)
    assert "view_queue" not in [e.action for e in store.audit_events()]


def test_a_failed_cross_check_rolls_the_purge_back(store):
    fill(store)
    store.delete_history(B, at=T0)
    store._con.execute(f"update records set pid = '{A}' where kind = 'deletion'")
    with pytest.raises(ValueError):
        store.purge(dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
                    RetentionPolicy(pilot_end=dt.date(2026, 12, 31)))  # fmt: skip
    assert store.questions_for(A)  # nothing was deleted


# ---------- verifier pass 4 should-fix ----------


@pytest.mark.safety
def test_a_pause_row_with_a_mismatched_scope_fails_closed(store):
    store.set_pause("s03", True, "instructor-1", at=T0)
    store._con.execute("update records set key = 's04' where kind = 'pause' and key = 's03'")
    assert store.is_paused("s04") is True


def test_two_consents_at_the_same_instant_are_both_kept(store):
    store.put_consent(S.Consent(learner_pid=A, notice_version="n1",
                                purposes=("answer_questions",), given_at=T0))  # fmt: skip
    store.put_consent(S.Consent(learner_pid=A, notice_version="n1",
                                purposes=("improve_course",), given_at=T0))  # fmt: skip
    assert len(store.consents_for(A)) == 2
