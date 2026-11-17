"""One public error for the escalation queue, with fixed text (L-28, L-30).

Every public function does its work inside `boundary`, which turns any failure into an
`EscalationError` raised outside the except block, so no question, draft or reply text, id or
library message (a pydantic error echoes its input) can reach a caller through the message,
`__cause__` or `__context__`.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Literal, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")
Code = Literal["invalid", "not_found", "refused", "changed", "failed"]

_MESSAGES: dict[Code, str] = {
    "invalid": "the escalation request was refused: an id or value is not valid",
    "not_found": "no such question or escalation for this request",
    "refused": "this escalation cannot change that way",
    "changed": "this escalation changed since it was shown",
    "failed": "the escalation request could not be completed",
}


class EscalationError(Exception):
    def __init__(self, code: Code) -> None:
        super().__init__(_MESSAGES[code])
        self.code: Code = code


class Invalid(Exception):
    """Internal: an input was refused before any work was done."""


class NotFound(Exception):
    """Internal: the question or escalation does not exist for this caller."""


class Refused(Exception):
    """Internal: the reason does not fit the stored answer, or the transition is not allowed."""


class Changed(Exception):
    """Internal: the escalation's status is not the one the caller was shown (B2.3)."""


def boundary(fn: Callable[P, R]) -> Callable[P, R]:
    """Single exit: any exception inside becomes an EscalationError raised outside the handler.
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
        except NotFound:
            code = "not_found"
        except Refused:
            code = "refused"
        except Changed:
            code = "changed"
        except Exception:
            code = "failed"
        raise EscalationError(code)

    return wrapper
