"""The learner chat logic (B2.5, CHT-01 to CHT-06). No Streamlit here: the page only draws
what these functions return, so everything is tested with fakes.

Every `LearnerChat` method except `unlock`, `sign_out` and `locked_reason` calls
`PasskeyGate.require` first, before it reads the store, the corpus or a key (OWNER D-45 and
D-48): no page answers anything without the passkey, whether or not the visitor brought their
own OpenAI key. A visitor's key lives in an `OwnKey` in memory only (never logged, traced,
stored, pickled or copied) and is cleared on sign-out. No question or answer text is logged
(D-14); failures are logged by exception type only.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import secrets
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

from course_assistant.answer.ask import Assistant, Turn
from course_assistant.answer.replay import Source
from course_assistant.answer.route import RouteStep
from course_assistant.demo._errors import GuardRefused
from course_assistant.demo.live import LiveDemo
from course_assistant.demo.passkey import PasskeyGate
from course_assistant.escalation import TEAM_LABEL, EscalationError, escalate, escalate_turn
from course_assistant.escalation import reply_waiting as _reply_waiting
from course_assistant.ingest.corpus import Corpus
from course_assistant.llm import TraceSink
from course_assistant.privacy import (
    NOTICE,
    ConversationHistory,
    PrivacyError,
    allowed,
    delete,
    give,
    history,
    needs_notice,
    withdraw,
)
from course_assistant.schemas import Consent, DeletionRequest, Escalation
from course_assistant.schemas.request import Answer, Citation
from course_assistant.store import Store

LOG = logging.getLogger("course_assistant.app")
STATE_KEY = "ca_chat"
ALL_SESSIONS = "All sessions"
AI_LABEL = "AI-generated from the course materials"
CHECK_NOTE = "Check the cited slide or page before you rely on this."
MISSING_UNIT = "This source is no longer in the live course materials."
NOTICE_FIRST = "Read and accept the notice before you ask a question."
EMPTY_QUESTION = "Type a question first."
TOO_LONG = f"Questions can be up to {RouteStep.MAX_QUESTION_CHARS} characters."
BAD_KEY = "That does not look like an OpenAI API key."
BAD_SCOPE = "Pick a session from the list."
PRIVACY_FAILED = "That could not be done just now. Please try again."
ESCALATION_FAILED = "This question could not be sent to the teaching team."
MAX_KEY_CHARS = 200

# status -> (text icon, text label): status is never shown by colour alone (CHT-05)
STATUS_LABELS: dict[str, tuple[str, str]] = {
    "answered": ("✓", "Answer from the course materials"),
    "not_covered": ("○", "Not covered by the course materials"),
    "hint_only": ("→", "Hint (graded work)"),
    "handed_over": ("↗", "Sent to the teaching team"),
    "error": ("!", "Could not answer"),
    "paused": ("‖", "Paused by the teaching team"),
    "refused": ("✕", "Not sent"),
}
HANDOVER_LABELS: dict[str, str] = {
    "open": "Waiting for the teaching team",
    "replied": "Replied by the teaching team",
    "material_gap": "The teaching team flagged a gap in the materials",
    "closed": "Closed by the teaching team",
}
_FIXED_SHOWN = {"handed_over": Assistant.HANDED_OVER, "error": Assistant.ERROR_MESSAGE}
_SESSION_ID = re.compile(r"^s(\d\d)$")


# ---------- the own key (CHT-06) ----------


class OwnKey:
    """A visitor's OpenAI key, held in memory for this browser session only. It has no repr
    or str that shows it, and refuses pickling and copying, so no session-state store, cache
    or log can write it out."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None = None) -> None:
        self._value = value

    @property
    def is_set(self) -> bool:
        return self._value is not None

    def set(self, value: str) -> None:
        self._value = value

    def clear(self) -> None:
        self._value = None

    def reveal(self) -> str | None:
        """Only to build one request's responder."""
        return self._value

    def __repr__(self) -> str:
        return "OwnKey(set)" if self._value is not None else "OwnKey(empty)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("an own key is never persisted")

    def __copy__(self) -> NoReturn:
        raise TypeError("an own key is never copied")

    def __deepcopy__(self, memo: Any) -> NoReturn:
        raise TypeError("an own key is never copied")


# ---------- cards (CHT-02, CHT-03, CHT-05) ----------


@dataclass(frozen=True)
class SourceChip:
    label: str
    session: str
    file_id: str
    unit_index: int
    text: str  # the unit's exact text from the corpus (D-3), or MISSING_UNIT
    found: bool


@dataclass(frozen=True)
class Card:
    question_id: str | None
    status: str  # an Answer status, "paused" or "refused"
    icon: str
    status_label: str
    text: str
    chips: tuple[SourceChip, ...] = ()
    ai_label: str | None = None
    check_note: str | None = None
    can_escalate: bool = False

    @property
    def heading(self) -> str:
        return f"{self.icon} {self.status_label}"


def _chip(c: Citation, corpus: Corpus) -> SourceChip:
    unit = next((u for u in corpus.units
                 if u.file_id == c.file_id and u.unit_index == c.unit_index), None)  # fmt: skip
    if unit is None:  # never guessed from a near match
        return SourceChip(label=c.label, session=c.session, file_id=c.file_id,
                          unit_index=c.unit_index, text=MISSING_UNIT, found=False)  # fmt: skip
    return SourceChip(label=unit.label, session=unit.session, file_id=unit.file_id,
                      unit_index=unit.unit_index, text=unit.text, found=True)  # fmt: skip


def _card(status: str, text: str, question_id: str | None, **kw: Any) -> Card:
    icon, label = STATUS_LABELS[status]
    return Card(question_id=question_id, status=status, icon=icon, status_label=label,
                text=text, **kw)  # fmt: skip


def card_for(item: Turn | Answer, corpus: Corpus) -> Card:
    """The card for one answer, by the Finalize table (§2.2): an answer or hint carries its
    source chips, the AI label and the check note; a not-covered reply showing NOT_COVERED
    offers the teaching team; fixed texts carry nothing else."""
    a = item.answer if isinstance(item, Turn) else item
    paused = (isinstance(item, Turn) and item.paused) or (
        a.status == "not_covered" and a.text == Assistant.PAUSED_MESSAGE
    )
    if paused:
        return _card("paused", a.text, a.question_id)
    if a.status in ("answered", "hint_only"):
        return _card(a.status, a.text, a.question_id,
                     chips=tuple(_chip(c, corpus) for c in a.citations),
                     ai_label=AI_LABEL, check_note=CHECK_NOTE)  # fmt: skip
    return _card(
        a.status,
        a.text,
        a.question_id,
        can_escalate=a.status == "not_covered" and a.text == Assistant.NOT_COVERED,
    )


def refused_card(reason: str) -> Card:
    return _card("refused", reason, None)


# ---------- sessions (CHT-01) ----------


def _session_label(session: str) -> str:
    m = _SESSION_ID.fullmatch(session)
    return f"Session {int(m[1])}" if m else session


def session_labels(corpus: Corpus) -> list[str]:
    sessions = sorted({u.session for u in corpus.units})
    return [ALL_SESSIONS, *(_session_label(s) for s in sessions)]


def scope_for(label: str, corpus: Corpus) -> str | None:
    """The session id a picker label stands for (None: all sessions); anything else is
    refused."""
    if label == ALL_SESSIONS:
        return None
    for s in sorted({u.session for u in corpus.units}):
        if _session_label(s) == label:
            return s
    raise ValueError("not a session of the live corpus")


# ---------- state ----------


@dataclass
class Shown:
    """One turn this page showed. `turn` (A2.5's, with the kept draft) stays in memory only."""

    question: str
    card: Card
    conversation_id: str | None
    turn: Turn | None = None
    escalated: bool = False


@dataclass
class ChatState:
    """One browser session: a random gate token and a random learner pid (D-13)."""

    token: str
    pid: str
    scope: str | None = None
    conversation_id: str | None = None
    turns: list[Shown] = field(default_factory=list)
    own_key: OwnKey = field(default_factory=OwnKey)

    @classmethod
    def new(cls) -> ChatState:
        return cls(token=secrets.token_urlsafe(24), pid=secrets.token_hex(8))

    def __repr__(self) -> str:
        return "ChatState()"

    __str__ = __repr__


@dataclass(frozen=True)
class PastTurn:
    question_id: str
    question: str
    icon: str
    status_label: str
    text: str | None
    handover: str | None
    reply: str | None
    reply_label: str | None


@dataclass(frozen=True)
class PastConversation:
    conversation_id: str
    started_at: dt.datetime
    scope_label: str
    turns: tuple[PastTurn, ...]


class PerThreadStore:
    """One `Store` per thread on one SQLite file: Streamlit runs each rerun on a new thread,
    and a SQLite connection belongs to the thread that opened it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._local = threading.local()

    def __call__(self) -> Store:
        store = getattr(self._local, "store", None)
        if store is None:
            store = Store(self.path)
            self._local.store = store
        return store

    def __repr__(self) -> str:
        return "PerThreadStore()"


class _NoCalls:
    """The source for store-only Assistant actions (new conversation): never called."""

    def call(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError("no model call is made here")


# ---------- the controller ----------


class LearnerChat:
    def __init__(self, gate: PasskeyGate, demo: LiveDemo, *,
                 assistant_for: Callable[[Source], Assistant], store: Callable[[], Store],
                 traces: Sequence[TraceSink], corpus: Corpus,
                 now: Callable[[], dt.datetime] | None = None,
                 daily_questions: int | None = None) -> None:  # fmt: skip
        if demo.gate is not gate:
            raise ValueError("the live demo must use the same passkey gate")
        if daily_questions is not None and daily_questions < 1:
            raise ValueError("daily_questions must be at least 1")
        self.daily_questions = daily_questions  # per learner per UTC day (pilot codes)
        self.gate = gate
        self.demo = demo
        self.assistant_for = assistant_for
        self.store = store
        self.traces = tuple(traces)
        self.corpus = corpus
        # the time written with consent and escalations (the real clock unless a test pins it)
        self.now = now or (lambda: dt.datetime.now(dt.UTC))

    def __repr__(self) -> str:
        return "LearnerChat()"

    # --- the gate (exempt from require: they are the gate) ---

    def locked_reason(self, state: ChatState) -> str | None:
        """None when this session may use the page; else the gate's reason."""
        try:
            self.gate.require(state.token)
        except GuardRefused as exc:
            return exc.reason
        return None

    def unlock(self, state: ChatState, code: str, ip: str | None = None) -> None:
        self.gate.unlock(code, session=state.token, ip=ip)
        pid = self.gate.identity(state.token)  # a personal code names its learner (D-98)
        if pid is not None:
            state.pid = pid

    def sign_out(self, state: ChatState) -> None:
        """Lock this session and forget everything it held: the own key, the turns shown,
        the conversation, the scope and the learner id, so the next person in the same browser
        starts as a new learner and never sees the earlier history."""
        self.gate.lock_session(state.token)
        state.own_key.clear()
        state.turns.clear()
        state.conversation_id = None
        state.scope = None
        fresh = ChatState.new()
        state.token, state.pid = fresh.token, fresh.pid

    def forget_own_key(self, state: ChatState) -> None:
        """Drop the own key. No passkey is needed: a locked session must be able to forget."""
        state.own_key.clear()

    def _require(self, state: ChatState) -> None:
        self.gate.require(state.token)

    # --- sessions and conversations (CHT-01) ---

    def session_options(self, state: ChatState) -> list[str]:
        self._require(state)
        return session_labels(self.corpus)

    def scope_label(self, state: ChatState) -> str:
        self._require(state)
        return ALL_SESSIONS if state.scope is None else _session_label(state.scope)

    def choose_scope(self, state: ChatState, label: str) -> None:
        """A different scope starts a new conversation (a conversation has one scope)."""
        self._require(state)
        try:
            scope = scope_for(label, self.corpus)
        except ValueError:
            raise GuardRefused(BAD_SCOPE) from None
        if scope != state.scope:
            state.scope = scope
            state.conversation_id = None
            state.turns.clear()

    def new_conversation(self, state: ChatState) -> None:
        """Close the learner's open conversations and start a new one: an empty window."""
        self._require(state)
        conv = self.assistant_for(_NoCalls()).new_conversation(state.pid, state.scope)
        state.conversation_id = conv.conversation_id
        state.turns.clear()

    # --- notice and consent (CHT-04) ---

    def notice(self, state: ChatState) -> str:
        self._require(state)
        return NOTICE

    def needs_notice(self, state: ChatState) -> bool:
        self._require(state)
        try:
            return needs_notice(state.pid, self.store())
        except PrivacyError:
            return True  # fail closed: show the notice

    def accept_notice(self, state: ChatState, improve_course: bool = False) -> Consent:
        self._require(state)
        purposes = (
            ("answer_questions", "improve_course") if improve_course else ("answer_questions",)
        )
        return self._privacy(lambda: give(state.pid, purposes, self.store(), at=self.now()))

    def withdraw(self, state: ChatState) -> int:
        self._require(state)
        return self._privacy(lambda: withdraw(state.pid, self.store(), at=self.now()))

    # --- asking (CHT-02, CHT-03, CHT-06) ---

    def ask(self, state: ChatState, question: str) -> Card:
        self._require(state)
        if not allowed(state.pid, "answer_questions", self.store()):
            raise GuardRefused(NOTICE_FIRST)
        q = question.strip() if isinstance(question, str) else ""
        if not q:
            raise GuardRefused(EMPTY_QUESTION)
        if len(q) > RouteStep.MAX_QUESTION_CHARS:
            raise GuardRefused(TOO_LONG)
        if self.daily_questions is not None:
            today = self.now().astimezone(dt.UTC).date()
            asked = sum(1 for x in self.store().questions_for(state.pid)
                        if x.asked_at.astimezone(dt.UTC).date() == today)  # fmt: skip
            if asked >= self.daily_questions:
                raise GuardRefused(
                    f"You have asked today's {self.daily_questions} questions. "
                    "You can ask more tomorrow (the count resets at midnight UTC)."
                )
        turn: Turn | None = None
        try:
            turn = self.demo.ask(state.token, q, state.pid, own_key=state.own_key.reveal(),
                                 conversation_id=state.conversation_id,
                                 scope=state.scope)  # fmt: skip
        except GuardRefused as exc:
            card = refused_card(exc.reason)
        except Exception as exc:  # never the message: it could hold text or a key
            LOG.warning("learner chat: ask failed (%s)", type(exc).__name__)
            card = _card("error", Assistant.ERROR_MESSAGE, None)
        else:
            # the session may have signed out (or the passkey changed) while the answer was on
            # its way: then the turn is dropped from this page
            self._require(state)
            card = card_for(turn, self.corpus)
            if turn.conversation_id is not None:  # a paused turn stored nothing
                state.conversation_id = turn.conversation_id
        state.turns.append(Shown(question=q, card=card, conversation_id=state.conversation_id,
                                 turn=turn))  # fmt: skip
        return card

    # --- worked examples: the demo cache, replayed with no model call (D3.2, DEM-01) ---

    def replay_options(self, state: ChatState) -> tuple[tuple[str, str], ...]:
        """(case id, question) for each stored example; empty when no cache is loaded."""
        self._require(state)
        cache = self.demo.cache
        if cache is None:
            return ()
        return tuple((cid, cache.question(cid)) for cid in cache.ids())

    def replay(self, state: ChatState, case_id: str) -> Card:
        """The stored answer of one example as a card (chips from the cache's own corpus),
        with no question id and no "Ask the teaching team".
        The live demo checks the passkey again; nothing is stored and no model is called."""
        self._require(state)
        answer = self.demo.replay(state.token, case_id)
        cache = self.demo.cache
        if cache is None:  # the live demo refused above already; kept for the type
            raise GuardRefused("The demo replay file is not available yet.")
        # an example is not this learner's question: nothing to send to the teaching team
        return replace(card_for(answer, cache.fixture.corpus), question_id=None,
                       can_escalate=False)  # fmt: skip

    def escalate(self, state: ChatState, question_id: str | None) -> Escalation:
        """ "Ask the teaching team" on a not-covered reply: the turn this page holds goes with
        its kept draft (B2.1); a second tap returns the first escalation."""
        self._require(state)
        shown = next((s for s in state.turns if s.card.question_id == question_id), None)
        if question_id is None or shown is None or not shown.card.can_escalate:
            raise GuardRefused(ESCALATION_FAILED)
        try:
            if shown.turn is not None:
                e = escalate_turn(shown.turn, store=self.store(), reason="not_covered",
                                  at=self.now())  # fmt: skip
            else:
                e = escalate(question_id, "not_covered", learner_pid=state.pid,
                             store=self.store(), at=self.now())  # fmt: skip
        except EscalationError:
            raise GuardRefused(ESCALATION_FAILED) from None
        shown.escalated = True
        return e

    # --- own key (CHT-06) ---

    def set_own_key(self, state: ChatState, key: str) -> None:
        self._require(state)
        k = key.strip() if isinstance(key, str) else ""
        if not k or len(k) > MAX_KEY_CHARS or any(ch.isspace() for ch in k):
            raise GuardRefused(BAD_KEY)
        state.own_key.set(k)

    # --- history and delete (CHT-04) ---

    def history_raw(self, state: ChatState) -> tuple[ConversationHistory, ...]:
        self._require(state)
        return self._privacy(lambda: history(state.pid, self.store()))

    def history(self, state: ChatState) -> tuple[PastConversation, ...]:
        """Only this learner's turns: what was shown, the handover and any team reply."""
        out = []
        for conv in self.history_raw(state):
            turns = []
            for t in conv.turns:
                status = "paused" if t.answer is None else t.answer.status
                icon, label = STATUS_LABELS.get(status, STATUS_LABELS["error"])
                # B2.2 keeps the text of answers and hints; a handover and an error showed one
                # fixed text each; a not-covered reply showed one of several, so none is guessed
                shown = (
                    None
                    if t.answer is None
                    else (t.answer.text or _FIXED_SHOWN.get(t.answer.status))
                )
                turns.append(PastTurn(
                    question_id=t.question.question_id, question=t.question.text, icon=icon,
                    status_label=label, text=shown,
                    handover=None if t.handover is None else HANDOVER_LABELS[t.handover],
                    reply=None if t.reply is None else t.reply.text,
                    reply_label=None if t.reply is None else TEAM_LABEL))  # fmt: skip
            c = conv.conversation
            scope = ALL_SESSIONS if c.session_scope is None else _session_label(c.session_scope)
            out.append(PastConversation(conversation_id=c.conversation_id,
                                        started_at=c.started_at, scope_label=scope,
                                        turns=tuple(turns)))  # fmt: skip
        return tuple(out)

    def replies_waiting(self, state: ChatState) -> int:
        self._require(state)
        try:
            return _reply_waiting(state.pid, self.store())
        except EscalationError:
            return 0

    def delete(self, state: ChatState, conversation_id: str | None = None) -> DeletionRequest:
        """B2.2's delete: app rows at once, trace lines in every sink. Deleting the open
        conversation (or everything) starts a fresh one."""
        self._require(state)
        req = self._privacy(lambda: delete(state.pid, conversation_id, store=self.store(),
                                           traces=self.traces))  # fmt: skip
        if conversation_id is None or conversation_id == state.conversation_id:
            state.conversation_id = None
        state.turns[:] = [s for s in state.turns if conversation_id is not None
                          and s.conversation_id != conversation_id]  # fmt: skip
        return req

    @staticmethod
    def _privacy(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except PrivacyError:
            raise GuardRefused(PRIVACY_FAILED) from None
