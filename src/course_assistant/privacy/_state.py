"""The learner's consent state, read from the store (shared by notice and consent)."""

from __future__ import annotations

from course_assistant.schemas import Consent
from course_assistant.store import Store


def current_consent(pid: str, store: Store) -> Consent | None:
    """The learner's latest open consent (by time given, then by order recorded), or None.
    A later consent replaces an earlier one, so narrowing the purposes takes effect."""
    open_ = [c for c in store.consents_for(pid) if c.withdrawn_at is None]
    return open_[-1] if open_ else None
