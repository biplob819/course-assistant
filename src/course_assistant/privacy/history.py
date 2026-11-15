"""A learner's own history (PRV-03): conversations in order, each with its questions, the
answers shown and any teaching-team reply. The teaching team's draft is never included."""

from __future__ import annotations

from typing import Literal

from course_assistant.privacy._errors import boundary, check_pid
from course_assistant.schemas import Answer, Citation, Conversation, Question
from course_assistant.schemas._base import Model, NonBlank, RoleId, Timestamp
from course_assistant.store import Store

Handover = Literal["open", "replied", "material_gap", "closed"]


class Reply(Model):
    text: NonBlank
    replied_by: RoleId
    replied_at: Timestamp


class ShownAnswer(Model):
    """Only what the learner saw (verifier B2.2 pass 1): the status, the text when it was
    shown (an answer or a hint; a not-covered or handed-over reply showed fixed text, not the
    kept draft) and its citations. Hits, grounding, cost and model details stay out."""

    question_id: str
    status: str
    text: str | None
    citations: tuple[Citation, ...]

    @classmethod
    def of(cls, a: Answer) -> ShownAnswer:
        shown = a.status in ("answered", "hint_only")
        return cls(question_id=a.question_id, status=a.status, text=a.text if shown else None,
                   citations=a.citations if shown else ())  # fmt: skip


class Turn(Model):
    question: Question
    answer: ShownAnswer | None = None
    handover: Handover | None = None  # set when the question went to the teaching team
    reply: Reply | None = None


class ConversationHistory(Model):
    conversation: Conversation
    turns: tuple[Turn, ...]


def _turn(q: Question, store: Store) -> Turn:
    a = store.get_answer(q.question_id)
    e = store.get_escalation(q.question_id)
    if (a is not None and a.question_id != q.question_id) or (
        e is not None and e.question_id != q.question_id
    ):
        raise ValueError("stored record does not match its question")
    reply = None
    if e is not None and e.reply_text and e.replied_by and e.replied_at:
        reply = Reply(text=e.reply_text, replied_by=e.replied_by, replied_at=e.replied_at)
    return Turn(
        question=q,
        answer=None if a is None else ShownAnswer.of(a),
        handover=None if e is None else e.status,
        reply=reply,
    )


@boundary
def history(pid: str, store: Store) -> tuple[ConversationHistory, ...]:
    """Only this learner's records: conversations by start time, turns by turn index."""
    check_pid(pid)
    out = []
    for c in store.conversations_for(pid):
        qs = store.questions_for(pid, c.conversation_id)
        if any(q.learner_pid != pid or q.conversation_id != c.conversation_id for q in qs):
            raise ValueError("stored question is outside this learner's conversation")
        qs.sort(key=lambda q: (q.turn_index, q.asked_at, q.question_id))
        out.append(ConversationHistory(conversation=c, turns=tuple(_turn(q, store) for q in qs)))
    return tuple(out)
