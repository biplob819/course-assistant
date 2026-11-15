"""Shared base model and strict field types for every course-assistant schema.

Models are deeply immutable: list fields are tuples, dict fields become `FrozenDict`, and
free-form JSON is frozen recursively, so an object's JSON never changes after creation
(pattern from EvalKit `schemas/_base.py`, D-26 there; L-4 here).
"""

from __future__ import annotations

import copy
import datetime as dt
import math
from collections.abc import Iterator, Mapping
from typing import Annotated, Any, NoReturn, Self, TypeVar

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

SCHEMA_VERSION = "1.0"
VERSION_PATTERN = r"^\d+\.\d+$"


class FrozenDict(dict):
    """A dict that refuses mutation and can be hashed."""

    def _blocked(self, *_: Any, **__: Any) -> NoReturn:
        raise TypeError("course-assistant schema objects are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _blocked  # type: ignore[assignment]
    __ior__ = _blocked  # type: ignore[assignment]

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(frozenset(self.items()))

    def __reduce__(self) -> tuple[type, tuple[dict]]:  # pickle and copy without __setitem__
        return (FrozenDict, (dict(self),))

    def __deepcopy__(self, memo: dict) -> FrozenDict:
        return FrozenDict({k: copy.deepcopy(v, memo) for k, v in self.items()})

    def __iter__(self) -> Iterator[Any]:
        return super().__iter__()


def deep_freeze(value: Any) -> Any:
    """Recursively turn lists into tuples and dicts into FrozenDicts; reject sets and NaN."""
    if isinstance(value, Mapping):
        return FrozenDict({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(deep_freeze(v) for v in value)
    if isinstance(value, set | frozenset):
        raise ValueError("sets are not JSON values; use a list")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("NaN and infinity are not allowed")
    return value


def _freeze_mapping(value: Mapping[Any, Any]) -> FrozenDict:
    # keys sorted, so two equal maps always serialise to the same JSON (content hashes)
    return FrozenDict({k: deep_freeze(value[k]) for k in sorted(value)})


def _utc(value: dt.datetime) -> dt.datetime:
    return value.astimezone(dt.UTC)  # one canonical form for equal instants


NonNegInt = Annotated[int, Field(ge=0, strict=True)]
PosInt = Annotated[int, Field(ge=1, strict=True)]
NonNegFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
Score = Annotated[float, Field(allow_inf_nan=False)]
NonBlank = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Name = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S(.*\S)?$")]
Ident = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._:-]{0,127}$")]
SessionId = Annotated[str, StringConstraints(pattern=r"^s\d\d$")]
Pid = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]  # D-13, never a name or email
# staff are referred to by role and number, never by name (D-28): "instructor-1", "ta-2"
ROLE_PATTERN = r"^(instructor|ta|owner|system)-[0-9]{1,4}$"
RoleId = Annotated[str, StringConstraints(pattern=ROLE_PATTERN)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ShortHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12,64}$")]
Timestamp = Annotated[AwareDatetime, AfterValidator(_utc)]
# generated ids: "q-" or "c-" + 12-32 hex digits, so a name or email cannot be an id
QuestionId = Annotated[str, StringConstraints(pattern=r"^q-[0-9a-f]{12,32}$")]
ConversationId = Annotated[str, StringConstraints(pattern=r"^c-[0-9a-f]{12,32}$")]
Version = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,31}$")]

V = TypeVar("V")
FrozenMap = Annotated[dict[str, V], AfterValidator(_freeze_mapping)]


class Model(BaseModel):
    """Frozen, strict base: unknown fields are errors, objects are immutable, floats finite."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False, strict=True)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Copies are validated like new objects, so an update cannot bypass an invariant."""
        if not update:
            return super().model_copy(deep=deep)
        return type(self).model_validate({**dict(self), **update})


class Versioned(Model):
    """A top-level persisted record. `schema_version` is required and must be major 1."""

    schema_version: Annotated[str, StringConstraints(pattern=VERSION_PATTERN)]

    @field_validator("schema_version")
    @classmethod
    def _same_major(cls, v: str) -> str:
        if v.split(".", 1)[0] != SCHEMA_VERSION.split(".", 1)[0]:
            raise ValueError(f"schema {v} is not compatible (expects {SCHEMA_VERSION[0]}.x)")
        return v
