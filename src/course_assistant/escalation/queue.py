"""The escalation queue (B2.1, US5, FR8, Flow B).

A question goes to the teaching team with its reason, the assistant's draft and the sources it
used; an instructor replies, flags a material gap or closes it; the reply waits in the
learner's history (B2.2), marked as from the teaching team, with no email (finding H5).

Teaching-team actions are audited with a role id, an action and the question id only (NFR7).
Nothing here writes a trace (D-14). Escalations are learner text in the app store, so B2.2's
delete and N1.5's purge remove them with the rest of the history (§7).
"""

from __future__ import annotations

import datetime as dt
import statistics
import unicodedata
from typing import Literal, get_args

from pydantic import TypeAdapter, ValidationError

from course_assistant.answer.ask import Assistant, Turn
from course_assistant.answer.leak import scan
from course_assistant.answer.measure import percentile
from course_assistant.escalation._errors import Invalid, NotFound, Refused, boundary
from course_assistant.privacy import history
from course_assistant.schemas import Answer, Escalation, Question
from course_assistant.schemas._base import (
    ConversationId,
    Model,
    NonBlank,
    NonNegInt,
    Pid,
    QuestionId,
    RoleId,
    SessionId,
    Timestamp,
)
from course_assistant.schemas.people import Draft, Reason
from course_assistant.store import Store

REASONS: tuple[str, ...] = get_args(Reason)
EscalationStatus = Literal["open", "replied", "material_gap", "closed"]
STATUSES: tuple[str, ...] = get_args(EscalationStatus)
MAX_REPLY_CHARS = 4_000
TEAM_LABEL = "From the teaching team"

# which stored answers each reason fits (spec table): off-topic and injection replies carry no
# handover offer (§2.2), so only the NOT_COVERED text counts as a not-covered reply
_FITS: dict[str, frozenset[str]] = {
    "not_covered": frozenset({"not_covered"}),
    "graded_work": frozenset({"hint_only"}),
    "admin": frozenset({"handed_over"}),
    "personal": frozenset({"handed_over"}),
    "learner_request": frozenset({"answered", "hint_only", "error", "not_covered", "handed_over"}),
}
# a teaching-team change may start only from these statuses
_FROM: dict[str, frozenset[str]] = {
    "reply": frozenset({"open", "material_gap"}),
    "flag_gap": frozenset({"open", "replied"}),
    "close": frozenset({"open", "replied", "material_gap"}),
}

_PID = TypeAdapter(Pid)


def _visible(text: str) -> bool:
    """Holds something a reader can see: not only spaces or invisible format characters such
    as zero-width spaces (verifier B2.1 pass 2)."""
    return any(not ch.isspace() and unicodedata.category(ch) != "Cf" for ch in text)


_QID = TypeAdapter(QuestionId)
_ROLE = TypeAdapter(RoleId)


class LearnerReply(Model):
    """A teaching-team reply as the learner sees it: labelled, with the role id, never a name."""

    question_id: QuestionId
    conversation_id: ConversationId
    text: NonBlank
    label: Literal["From the teaching team"] = TEAM_LABEL
    replied_by: RoleId
    replied_at: Timestamp


class QueueItem(Model):
    """One row of the teaching team's queue (TTV-01 shows it): question, reason, draft and
    sources, status, reply and times."""

    question_id: QuestionId
    question: str
    session: SessionId | None
    reason: Reason
    status: EscalationStatus
    draft: Draft | None
    reply_text: str | None
    replied_by: RoleId | None
    created_at: Timestamp
    replied_at: Timestamp | None


class ReplyTimes(Model):
    """The pilot reply-time metric: numbers only."""

    replied: NonNegInt
    median_seconds: float | None
    p90_seconds: float | None


# ---------- checks ----------


def _check(adapter: TypeAdapter[str], value: object) -> str:
    try:
        return adapter.validate_python(value)
    except ValidationError:
        raise Invalid from None


def _when(at: dt.datetime | None) -> dt.datetime:
    if at is None:
        return dt.datetime.now(dt.UTC)
    if not isinstance(at, dt.datetime) or at.tzinfo is None:
        raise Invalid
    return at.astimezone(dt.UTC)


def _owned(question_id: str, pid: str, store: Store) -> tuple[Question, Answer]:
    """The learner's own question and its stored answer. Another learner's question reads as
    missing, so its existence is not revealed."""
    q = store.get_question(question_id)
    if q is None or q.learner_pid != pid or q.question_id != question_id:
        raise NotFound
    a = store.get_answer(question_id)
    if a is None:
        raise Refused  # no answer yet: nothing to escalate from
    return q, a


def _fits(reason: str, answer: Answer) -> bool:
    if answer.status not in _FITS[reason]:
        return False
    # a not-covered reply counts only with the NOT_COVERED text (not off-topic or injection)
    return answer.status != "not_covered" or answer.text == Assistant.NOT_COVERED


def _shown_draft(answer: Answer) -> Draft | None:
    """For a shown answer or hint: what the learner saw, with its sources."""
    if answer.status in ("answered", "hint_only"):
        return Draft(text=answer.text, citations=answer.citations, hits=answer.hits)
    return None


def _queue(q: Question, reason: str, draft: Draft | None, at: dt.datetime,
           store: Store) -> Escalation:  # fmt: skip
    e = Escalation(question_id=q.question_id, reason=reason, draft=draft,  # type: ignore[arg-type]
                   status="open", created_at=max(at, q.asked_at))  # fmt: skip
    return store.add_escalation(e)


# ---------- learner side (ESC-01) ----------


def _default_reason(turn: Turn) -> str:
    """The reason a turn's escalation gets when the caller gives none."""
    a = turn.answer
    if a.status == "handed_over":
        if turn.route not in ("admin", "personal"):
            raise Refused  # a handover always comes from the admin or personal route
        return turn.route
    if a.status == "not_covered" and a.text == Assistant.NOT_COVERED:
        return "not_covered"
    if a.status == "hint_only" and turn.route == "graded_work":
        return "graded_work"
    return "learner_request"


@boundary
def escalate_turn(turn: Turn, *, store: Store, reason: str | None = None,
                  at: dt.datetime | None = None) -> Escalation:  # fmt: skip
    """Queue the question of a turn `ask()` just returned, with the kept draft bound to it."""
    return _escalate_turn(turn, store, reason, at)


def _escalate_turn(turn: Turn, store: Store, reason: str | None,
                   at: dt.datetime | None) -> Escalation:  # fmt: skip
    if not isinstance(turn, Turn) or turn.paused or turn.conversation_id is None:
        raise Invalid  # a paused turn stored nothing
    when = _when(at)
    chosen = _default_reason(turn) if reason is None else reason
    if chosen not in REASONS:
        raise Invalid
    pid = _stored_pid(turn, store)
    q, a = _owned(turn.answer.question_id, pid, store)
    if a != turn.answer or not _fits(chosen, a):
        raise Refused  # the turn is not the stored one, or the reason does not fit
    if chosen in ("admin", "personal") and turn.route != chosen:
        raise Refused
    draft = _shown_draft(a)
    if (a.status == "not_covered" and turn.draft is not None and not turn.leak
            and not scan(turn.draft.text)):  # fmt: skip
        # never-shown drafts are scanned here too: ask() scans only text it shows (pass 1)
        # the kept course draft with its sources; a leaked draft is never attached (it may
        # hold the canary, a pid or rules text)
        d = turn.draft
        draft = Draft(text=d.text, citations=d.citations, hits=d.hits)
    if draft is not None and scan(draft.text):
        draft = None  # a draft that leaks is never attached, whatever its source
    return _queue(q, chosen, draft, when, store)


def _stored_pid(turn: Turn, store: Store) -> str:
    q = store.get_question(turn.answer.question_id)
    if q is None or q.conversation_id != turn.conversation_id:
        raise NotFound  # deleted, or not this turn's question
    return q.learner_pid


@boundary
def queue_handover(turn: Turn, *, store: Store, at: dt.datetime | None = None) -> Escalation | None:
    """Queue a handed-over turn at once (§2.2: "Sent to the teaching team" plus the queue
    entry); None for any other turn. The chat page calls it after every turn."""
    if not isinstance(turn, Turn):
        raise Invalid
    if turn.paused or not turn.handover or turn.answer.status != "handed_over":
        return None
    return _escalate_turn(turn, store, None, at)


@boundary
def escalate(question_id: str, reason: str, *, learner_pid: str, store: Store,
             at: dt.datetime | None = None) -> Escalation:  # fmt: skip
    """The learner's later "Ask the teaching team" on a stored turn, with no kept draft in
    hand: the draft is the shown answer or hint where one was shown."""
    qid = _check(_QID, question_id)
    pid = _check(_PID, learner_pid)
    if reason not in REASONS:
        raise Invalid
    when = _when(at)
    q, a = _owned(qid, pid, store)
    # admin and personal need the route, which only the turn carries: a stored handover
    # missed by `queue_handover` can still be raised here as a learner request
    if reason in ("admin", "personal") or not _fits(reason, a):
        raise Refused
    return _queue(q, reason, _shown_draft(a), when, store)


# ---------- teaching-team side (ESC-02, ESC-04) ----------


def _change(question_id: str, actor: str, action: str, at: dt.datetime | None,
            update: dict[str, object], store: Store) -> Escalation:  # fmt: skip
    qid = _check(_QID, question_id)
    role = _check(_ROLE, actor)
    when = _when(at)
    if store.get_escalation(qid) is None:
        raise NotFound

    def apply(current: Escalation) -> Escalation:
        if current.status not in _FROM[action]:
            raise Refused
        if action == "reply" and current.reply_text is not None:
            raise Refused  # one reply per question, never overwritten (verifier B2.1 pass 1)
        fields = dict(update)
        if action == "reply":
            fields["replied_at"] = max(when, current.created_at)
            if current.status == "material_gap":
                fields["status"] = "material_gap"  # a flagged gap keeps its flag
        try:
            return Escalation.model_validate({**current.model_dump(), **fields})
        except ValidationError:
            raise Invalid from None

    try:
        return store.update_escalation(qid, apply, role, action, at=when)
    except KeyError:
        raise NotFound from None  # deleted between the check and the change


@boundary
def reply(question_id: str, text: str, *, instructor: str, store: Store,
          at: dt.datetime | None = None) -> Escalation:  # fmt: skip
    """Send the teaching team's reply. It waits in the learner's history, labelled as from the
    teaching team; audited `reply` in the same transaction."""
    if not isinstance(text, str) or len(text) > MAX_REPLY_CHARS or not _visible(text):
        raise Invalid
    return _change(question_id, instructor, "reply", at,
                   {"status": "replied", "reply_text": text, "replied_by": instructor},
                   store)  # fmt: skip


@boundary
def flag_gap(question_id: str, *, instructor: str, store: Store,
             at: dt.datetime | None = None) -> Escalation:  # fmt: skip
    """Mark the question as showing a gap in the materials (for the digest); a reply stays."""
    return _change(question_id, instructor, "flag_gap", at, {"status": "material_gap"}, store)


@boundary
def close(question_id: str, *, instructor: str, store: Store,
          at: dt.datetime | None = None) -> Escalation:  # fmt: skip
    """End the escalation; a reply stays and still shows in the learner's history."""
    return _change(question_id, instructor, "close", at, {"status": "closed"}, store)


@boundary
def team_queue(store: Store, *, viewer: str, status: str | None = None,
               at: dt.datetime | None = None) -> tuple[QueueItem, ...]:  # fmt: skip
    """The teaching team's queue, oldest first, audited `view_queue` (no target, no text)."""
    role = _check(_ROLE, viewer)
    if status is not None and status not in STATUSES:
        raise Invalid
    when = _when(at)
    items = []
    for e in store.escalations(status):
        q = store.get_question(e.question_id)
        if q is None:
            continue  # its learner deleted it mid-read
        items.append(QueueItem(question_id=e.question_id, question=q.text,
                               session=q.session_scope, reason=e.reason, status=e.status,
                               draft=e.draft, reply_text=e.reply_text, replied_by=e.replied_by,
                               created_at=e.created_at, replied_at=e.replied_at))  # fmt: skip
    store.audit(role, "view_queue", None, at=when)
    return tuple(items)


@boundary
def reply_times(store: Store) -> ReplyTimes:
    """Seconds from `created_at` to `replied_at` over every replied escalation."""
    secs = [(e.replied_at - e.created_at).total_seconds()
            for e in store.escalations() if e.replied_at is not None]  # fmt: skip
    if not secs:
        return ReplyTimes(replied=0, median_seconds=None, p90_seconds=None)
    return ReplyTimes(replied=len(secs), median_seconds=float(statistics.median(secs)),
                      p90_seconds=percentile(secs, 90))  # fmt: skip


# ---------- the learner's history (ESC-03) ----------


@boundary
def learner_replies(pid: str, store: Store) -> tuple[LearnerReply, ...]:
    """The learner's teaching-team replies, newest first, read through B2.2's history so only
    the learner's own rows are ever returned."""
    return _learner_replies(pid, store)


def _learner_replies(pid: str, store: Store) -> tuple[LearnerReply, ...]:
    _check(_PID, pid)
    out = []
    for conv in history(pid, store):
        for t in conv.turns:
            if t.reply is not None:
                out.append(LearnerReply(question_id=t.question.question_id,
                                        conversation_id=conv.conversation.conversation_id,
                                        text=t.reply.text, replied_by=t.reply.replied_by,
                                        replied_at=t.reply.replied_at))  # fmt: skip
    out.sort(key=lambda r: (r.replied_at, r.question_id), reverse=True)
    return tuple(out)


@boundary
def reply_waiting(pid: str, store: Store) -> int:
    """How many of the learner's turns hold a teaching-team reply (the history badge)."""
    return len(_learner_replies(pid, store))


__all__ = [
    "MAX_REPLY_CHARS",
    "REASONS",
    "TEAM_LABEL",
    "LearnerReply",
    "QueueItem",
    "ReplyTimes",
    "close",
    "escalate",
    "escalate_turn",
    "flag_gap",
    "learner_replies",
    "queue_handover",
    "reply",
    "reply_times",
    "reply_waiting",
    "team_queue",
]
