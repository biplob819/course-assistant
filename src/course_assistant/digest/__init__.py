"""The weekly digest: topic labels by session with counts, k = 5, consenting learners only
(B2.4, US6, FR9, findings H7 and H8)."""

from course_assistant.digest._errors import DigestError
from course_assistant.digest.weekly import (
    K_MIN,
    MAX_BATCH,
    MAX_BATCH_CHARS,
    MAX_LABEL_CHARS,
    MAX_LABEL_WORDS,
    RUN_WORDS,
    TopicStep,
    label_problem,
    week_window,
    weekly_digest,
)

__all__ = [
    "K_MIN",
    "MAX_BATCH",
    "MAX_BATCH_CHARS",
    "MAX_LABEL_CHARS",
    "MAX_LABEL_WORDS",
    "RUN_WORDS",
    "DigestError",
    "TopicStep",
    "label_problem",
    "week_window",
    "weekly_digest",
]
