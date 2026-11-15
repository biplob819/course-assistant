"""One public error for the privacy actions, with fixed text (L-28, L-30).

Every public function does its work in an inner function and converts any failure to a
`PrivacyError` outside the except block, so no learner text, id or library message can
reach a caller through the message, `__cause__` or `__context__`.
"""

from __future__ import annotations

import datetime as dt
import functools
import inspect
from collections.abc import Callable
from typing import Literal, ParamSpec, TypeVar

from pydantic import TypeAdapter, ValidationError

from course_assistant.schemas._base import ConversationId, Pid

P = ParamSpec("P")
R = TypeVar("R")
Code = Literal["invalid", "failed"]

_MESSAGES: dict[Code, str] = {
    "invalid": "the privacy request was refused: an id or value is not valid",
    "failed": "the privacy request could not be completed",
}
_PID = TypeAdapter(Pid)
_CID = TypeAdapter(ConversationId)


class PrivacyError(Exception):
    def __init__(self, code: Code) -> None:
        super().__init__(_MESSAGES[code])
        self.code: Code = code


class Invalid(Exception):
    """Internal: the input was refused before any work was done."""


def check_pid(pid: object) -> str:
    try:
        return _PID.validate_python(pid)
    except ValidationError:
        raise Invalid from None


def check_cid(cid: object) -> str:
    try:
        return _CID.validate_python(cid)
    except ValidationError:
        raise Invalid from None


def now_or(at: dt.datetime | None) -> dt.datetime:
    if at is None:
        return dt.datetime.now(dt.UTC)
    if at.tzinfo is None:
        raise Invalid
    return at


def boundary(fn: Callable[P, R]) -> Callable[P, R]:
    """Single exit: any exception inside becomes a PrivacyError raised outside the handler.
    A call that does not match the signature is a programming error and stays a TypeError."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        signature.bind(*args, **kwargs)
        code: Code
        try:
            return fn(*args, **kwargs)
        except Invalid:
            code = "invalid"
        except Exception:
            code = "failed"
        raise PrivacyError(code)

    return wrapper
