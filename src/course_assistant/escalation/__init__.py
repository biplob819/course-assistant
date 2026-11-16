"""The escalation queue: hand a question to the teaching team and bring the reply back (B2.1)."""

from course_assistant.escalation._errors import EscalationError
from course_assistant.escalation.queue import (
    MAX_REPLY_CHARS,
    REASONS,
    TEAM_LABEL,
    LearnerReply,
    QueueItem,
    ReplyTimes,
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

__all__ = [
    "MAX_REPLY_CHARS",
    "REASONS",
    "TEAM_LABEL",
    "EscalationError",
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
