"""The learner chat page (B2.5). A thin Streamlit view: every decision is made in
`course_assistant.app.chat`, so this file only draws what it returns.

    uv run --extra app --extra openai streamlit run src/course_assistant/app/learner_page.py

Order on every rerun: the passkey (nothing else is drawn until it passes), the sidebar (own
key, sign out), the notice until consent, the session picker and "New conversation", the
chat, and "Your history". Only Streamlit's own widgets, each with a visible label; learner
and model text always go through `plain`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import streamlit as st

from course_assistant.app import wiring
from course_assistant.app.chat import STATE_KEY, Card, ChatState, LearnerChat, Shown, plain
from course_assistant.demo._errors import GuardRefused

st.set_page_config(page_title="Course assistant")
SECRET_INPUTS = ("ca_passkey_input", "ca_own_key_input")
FORGET = "ca_forget_inputs"
FLASH = "ca_flash"
T = TypeVar("T")


@st.cache_resource
def _from_env() -> LearnerChat:
    return wiring.from_env(secrets_store=_secrets())


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


def _forget_secret_inputs() -> None:
    """A submitted passkey or key is dropped from the widget state on the next run, before
    any widget is drawn (Streamlit clears a form only in the browser)."""
    if st.session_state.pop(FORGET, False):
        for k in SECRET_INPUTS:
            st.session_state.pop(k, None)


def _submitted(action: Callable[[], Any]) -> None:
    """Run a passkey or key action, keep its refusal for the next run, forget the input."""
    try:
        action()
    except GuardRefused as exc:
        st.session_state[FLASH] = exc.reason
    st.session_state[FORGET] = True
    st.rerun()


def _flash() -> None:
    reason = st.session_state.pop(FLASH, None)
    if reason:
        st.error(reason)


def _passkey(chat: LearnerChat, state: ChatState) -> None:
    st.title("Course assistant")
    _flash()
    reason = chat.locked_reason(state)
    if reason and "closed" in reason:
        st.info(reason)
    with st.form("passkey", clear_on_submit=True):
        st.text_input("Passkey", type="password", key=SECRET_INPUTS[0],
                      help="The numeric passkey the course team gave you.")  # fmt: skip
        unlocked = st.form_submit_button("Unlock")
    if unlocked:
        code = st.session_state.get(SECRET_INPUTS[0]) or ""
        _submitted(lambda: chat.unlock(state, code, ip=_ip()))


def _sidebar(chat: LearnerChat, state: ChatState) -> None:
    with st.sidebar:
        st.subheader("Your own OpenAI key")
        if state.own_key.is_set:
            st.caption("Using your own key for this session.")
            if st.button("Forget my key"):
                chat.forget_own_key(state)
                st.rerun()
        else:
            with st.form("own_key", clear_on_submit=True):
                st.text_input(
                    "Your own OpenAI API key (optional)", type="password", key=SECRET_INPUTS[1],
                    help="Used for this browser session only: never stored or logged, and "
                         "cleared when you sign out. You still need the passkey.",
                )  # fmt: skip
                use = st.form_submit_button("Use my key")
            if use:
                key = st.session_state.get(SECRET_INPUTS[1]) or ""
                _submitted(lambda: chat.set_own_key(state, key))
        st.divider()
        if st.button("Sign out"):
            chat.sign_out(state)
            st.rerun()


def _notice(chat: LearnerChat, state: ChatState) -> None:
    st.header("Before you start")
    st.markdown(plain(chat.notice(state)))
    with st.form("notice"):
        improve = st.checkbox(
            "Also count my questions, in groups of five or more learners, to help improve the "
            "course (optional)"
        )
        agreed = st.form_submit_button("I agree and continue")
    if agreed and _safely(lambda: chat.accept_notice(state, improve_course=improve)):
        st.rerun()


def _card(chat: LearnerChat, state: ChatState, shown: Shown, n: int) -> None:
    card: Card = shown.card
    with st.chat_message("user"):
        st.markdown(plain(shown.question))
    with st.chat_message("assistant"):
        st.markdown(f"**{plain(card.heading)}**")
        if card.ai_label:
            st.caption(card.ai_label)
        st.markdown(plain(card.text))
        for chip in card.chips:
            with st.expander(f"Source: {chip.label}"):
                st.markdown(plain(chip.text))
        if card.check_note:
            st.caption(card.check_note)
        if card.can_escalate and card.question_id:
            if shown.escalated:
                st.success("Sent to the teaching team. They will reply here, in your history.")
            elif st.button("Ask the teaching team", key=f"escalate-{n}") and _safely(
                lambda: chat.escalate(state, card.question_id)
            ):
                st.rerun()


def _history(chat: LearnerChat, state: ChatState) -> None:
    waiting = chat.replies_waiting(state)
    title = "Your history" + (f" ({waiting} replies from the teaching team)" if waiting else "")
    with st.expander(title):
        past = _safely(lambda: chat.history(state)) or ()
        if not past:
            st.markdown("Nothing stored yet.")
        for i, conv in enumerate(past):
            st.markdown(f"**Conversation {i + 1}** ({plain(conv.scope_label)}, started "
                        f"{conv.started_at:%d %b %Y %H:%M} UTC)")  # fmt: skip
            for t in conv.turns:
                st.markdown(f"You asked: {plain(t.question)}")
                st.markdown(f"{plain(t.icon)} {plain(t.status_label)}")
                if t.text:
                    st.markdown(plain(t.text))
                if t.handover:
                    st.caption(t.handover)
                if t.reply and t.reply_label:
                    st.markdown(f"**{plain(t.reply_label)}:** {plain(t.reply)}")
            if st.button("Delete this conversation", key=f"delete-{conv.conversation_id}") and (
                _safely(lambda c=conv.conversation_id: chat.delete(state, c))
            ):
                st.rerun()
        st.divider()
        sure = st.checkbox("Yes, delete all my history")
        if st.button("Delete all my history", disabled=not sure) and _safely(
            lambda: chat.delete(state)
        ):
            st.session_state["ca_deleted"] = True
            st.rerun()
        if st.button("Withdraw my consent") and _safely(lambda: chat.withdraw(state)) is not None:
            st.rerun()


def main() -> None:
    chat = wiring.controller(_from_env)
    state: ChatState = st.session_state.setdefault(STATE_KEY, ChatState.new())
    _forget_secret_inputs()
    if chat.locked_reason(state) is not None:
        _passkey(chat, state)
        return
    _sidebar(chat, state)
    _flash()
    if chat.needs_notice(state):
        _notice(chat, state)
        return
    st.title("Course assistant")
    if st.session_state.pop("ca_deleted", False):
        st.success("Your history is deleted.")
    options = chat.session_options(state)
    current = chat.scope_label(state)
    label = st.selectbox("Search in", options, index=options.index(current),
                         help="Pick one session, or search all of them.")  # fmt: skip
    if label != current:
        _safely(lambda: chat.choose_scope(state, label))
        st.rerun()
    if st.button("New conversation"):
        _safely(lambda: chat.new_conversation(state))
        st.rerun()
    for n, shown in enumerate(state.turns):
        _card(chat, state, shown, n)
    st.caption("Type your question in the box below and press Enter.")
    question = st.chat_input("Ask about the course materials")
    if question and _safely(lambda: chat.ask(state, question)):
        st.rerun()
    _history(chat, state)


main()
