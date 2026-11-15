"""First-use notice, versioned (PRV-01). A learner sees it until they consent to the current
version, so publishing a new version asks everyone again."""

from __future__ import annotations

from course_assistant.privacy._errors import boundary, check_pid
from course_assistant.privacy._state import current_consent
from course_assistant.store import Store

NOTICE_VERSION = "notice-1"

NOTICE = f"""\
Course assistant: how your questions are handled (notice version {NOTICE_VERSION})

What we store. Your questions, the assistant's answers, any reply from the teaching team, and
the conversation they belong to. You are known by a random-looking id made from your platform
account, never your name or email. Traces and logs hold that id, timings and costs, never what
you typed.

Why. Purpose answer_questions: to answer your questions from the course materials and pass
the ones it cannot answer to the teaching team. Purpose improve_course (optional): to count,
in groups of five or more learners, which topics people ask about, so the team can improve the
course. You choose each purpose separately.

How long. Your conversations are kept for the length of the pilot plus 30 days, or until you
delete them. The record of your consent is kept for 1 year after the pilot, as proof of what
you agreed to. The access log, which holds no question text, is kept for 1 year.

Where it goes. Each question is sent to OpenAI to write the answer, with storage off
(store=false). OpenAI may keep request data for up to 30 days for abuse monitoring unless zero
data retention applies. The teaching team sees questions passed to them. Nothing is sold or
used for advertising.

How to delete it. You can withdraw consent at any time; processing stops at once. Withdrawing
is separate from deleting: you can delete your whole history, or one conversation, at any
time. Your stored conversations are removed at once, and their trace lines within 7 days.
"""


def needs_notice(pid: str, store: Store, *, version: str = NOTICE_VERSION) -> bool:
    """True unless the learner has an open consent for `version`. Any read failure shows the
    notice (fails closed); an id that is not a pseudonym is refused with a PrivacyError."""
    _valid_pid(pid)
    try:
        c = current_consent(pid, store)
    except Exception:
        return True
    return c is None or c.notice_version != version


_valid_pid = boundary(check_pid)
