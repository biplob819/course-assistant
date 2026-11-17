"""B2.3 teaching-team view logic: TTV-01 to TTV-05 and the staff passkey (the page itself is
in test_team_page.py)."""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from course_assistant.answer.ask import Assistant
from course_assistant.app import chat as chat_mod
from course_assistant.app import team as team_mod
from course_assistant.app import team_wiring
from course_assistant.app.chat import ChatState
from course_assistant.app.team import (
    QUEUE_STATUS,
    REASON_LABELS,
    SAME_PASSKEY,
    TeachingTeam,
    TeamState,
    sources_for,
    staff_hash,
)
from course_assistant.demo._errors import GuardRefused
from course_assistant.demo.passkey import CLOSED, NEEDED, WRONG, PasskeyGate, hash_passcode
from course_assistant.escalation import MAX_REPLY_CHARS, TEAM_LABEL
from course_assistant.schemas import DigestRow
from course_assistant.schemas.people import Draft
from course_assistant.schemas.request import Citation, SearchHit
from tests.answer.test_ask import CONFIG, CORPUS, QUESTION, S01, S03
from tests.app.rig import KINDS, queues
from tests.app.team_rig import NOW, STAFF_CODE, STAFF_HASH, WEEK, TeamRig, manifest
from tests.demo.test_demo import CODE, FAST, HASH, SALT, Secret

REPLY = "Pricing is not in this course; see the reading list for Session 3."
NOT_COVERED_DRAFT = "The draft names the user first."
GATED = ("queue", "reply", "flag_gap", "close", "weeks", "digest", "manifest_view", "pauses",
         "set_pause", "ops")  # fmt: skip


def unit(file_id: str) -> Any:
    return next(u for u in CORPUS.units if u.file_id == file_id)


def actions(rig: TeamRig) -> list[str]:
    return [e.action for e in rig.audits()]


def calls_for(name: str, rig: TeamRig, st: TeamState) -> Any:
    args: dict[str, tuple[Any, ...]] = {
        "queue": (), "reply": ("q-0123456789abcdef", REPLY), "flag_gap": ("q-0123456789abcdef",),
        "close": ("q-0123456789abcdef",), "weeks": (), "digest": (WEEK,), "manifest_view": (),
        "pauses": (), "set_pause": ("all", True), "ops": (),
    }  # fmt: skip
    kw: dict[str, Any] = {"confirmed": True} if name == "set_pause" else {}
    if name in ("reply", "flag_gap", "close"):
        kw["seen"] = "open"
    return lambda: getattr(rig.team, name)(st, *args[name], **kw)


def digest_rows(week: str = WEEK) -> list[DigestRow]:
    return [
        DigestRow(week=week, session="s01", topic="Naming the user problem", count=6, gaps=1),
        DigestRow(week=week, session="s03", topic="Incident runbook steps", count=5),
    ]


# ======================= the staff passkey (every criterion) =======================


def test_every_public_method_needs_the_staff_passkey_first():
    exempt = {"unlock", "sign_out", "locked_reason"}
    public = {n for n, _ in inspect.getmembers(TeachingTeam, inspect.isfunction)
              if not n.startswith("_")}  # fmt: skip
    assert public - exempt == set(GATED)


@pytest.mark.safety
@pytest.mark.parametrize("name", GATED)
def test_no_method_reads_or_writes_without_the_staff_passkey(tmp_path, name):
    rig = TeamRig(tmp_path, owner=KINDS["not_covered"])
    qid = rig.escalated()
    before = rig.store().audit_events()
    with pytest.raises(GuardRefused) as exc:
        calls_for(name, rig, rig.state)()
    assert exc.value.reason == NEEDED
    assert rig.store().audit_events() == before
    assert rig.store().get_escalation(qid).status == "open"  # type: ignore[union-attr]
    assert not rig.store().is_paused(None)


@pytest.mark.safety
def test_the_learner_passkey_and_a_learner_session_never_open_the_staff_view(tmp_path):
    rig = TeamRig(tmp_path, owner=KINDS["not_covered"])
    learner = rig.learners.ready()  # a learner unlocked on the learner gate
    st = TeamState(token=learner.token)  # even with the learner's own gate token
    assert rig.team.locked_reason(st) == NEEDED
    with pytest.raises(GuardRefused, match=WRONG):
        rig.team.unlock(st, CODE)  # the learner passkey
    with pytest.raises(GuardRefused, match=NEEDED):
        rig.team.queue(st)
    # and the staff passkey never opens the learner page
    other = ChatState.new()
    with pytest.raises(GuardRefused, match="Wrong passkey"):
        rig.learners.chat.unlock(other, STAFF_CODE)
    assert rig.learners.chat.locked_reason(other) == NEEDED
    rig.unlock()
    assert rig.learners.chat.locked_reason(ChatState(token=rig.state.token, pid="0" * 16)) == NEEDED
    assert "staff_unlock" in actions(rig)


@pytest.mark.safety
def test_a_staff_hash_equal_to_or_opened_by_the_learner_code_keeps_the_page_closed(tmp_path):
    env = {"STAFF_PASSKEY_HASH": HASH, "DEMO_PASSKEY_HASH": HASH}
    assert staff_hash(env)() is None  # the same hash string: closed
    assert staff_hash({"DEMO_PASSKEY_HASH": HASH})() is None  # unset: closed
    assert staff_hash({"STAFF_PASSKEY_HASH": STAFF_HASH, "DEMO_PASSKEY_HASH": HASH})() == STAFF_HASH
    gate = PasskeyGate(staff_hash(env))
    st = TeamState.new()
    with pytest.raises(GuardRefused, match=CLOSED):
        gate.unlock(CODE, session=st.token)
    # the learner code under another salt: the staff gate opens, then the view locks it again
    resalted = hash_passcode(CODE, salt=b"fedcba9876543210", iterations=FAST)
    rig = TeamRig(tmp_path)
    rig.staff_secret.value = resalted
    with pytest.raises(GuardRefused) as exc:
        rig.team.unlock(st, CODE)
    assert exc.value.reason == SAME_PASSKEY
    assert rig.team.locked_reason(st) == NEEDED and "staff_unlock" not in actions(rig)


def test_unlock_and_sign_out_are_audited_and_sign_out_gives_a_new_token(tmp_path):
    rig = TeamRig(tmp_path)
    st = rig.unlock()
    old = st.token
    assert rig.team.locked_reason(st) is None
    rig.team.sign_out(st)
    assert st.token != old and rig.team.locked_reason(st) == NEEDED
    assert rig.team.locked_reason(TeamState(token=old)) == NEEDED
    rig.team.sign_out(st)  # signing out a locked session audits nothing more
    assert actions(rig) == ["staff_unlock", "staff_sign_out"]
    assert {e.actor for e in rig.audits()} == {"instructor-1"}
    # changing the staff secret revokes every staff session
    rig.unlock(st)
    rig.staff_secret.value = hash_passcode("99887766", salt=SALT, iterations=FAST)
    assert rig.team.locked_reason(st) == NEEDED


def test_a_role_that_is_not_a_role_id_is_refused(tmp_path):
    rig = TeamRig(tmp_path)
    with pytest.raises(ValueError):
        TeachingTeam(rig.gate, store=rig.learners.stores, corpus=CORPUS, manifest=manifest(),
                     config=CONFIG, role="Dr Jane Smith")  # fmt: skip


# ======================= TTV-01 queue and reply =======================


def test_the_queue_shows_question_reason_draft_and_sources_with_exact_unit_text(tmp_path):
    rig = TeamRig(tmp_path, owner=KINDS["not_covered"])
    qid = rig.escalated()
    st = rig.unlock()
    rows = rig.team.queue(st)
    assert [r.question_id for r in rows] == [qid]
    row = rows[0]
    assert row.question == "Is there a session on pricing models?"
    assert row.reason_label == REASON_LABELS["not_covered"]
    assert row.status == "open" and row.heading == " ".join(QUEUE_STATUS["open"])
    assert row.draft == NOT_COVERED_DRAFT and not row.sources_are_hits
    assert [c.text for c in row.sources] == [unit(S01).text]
    assert row.sources[0].label == unit(S01).label and row.sources[0].found
    assert row.can_reply and row.can_flag and row.can_close and row.reply is None
    assert "view_queue" in actions(rig)


def test_reply_reaches_the_learner_labelled_from_the_teaching_team(tmp_path):
    rig = TeamRig(tmp_path, owner=KINDS["not_covered"])
    qid = rig.escalated()
    st = rig.unlock()
    rig.team.reply(st, qid, REPLY, seen="open")
    e = rig.store().get_escalation(qid)
    assert e.status == "replied" and e.reply_text == REPLY  # type: ignore[union-attr]
    assert e.replied_by == "instructor-1"  # type: ignore[union-attr]
    learner = rig.learners.state
    past = rig.learners.chat.history(learner)
    turn = past[0].turns[0]
    assert turn.reply == REPLY and turn.reply_label == TEAM_LABEL
    assert rig.learners.chat.replies_waiting(learner) == 1
    assert rig.team.queue(st) == ()  # no longer waiting
    row = rig.team.queue(st, None)[0]
    assert row.reply == REPLY and not row.can_reply and row.status == "replied"
    rig.team.flag_gap(st, qid, seen="replied")
    rig.team.close(st, qid, seen="material_gap")
    assert rig.store().get_escalation(qid).status == "closed"  # type: ignore[union-attr]
    assert [a for a in actions(rig) if a in ("reply", "flag_gap", "close")] == [
        "reply", "flag_gap", "close"]  # fmt: skip


def test_a_blank_too_long_or_second_reply_is_refused_and_nothing_changes(tmp_path):
    rig = TeamRig(tmp_path, owner=KINDS["not_covered"])
    qid = rig.escalated()
    st = rig.unlock()
    for bad in ("", "   ", "​", "x" * (MAX_REPLY_CHARS + 1)):
        with pytest.raises(GuardRefused, match="visible text"):
            rig.team.reply(st, qid, bad, seen="open")
    assert rig.store().get_escalation(qid).status == "open"  # type: ignore[union-attr]
    rig.team.reply(st, qid, REPLY, seen="open")
    with pytest.raises(GuardRefused, match="cannot change"):
        rig.team.reply(st, qid, "A second reply.", seen="replied")
    with pytest.raises(GuardRefused, match="no longer in the queue"):
        rig.team.reply(st, "q-0123456789abcdef", REPLY, seen="open")
    assert rig.store().get_escalation(qid).reply_text == REPLY  # type: ignore[union-attr]
    with pytest.raises(GuardRefused):
        rig.team.queue(st, "everything")


def test_a_draft_without_citations_shows_its_closest_search_hits(tmp_path):
    cited = Draft(text="d", citations=(Citation(file_id=S01, unit_index=1, label="L",
                                                session="s01"),))  # fmt: skip
    chips, hits = sources_for(cited, CORPUS)
    assert not hits and chips[0].text == unit(S01).text
    found = (SearchHit(file_id=S01, score=0.2, unit_index=1),
             SearchHit(file_id=S03, score=0.9, unit_index=4),
             SearchHit(file_id="file-gone", score=0.5, unit_index=9),
             SearchHit(file_id=S01, score=0.1, unit_index=1))  # fmt: skip
    chips, hits = sources_for(Draft(text="d", hits=found), CORPUS)
    assert hits and len(chips) == 3
    assert [c.text for c in chips] == [unit(S03).text, chat_mod.MISSING_UNIT, unit(S01).text]
    assert sources_for(None, CORPUS) == ((), False)


@pytest.mark.safety
def test_queue_rows_and_audits_hold_no_learner_id_and_audits_hold_no_text(tmp_path):
    rig = TeamRig(tmp_path, owner=queues("not_covered", "admin"))
    qid = rig.escalated()
    st = rig.learners.state
    rig.learners.chat.ask(st, "Can I move my deadline? My father is in hospital.")
    team = rig.unlock()
    rows = rig.team.queue(team)
    assert len(rows) == 2 and rows[1].reason_label == REASON_LABELS["admin"]
    assert st.pid not in repr(rows)
    rig.team.reply(team, qid, REPLY, seen="open")
    rig.team.digest(team, WEEK)
    rig.team.manifest_view(team)
    rig.team.pauses(team)
    rig.team.set_pause(team, "s01", True, confirmed=True)
    rig.team.ops(team)
    rig.team.sign_out(team)
    staff = [e for e in rig.audits() if e.actor == "instructor-1"]
    blob = json.dumps([e.model_dump(mode="json") for e in staff])
    assert {e.action for e in staff} == {"staff_unlock", "view_queue", "reply", "view_digest",
                                         "view_manifest", "view_pauses", "pause", "view_ops",
                                         "staff_sign_out"}  # fmt: skip
    for text in (REPLY, "pricing", "hospital", NOT_COVERED_DRAFT, st.pid):
        assert text not in blob
    assert {e.target for e in staff} <= {None, qid, "s01"}


# ======================= TTV-02 digest =======================


def test_the_digest_shows_the_weeks_rows_with_counts_and_gaps(tmp_path):
    rig = TeamRig(tmp_path)
    rig.store().put_digest(WEEK, digest_rows(), k=5, at=NOW)
    st = rig.unlock()
    lines = rig.team.digest(st, WEEK)
    assert [(x.session_label, x.topic, x.count, x.gaps) for x in lines] == [
        ("Session 1", "Naming the user problem", 6, 1),
        ("Session 3", "Incident runbook steps", 5, 0)]  # fmt: skip
    assert rig.team.digest(st, "2026-W46") == ()
    assert rig.team.weeks(st)[:2] == [WEEK, "2026-W46"] and len(rig.team.weeks(st)) == 8
    assert actions(rig).count("view_digest") == 2
    for bad in ("2026-W60", "last week", ""):
        with pytest.raises(GuardRefused, match="week"):
            rig.team.digest(st, bad)


def test_a_configured_k_above_5_hides_smaller_rows(tmp_path):
    rig = TeamRig(tmp_path, config=CONFIG.model_copy(update={"digest_k": 6}))
    rig.store().put_digest(WEEK, digest_rows(), k=5, at=NOW)
    st = rig.unlock()
    assert [x.count for x in rig.team.digest(st, WEEK)] == [6]


@pytest.mark.privacy
def test_a_digest_row_below_k_is_never_shown_even_if_it_reaches_the_store(tmp_path):
    rig = TeamRig(tmp_path)
    store = rig.store()
    store.put_digest(WEEK, digest_rows(), k=5, at=NOW)
    small = DigestRow(week=WEEK, session="s02", topic="Labelling judges", count=4)
    with pytest.raises(ValueError):
        store.put_digest(WEEK, [small], k=5, at=NOW)  # B2.4's store refuses it
    with store._tx():  # planted past the store's own check
        store._put("digest", f"{WEEK}:000099", small, at=NOW)
    st = rig.unlock()
    lines = rig.team.digest(st, WEEK)
    assert "Labelling judges" not in repr(lines) and all(x.count >= 5 for x in lines)
    assert len(lines) == 2


# ======================= TTV-03 manifest view =======================


def test_the_manifest_view_shows_entries_units_and_the_live_corpus_version(tmp_path):
    rig = TeamRig(tmp_path)
    st = rig.unlock()
    view = rig.team.manifest_view(st)
    assert view.cohort == "sample" and view.corpus_version == CORPUS.corpus_version
    assert [(x.manifest_id, x.session_label, x.kind, x.units) for x in view.lines] == [
        ("m-s01-slides", "Session 1", "slides", 2), ("m-s02-agenda", "Session 2", "agenda", 0),
        ("m-s03-slides", "Session 3", "slides", 1)]  # fmt: skip
    first = view.lines[0]
    assert (first.path, first.licence, first.approved_by) == (
        "course/m-s01-slides.pdf", "CC-BY-4.0", "owner-1")  # fmt: skip
    assert str(first.approved_at) == "2026-11-15" and first.speaker_notes is False
    assert view.units == len(CORPUS.units) and view.unlisted_units == 1
    assert actions(rig) == ["staff_unlock", "view_manifest"]


def test_the_view_has_no_way_to_change_the_manifest_or_the_corpus(tmp_path):
    source = Path(inspect.getfile(team_mod)).read_text(encoding="utf-8")
    for writer in ("save_ledger", ".save(", "write_text", "open(", "upload", "ingest(",
                   "model_copy", "put_digest", "delete_history"):  # fmt: skip
        assert writer not in source, writer
    public = {n for n, _ in inspect.getmembers(TeachingTeam, inspect.isfunction)
              if not n.startswith("_")}  # fmt: skip
    assert not any(w in n for n in public for w in ("edit", "add", "remove", "approve"))
    rig = TeamRig(tmp_path)
    m = rig.team.manifest
    rig.team.manifest_view(rig.unlock())
    assert rig.team.manifest == m and rig.team.corpus == CORPUS


def test_wiring_refuses_a_manifest_of_another_cohort(tmp_path):
    rig = TeamRig(tmp_path)
    other = manifest().model_copy(update={"cohort": "other"})
    with pytest.raises(ValueError):
        TeachingTeam(rig.gate, store=rig.learners.stores, corpus=CORPUS, manifest=other,
                     config=CONFIG)  # fmt: skip
    with pytest.raises(team_wiring.WiringError):
        team_wiring.from_env({})


# ======================= TTV-04 pause switches =======================


def test_pause_switches_list_all_and_each_session_and_need_the_confirm(tmp_path):
    rig = TeamRig(tmp_path)
    st = rig.unlock()
    lines = rig.team.pauses(st)
    assert [(x.scope, x.scope_label, x.paused) for x in lines] == [
        ("all", "All sessions", False), ("s01", "Session 1", False),
        ("s02", "Session 2", False), ("s03", "Session 3", False)]  # fmt: skip
    assert lines[0].state_label == "▶ Running"
    with pytest.raises(GuardRefused, match="confirm"):
        rig.team.set_pause(st, "s01", True)
    with pytest.raises(GuardRefused, match="confirm"):
        rig.team.set_pause(st, "s01", True, confirmed="yes")  # type: ignore[arg-type]
    for scope in ("s09", "everything", ""):
        with pytest.raises(GuardRefused, match="session"):
            rig.team.set_pause(st, scope, True, confirmed=True)
    assert not rig.store().is_paused("s01") and "pause" not in actions(rig)
    rig.team.set_pause(st, "s02", True, confirmed=True)
    lines = rig.team.pauses(st)
    assert [x.paused for x in lines] == [False, False, True, False]
    assert lines[2].state_label == "‖ Paused"
    rig.team.set_pause(st, "s02", False, confirmed=True)
    events = [e for e in rig.audits() if e.action in ("pause", "resume")]
    assert [(e.action, e.target, e.actor) for e in events] == [
        ("pause", "s02", "instructor-1"), ("resume", "s02", "instructor-1")]  # fmt: skip


def test_a_paused_scope_gives_learners_the_paused_card(tmp_path):
    rig = TeamRig(tmp_path, owner=queues("answered", "answered"))
    learner = rig.learners.ready()
    st = rig.unlock()
    rig.team.set_pause(st, "s01", True, confirmed=True)
    rig.learners.chat.choose_scope(learner, "Session 1")
    card = rig.learners.chat.ask(learner, QUESTION)
    assert card.status == "paused" and card.text == Assistant.PAUSED_MESSAGE
    rig.learners.chat.choose_scope(learner, "All sessions")  # only "all" pauses every scope
    assert rig.learners.chat.ask(learner, QUESTION).status == "answered"
    rig.team.set_pause(st, "all", True, confirmed=True)
    assert rig.learners.chat.ask(learner, QUESTION).status == "paused"
    assert rig.learners.owner_client.calls  # the answered turn did call the model
    rig.team.set_pause(st, "all", False, confirmed=True)
    rig.team.set_pause(st, "s01", False, confirmed=True)
    assert not rig.store().is_paused("s01")


@pytest.mark.safety
def test_an_unreadable_pause_state_shows_as_paused(tmp_path, caplog):
    rig = TeamRig(tmp_path)
    st = rig.unlock()
    store = rig.store()
    rig.team.set_pause(st, "s03", True, confirmed=True)
    with store._tx():  # a row whose key and record disagree
        store._con.execute("update records set key = 's01' where kind = 'pause' and key = 's03'")
    with caplog.at_level(logging.WARNING):
        lines = rig.team.pauses(st)
    s01 = next(x for x in lines if x.scope == "s01")
    assert s01.paused and s01.unreadable and "could not be read" in s01.state_label
    assert store.is_paused("s01")  # the learner side fails closed too (N1.5)
    assert "ValueError" in caplog.text


# ======================= TTV-05 ops tile =======================


def test_the_ops_tile_counts_questions_statuses_handovers_p95_and_spend(tmp_path):
    rig = TeamRig(tmp_path, owner=queues("answered", "not_covered", "admin", "answered"),
                  spend=lambda: 1.25)  # fmt: skip
    learner = rig.learners.ready()
    chat = rig.learners.chat
    chat.ask(learner, QUESTION)
    nc = chat.ask(learner, "Is there a session on pricing models?")
    chat.escalate(learner, nc.question_id)
    chat.ask(learner, "Can I move my deadline?")
    chat.ask(learner, "And the second slide?")
    st = rig.unlock()
    tile = rig.team.ops(st)
    store = rig.store()
    answers = [store.get_answer(q.question_id) for q in store.questions_for(learner.pid)]
    assert tile.questions == 4 and tile.answers == 4
    mix = dict(tile.status_mix)
    assert mix["Answer from the course materials"] == 2
    assert mix["Not covered by the course materials"] == 1
    assert mix["Sent to the teaching team"] == 1 and mix["Could not answer"] == 0
    assert tile.handed_over == 2 and tile.handover_share == 0.5
    lat = sorted(a.latency_ms for a in answers)  # type: ignore[union-attr]
    assert tile.p95_latency_ms == lat[-1]
    assert tile.spend_usd == pytest.approx(sum(a.cost_usd for a in answers) + 1.25)  # type: ignore[union-attr]
    assert actions(rig)[-1] == "view_ops"


def test_an_empty_store_shows_zeros_and_no_p95(tmp_path):
    rig = TeamRig(tmp_path)
    tile = rig.team.ops(rig.unlock())
    assert (tile.questions, tile.answers, tile.handed_over) == (0, 0, 0)
    assert tile.handover_share is None and tile.p95_latency_ms is None
    assert tile.spend_usd == 0.0 and all(n == 0 for _, n in tile.status_mix)


@pytest.mark.safety
def test_a_bad_spend_source_is_refused_with_a_fixed_reason_and_logged_by_type(tmp_path, caplog):
    for bad in (lambda: float("nan"), lambda: -1.0, lambda: 1 / 0):
        rig = TeamRig(tmp_path, spend=bad)
        st = rig.unlock()
        with caplog.at_level(logging.WARNING), pytest.raises(GuardRefused) as exc:
            rig.team.ops(st)
        assert exc.value.reason == team_mod.READ_FAILED
        assert "view_ops" not in actions(rig)
    assert "nan" not in caplog.text.lower().replace("ops read failed", "")


def test_secrets_never_come_from_a_file_and_unset_staff_hash_keeps_the_gate_closed():
    source = Path(inspect.getfile(team_wiring)).read_text(encoding="utf-8")
    assert "read_text" not in source and "open(" not in source
    assert staff_hash({}, {"STAFF_PASSKEY_HASH": STAFF_HASH})() == STAFF_HASH  # host secrets
    gate = PasskeyGate(staff_hash({}))
    with pytest.raises(GuardRefused, match=CLOSED):
        gate.require(TeamState.new().token)
    assert Secret(STAFF_HASH)() == STAFF_HASH and STAFF_HASH != HASH


# ==================== verifier pass 1: one audit per view, store failures ====================

VIEWS = ("queue", "digest", "manifest_view", "pauses", "ops")
ACTIONS = ("reply", "flag_gap", "close", "set_pause")
NO_STORE = ("weeks",)  # computes dates only: reads and shows nothing from the store


def test_every_gated_method_is_classed_as_a_view_an_action_or_storeless():
    assert set(VIEWS) | set(ACTIONS) | set(NO_STORE) == set(GATED)
    assert len(VIEWS) + len(ACTIONS) + len(NO_STORE) == len(GATED)


@pytest.mark.safety
@pytest.mark.parametrize("name", VIEWS + ACTIONS)
def test_every_view_and_action_writes_exactly_one_audit_row(tmp_path, name):
    rig = TeamRig(tmp_path, owner=KINDS["not_covered"])
    qid = rig.escalated()
    st = rig.unlock()
    before = len(rig.audits())
    args: dict[str, tuple[Any, ...]] = {"reply": (qid, REPLY), "flag_gap": (qid,),
                                        "close": (qid,), "digest": (WEEK,),
                                        "set_pause": ("s01", True)}  # fmt: skip
    kw: dict[str, Any] = {"confirmed": True} if name == "set_pause" else {}
    if name in ACTIONS and name != "set_pause":
        kw["seen"] = "open"
    getattr(rig.team, name)(st, *args.get(name, ()), **kw)
    new = rig.audits()[before:]
    assert len(new) == 1, (name, [e.action for e in new])
    assert new[0].actor == "instructor-1"
    expected = {"queue": "view_queue", "digest": "view_digest", "manifest_view": "view_manifest",
                "pauses": "view_pauses", "ops": "view_ops", "reply": "reply",
                "flag_gap": "flag_gap", "close": "close", "set_pause": "pause"}  # fmt: skip
    assert new[0].action == expected[name]


def test_weeks_reads_nothing_from_the_store(tmp_path):
    rig = TeamRig(tmp_path)
    st = rig.unlock()
    before = len(rig.audits())
    rig.team.store = broken_store
    assert rig.team.weeks(st)[0] == WEEK
    rig.team.store = rig.learners.stores
    assert len(rig.audits()) == before


def broken_store() -> Any:
    raise OSError("unable to open database file /private/secret/path")


@pytest.mark.safety
@pytest.mark.parametrize("name", [n for n in GATED if n not in NO_STORE and n != "pauses"])
def test_a_store_that_will_not_open_gives_a_fixed_refusal_never_a_crash(tmp_path, caplog, name):
    rig = TeamRig(tmp_path)
    st = rig.unlock()
    rig.team.store = broken_store
    with caplog.at_level(logging.WARNING), pytest.raises(GuardRefused) as exc:
        calls_for(name, rig, st)()
    assert exc.value.reason == team_mod.READ_FAILED
    assert "secret" not in caplog.text and "OSError" in caplog.text


@pytest.mark.safety
def test_pauses_fail_closed_when_the_store_will_not_open(tmp_path):
    rig = TeamRig(tmp_path)
    st = rig.unlock()
    rig.team.store = broken_store
    lines = rig.team.pauses(st)
    assert len(lines) == 4 and all(x.paused and x.unreadable for x in lines)


@pytest.mark.safety
def test_unlock_with_a_store_that_will_not_open_is_refused_and_stays_locked(tmp_path):
    rig = TeamRig(tmp_path)
    rig.team.store = broken_store
    st = TeamState.new()
    with pytest.raises(GuardRefused) as exc:
        rig.team.unlock(st, STAFF_CODE)
    assert exc.value.reason == team_mod.READ_FAILED
    assert rig.team.locked_reason(st) == NEEDED  # never open without its audit row
    rig.team.store = rig.learners.stores
    rig.unlock(st)
    rig.team.store = broken_store
    rig.team.sign_out(st)  # signing out still locks, even when its audit cannot be written
    assert rig.team.locked_reason(st) == NEEDED


# ==================== verifier pass 2: actions act on the question seen ====================


@pytest.mark.safety
def test_an_action_on_a_question_that_changed_since_it_was_seen_is_refused(tmp_path):
    rig = TeamRig(tmp_path, owner={k: list(v) * 3 for k, v in KINDS["not_covered"].items()})
    q1 = rig.escalated("Is there a session on pricing models?")
    q2 = rig.escalated("Is there a session on hiring a team?")
    me, other = rig.unlock(), rig.unlock(TeamState.new())
    seen = {r.question_id: r.status for r in rig.team.queue(me)}
    rig.team.reply(other, q1, REPLY, seen="open")  # another staff session acts first
    before = len(rig.audits())
    for act in (lambda: rig.team.close(me, q1, seen=seen[q1]),
                lambda: rig.team.flag_gap(me, q1, seen=seen[q1]),
                lambda: rig.team.reply(me, q1, "Mine.", seen=seen[q1])):  # fmt: skip
        with pytest.raises(GuardRefused) as exc:
            act()
        assert exc.value.reason == team_mod.CHANGED
    assert len(rig.audits()) == before
    assert rig.store().get_escalation(q1).reply_text == REPLY  # type: ignore[union-attr]
    rig.team.close(me, q2, seen=seen[q2])  # the question seen is the one acted on
    assert rig.store().get_escalation(q2).status == "closed"  # type: ignore[union-attr]
    assert rig.audits()[-1].target == q2
    # a question deleted by its learner since it was seen is refused, not guessed
    q3 = rig.escalated("Is there a session on budgets?")
    seen3 = rig.team.queue(me)[0].status
    rig.learners.chat.delete(rig.learners.state)
    with pytest.raises(GuardRefused) as exc:
        rig.team.close(me, q3, seen=seen3)
    assert exc.value.reason == team_mod.GONE


@pytest.mark.safety
def test_a_queue_row_that_cannot_be_built_gives_a_fixed_refusal(tmp_path, monkeypatch, caplog):
    rig = TeamRig(tmp_path, owner=KINDS["not_covered"])
    rig.escalated()
    st = rig.unlock()

    def bad(*_: Any) -> Any:
        raise KeyError("a question text that must not be logged")

    monkeypatch.setattr(team_mod, "queue_row", bad)
    with caplog.at_level(logging.WARNING), pytest.raises(GuardRefused) as exc:
        rig.team.queue(st)
    assert exc.value.reason == team_mod.READ_FAILED
    assert "KeyError" in caplog.text and "must not be logged" not in caplog.text


# ============== verifier pass 3: the seen status is checked in the transaction ==============


def test_seen_is_a_required_keyword_on_every_escalation_action():
    for name in ("reply", "flag_gap", "close"):
        param = inspect.signature(getattr(TeachingTeam, name)).parameters["seen"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty, name


@pytest.mark.safety
def test_a_seen_status_that_is_not_a_status_is_refused(tmp_path):
    rig = TeamRig(tmp_path, owner=KINDS["not_covered"])
    qid = rig.escalated()
    st = rig.unlock()
    for bad in (None, "", "anything"):
        with pytest.raises(GuardRefused):
            rig.team.close(st, qid, seen=bad)  # type: ignore[arg-type]
    assert rig.store().get_escalation(qid).status == "open"  # type: ignore[union-attr]


@pytest.mark.safety
@pytest.mark.parametrize("action", ["reply", "flag_gap", "close"])
def test_a_change_between_the_read_and_the_write_is_caught_in_the_transaction(
    tmp_path, monkeypatch, action
):
    """Another session changes the escalation after the view's own reads, just before the
    compare-and-set: the seen status is compared inside `update_escalation`."""
    from course_assistant.store import Store

    rig = TeamRig(tmp_path, owner=KINDS["not_covered"])
    qid = rig.escalated()
    st = rig.unlock()
    store = rig.store()
    real = store.update_escalation

    def racing(*args: Any, **kw: Any) -> Any:
        other = Store(rig.learners.stores.path)  # another staff session, its own connection
        other.set_escalation_status(qid, "closed", "ta-2", at=NOW)
        other.close()
        return real(*args, **kw)

    monkeypatch.setattr(store, "update_escalation", racing)
    before = len(rig.audits())
    call = {"reply": lambda: rig.team.reply(st, qid, REPLY, seen="open"),
            "flag_gap": lambda: rig.team.flag_gap(st, qid, seen="open"),
            "close": lambda: rig.team.close(st, qid, seen="open")}[action]  # fmt: skip
    with pytest.raises(GuardRefused) as exc:
        call()
    assert exc.value.reason == team_mod.CHANGED
    e = rig.store().get_escalation(qid)
    assert e.status == "closed" and e.reply_text is None  # type: ignore[union-attr]
    new = [x for x in rig.audits()[before:] if x.actor == "instructor-1"]
    assert new == []  # nothing written or audited by the refused change
