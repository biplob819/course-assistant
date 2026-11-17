"""One public error for the weekly digest, with fixed text (L-28, L-30).

`weekly_digest` does its work inside `boundary`, which turns any failure into a `DigestError`
raised outside the except block, so no question text, learner id or library message can reach
a caller through the message, `__cause__` or `__context__`.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Literal, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")
Code = Literal["invalid", "failed"]

_MESSAGES: dict[Code, str] = {
    "invalid": "the digest request was refused: a week, k or session value is not valid",
    "failed": "the digest could not be built; nothing was written",
}


class DigestError(Exception):
    def __init__(self, code: Code) -> None:
        super().__init__(_MESSAGES[code])
        self.code: Code = code


class Invalid(Exception):
    """Internal: an input was refused before any work was done."""


class Failed(Exception):
    """Internal: the labels could not be read, so no digest is written."""


def boundary(fn: Callable[P, R]) -> Callable[P, R]:
    """Single exit: any exception inside becomes a DigestError raised outside the handler.
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
        raise DigestError(code)

    return wrapper
