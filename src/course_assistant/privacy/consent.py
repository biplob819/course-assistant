"""Consent per purpose (PRV-02). Withdrawal stops processing at once and is separate from
deletion; giving and withdrawing are each audited in the same transaction as the record."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from pydantic import ValidationError

from course_assistant.privacy._errors import Invalid, boundary, check_pid, now_or
from course_assistant.privacy._state import current_consent
from course_assistant.privacy.notice import NOTICE_VERSION
from course_assistant.schemas import Consent
from course_assistant.schemas.people import Purpose
from course_assistant.store import Store

PURPOSES: tuple[Purpose, ...] = ("answer_questions", "improve_course")


@boundary
def give(pid: str, purposes: Sequence[Purpose], store: Store, *, at: dt.datetime | None = None,
         version: str | None = None) -> Consent:  # fmt: skip
    """Record consent to `purposes` for a notice version (the current one by default)."""
    check_pid(pid)
    try:
        c = Consent(learner_pid=pid, notice_version=version or NOTICE_VERSION,
                    purposes=tuple(purposes), given_at=now_or(at))  # fmt: skip
    except ValidationError:
        raise Invalid from None
    store.give_consent(c)
    return c


@boundary
def withdraw(pid: str, store: Store, *, at: dt.datetime | None = None) -> int:
    """Close every open consent of the learner; returns how many were open. Audited even
    when nothing was open, since the learner asked."""
    check_pid(pid)
    return store.withdraw_consent(pid, at=now_or(at))


def allowed(pid: str, purpose: Purpose, store: Store) -> bool:
    """May this learner's data be processed for `purpose` now? Only with an open consent to
    the current notice version that names the purpose. Anything else, including a bad id, an
    unknown purpose or a read failure, is False (fails closed)."""
    try:
        check_pid(pid)
        if purpose not in PURPOSES:
            return False
        c = current_consent(pid, store)
    except Exception:
        return False
    return c is not None and c.notice_version == NOTICE_VERSION and purpose in c.purposes
