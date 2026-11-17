"""The teaching-team page (B2.3). A thin Streamlit view: every decision is made in
`course_assistant.app.team`, so this file only draws what it returns.

    uv run --extra app streamlit run src/course_assistant/app/team_page.py

Order on every rerun: the staff passkey (nothing else is drawn until it passes; it is not the
learner passkey), the sidebar (sign out), the section picker, and only the picked section, so
only what is shown is read and audited. Only Streamlit's own widgets, each with a visible
label; learner, model, teaching-team and course text (questions, drafts, replies, unit text,
topic labels, manifest paths and licences) only ever reach `st.text`, which renders literally.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import streamlit as st

from course_assistant.app import team_wiring
from course_assistant.app.team import (
    STATE_KEY,
    STATUS_FILTERS,
    QueueRow,
    TeachingTeam,
    TeamState,
)
from course_assistant.demo._errors import GuardRefused

st.set_page_config(page_title="Teaching team")
SECRET_INPUTS = ("ca_staff_passkey_input",)
FORGET = "ca_staff_forget_inputs"
FLASH = "ca_staff_flash"
SECTIONS = ("Queue", "Digest", "Materials", "Pause switches", "Ops")
T = TypeVar("T")


@st.cache_resource
def _from_env() -> TeachingTeam:
    return team_wiring.from_env(secrets_store=_secrets())


def _secrets() -> Any:
    try:
        return dict(st.secrets)
    except Exception:
        return None


def _ip() -> str | None:
    try:
        return st.context.ip_address
    except Exception:
        return None


def _safely(fn: Callable[[], T]) -> T | None:
    """Run one action; a refusal is shown as its readable reason."""
    try:
        return fn()
    except GuardRefused as exc:
        st.error(exc.reason)
        return None


def _done(fn: Callable[[], Any], message: str) -> None:
    """Run an action; on success keep a confirmation for the next run and rerun."""
    try:
        fn()
    except GuardRefused as exc:
        st.error(exc.reason)
        return
    st.session_state[FLASH] = ("success", message)
    st.rerun()


def _forget_secret_inputs() -> None:
    if st.session_state.pop(FORGET, False):
        for k in SECRET_INPUTS:
            st.session_state.pop(k, None)


def _flash() -> None:
    kind, message = st.session_state.pop(FLASH, (None, None))
    if kind == "error":
        st.error(message)
    elif kind == "success":
        st.success(message)


def _passkey(team: TeachingTeam, state: TeamState) -> None:
    st.title("Teaching team")
    _flash()
    reason = team.locked_reason(state)
    if reason and "closed" in reason:
        st.info(reason)
    with st.form("staff_passkey", clear_on_submit=True):
        st.text_input("Teaching-team passkey", type="password", key=SECRET_INPUTS[0],
                      help="The teaching team's own passkey, not the learners' one.")  # fmt: skip
        unlocked = st.form_submit_button("Unlock")
    if unlocked:
        code = st.session_state.get(SECRET_INPUTS[0]) or ""
        try:
            team.unlock(state, code, ip=_ip())
        except GuardRefused as exc:
            st.session_state[FLASH] = ("error", exc.reason)
        finally:
            st.session_state[FORGET] = True  # the typed passkey is forgotten whatever happened
        st.rerun()


def _row(team: TeachingTeam, state: TeamState, row: QueueRow, n: int) -> None:
    with st.container(border=True):
        st.markdown(f"**Question {n + 1}**: {row.heading}")  # fixed labels only
        st.text(row.question)
        st.caption(f"Reason: {row.reason_label}. Search scope: {row.session_label}.")
        st.caption("The assistant's draft (not shown to the learner):")
        st.text(row.draft if row.draft is not None else "No draft was kept.")
        if row.sources:
            st.caption("Closest search results:" if row.sources_are_hits else "Sources cited:")
        for i, chip in enumerate(row.sources, start=1):
            with st.expander(f"Source {i}"):
                st.text(chip.label)
                st.text(chip.text)
        if row.reply is not None:
            st.caption("Your team's reply:")
            st.text(row.reply)
        if row.can_reply:
            with st.form(f"reply-{row.question_id}", clear_on_submit=True):
                text = st.text_area("Reply to the learner", key=f"reply-text-{row.question_id}",
                                    help="The learner sees it in their history, marked as "
                                         "from the teaching team.")  # fmt: skip
                send = st.form_submit_button("Send reply")
            if send:
                _done(lambda: team.reply(state, row.question_id, text), "Reply sent.")
        if row.can_flag and st.button("Flag a gap in the materials", key=f"gap-{n}"):
            _done(lambda: team.flag_gap(state, row.question_id), "Flagged as a gap.")
        if row.can_close and st.button("Close", key=f"close-{n}"):
            _done(lambda: team.close(state, row.question_id), "Closed.")


def _queue(team: TeachingTeam, state: TeamState) -> None:
    st.header("Questions for the teaching team")
    shown = st.selectbox("Show", list(STATUS_FILTERS), index=0)
    rows = _safely(lambda: team.queue(state, STATUS_FILTERS[shown])) or ()
    if not rows:
        st.markdown("Nothing here.")
    for n, row in enumerate(rows):
        _row(team, state, row, n)


def _digest(team: TeachingTeam, state: TeamState) -> None:
    st.header("Weekly digest")
    st.caption("Topics asked by at least five consenting learners in a week. Counts only: no "
               "learner is named and no question is quoted.")  # fmt: skip
    weeks = _safely(lambda: team.weeks(state)) or []
    week = st.selectbox("Week", weeks, index=0)
    lines = _safely(lambda: team.digest(state, week)) if week else None
    if not lines:
        st.markdown("No digest for this week.")
        return
    for line in lines:
        st.text(f"{line.session_label} | {line.topic} | {line.count} learners | "
                f"{line.gaps} flagged gaps")  # fmt: skip


def _materials(team: TeachingTeam, state: TeamState) -> None:
    st.header("Approved materials")
    st.caption("Read-only. The manifest changes only through a reviewed change and an ingest.")
    view = _safely(lambda: team.manifest_view(state))
    if view is None:
        return
    st.text(f"Cohort: {view.cohort}")
    st.text(f"Live corpus version: {view.corpus_version}")
    st.text(f"Units in the live corpus: {view.units}")
    if view.unlisted_units:
        st.warning(f"{view.unlisted_units} live units have no manifest entry. Run the ingest "
                   "reconcile.")  # fmt: skip
    for x in view.lines:
        notes = "speaker notes on" if x.speaker_notes else "speaker notes off"
        st.text(f"{x.manifest_id} | {x.session_label} | {x.kind} | {x.path} | {x.licence} | "
                f"approved by {x.approved_by} on {x.approved_at:%Y-%m-%d} | {notes} | "
                f"{x.units} units")  # fmt: skip


def _pauses(team: TeachingTeam, state: TeamState) -> None:
    st.header("Pause switches")
    st.caption("A paused scope gets a fixed message instead of answers. Every change is logged.")
    lines = _safely(lambda: team.pauses(state)) or ()
    for x in lines:
        with st.container(border=True):
            st.markdown(f"**{x.scope_label}**: {x.state_label}")
            verb = "Resume" if x.paused else "Pause"
            target = "all sessions" if x.scope == "all" else x.scope_label
            # keyed by the action too, so after a change the next one needs a fresh tick
            sure = st.checkbox(f"Yes, {verb.lower()} {target}", key=f"confirm-{x.scope}-{verb}")
            if st.button(f"{verb} {target}", key=f"pause-{x.scope}", disabled=not sure):
                _done(lambda s=x.scope, p=not x.paused, ok=sure: team.set_pause(
                    state, s, p, confirmed=ok), f"{verb}d {target}.")  # fmt: skip


def _ops(team: TeachingTeam, state: TeamState) -> None:
    st.header("Ops")
    tile = _safely(lambda: team.ops(state))
    if tile is None:
        return
    a, b, c = st.columns(3)
    a.metric("Questions", tile.questions)
    share = "n/a" if tile.handover_share is None else f"{tile.handover_share:.0%}"
    b.metric("Sent to the teaching team", share)
    p95 = "n/a" if tile.p95_latency_ms is None else f"{tile.p95_latency_ms / 1000:.1f} s"
    c.metric("Answer time, p95", p95)
    st.metric("Spend to date (USD)", f"{tile.spend_usd:.2f}")
    st.subheader("Status mix")
    for status_name, count in tile.status_mix:  # fixed labels and integers
        st.markdown(f"{status_name}: {count}")


def main() -> None:
    team = team_wiring.controller(_from_env)
    state: TeamState = st.session_state.setdefault(STATE_KEY, TeamState.new(team.role))
    _forget_secret_inputs()
    if team.locked_reason(state) is not None:
        _passkey(team, state)
        return
    with st.sidebar:
        st.caption(f"Signed in as {team.role}.")
        if st.button("Sign out"):
            team.sign_out(state)
            st.rerun()
    st.title("Teaching team")
    _flash()
    section = st.radio("Section", SECTIONS, horizontal=True)
    {"Queue": _queue, "Digest": _digest, "Materials": _materials, "Pause switches": _pauses,
     "Ops": _ops}[section](team, state)  # fmt: skip


main()
