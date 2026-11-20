"""Pilot learner ids from platform ids (D-13, PIL-03).

A learner's id is `HMAC-SHA256(platform_user_id, PID_SECRET)` cut to 16 hex characters: the
same person always gets the same id, so history, consent and deletion follow them, while the
platform id itself is never stored or sent anywhere. Without the secret the id cannot be
linked back to the person. The demo keeps its random per-session ids (`app/chat.py`).

Errors carry fixed text only: never the platform id and never the secret.
"""

from __future__ import annotations

import hashlib
import hmac

from course_assistant.privacy._errors import check_pid

MIN_SECRET = 16  # characters; `PID_SECRET` is a long random value (Tech Spec §8)


class PseudonymError(ValueError):
    """The platform id or the secret cannot make a learner id (fixed text, no values)."""


def learner_pid(platform_user_id: str, secret: str) -> str:
    """The learner's pseudonymous id for a platform user id."""
    if not isinstance(secret, str) or len(secret.strip()) < MIN_SECRET:
        raise PseudonymError("PID_SECRET is missing or too short")
    if not isinstance(platform_user_id, str) or not platform_user_id.strip():
        raise PseudonymError("the sign-in gave no platform user id")
    digest = hmac.new(secret.encode("utf-8"), platform_user_id.strip().encode("utf-8"),
                      hashlib.sha256).hexdigest()  # fmt: skip
    return check_pid(digest[:16])
