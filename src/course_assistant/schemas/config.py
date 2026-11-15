"""App configuration, golden cases and YAML loaders (SCH-04)."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, StringConstraints, ValidationError, model_validator

from course_assistant.schemas._base import (
    FrozenMap,
    Ident,
    Model,
    Name,
    NonBlank,
    NonNegFloat,
    PosInt,
    Score,
    SessionId,
    Versioned,
)
from course_assistant.schemas.corpus import AssessmentList, Manifest

Effort = Literal["minimal", "low", "medium", "high"]
# a unit of the course by place, not by index: "s02:slides:9", "s02:pre_read:3", "s01:agenda:1"
Locator = Annotated[
    str,
    StringConstraints(pattern=r"^s\d\d:(slides|pre_read|agenda|post_session|faq):[1-9]\d{0,3}$"),
]
Rule = Literal["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9", "B10"]
Expected = Literal["answered", "not_covered", "hint_only", "handed_over"]

# Tech Spec §6.1: the statuses each rule's cases may expect.
ALLOWED_STATUS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "B1": ("answered",),
        "B2": ("not_covered",),
        "B3": ("hint_only",),
        "B4": ("handed_over",),
        "B5": ("answered",),
        "B6": ("answered",),
        "B7": ("not_covered",),
        "B8": ("not_covered",),
        "B9": ("not_covered", "answered"),
        "B10": ("answered", "not_covered", "hint_only", "handed_over"),
    }
)


class AppConfig(Versioned):
    cohort: Ident
    vector_store_id: Name
    answer_model: Name
    check_model: Name
    reasoning_effort: FrozenMap[Effort]  # deep-frozen (L-4)
    max_num_results: PosInt = 8
    grounding_bar: Score
    digest_k: PosInt = 5
    window_turns: PosInt = 6
    window_tokens: PosInt = 3000
    trace_sink: Literal["jsonl", "langfuse"] = "jsonl"
    monthly_budget_usd: NonNegFloat

    @model_validator(mode="after")
    def _effort_is_explicit(self) -> AppConfig:
        missing = [
            m for m in (self.answer_model, self.check_model) if m not in self.reasoning_effort
        ]
        if missing:
            raise ValueError(f"reasoning_effort must name every model explicitly: {missing}")
        return self


class GoldenCase(Versioned):
    id: Ident
    rule: Rule
    prior_turns: tuple[tuple[NonBlank, NonBlank], ...] = ()
    question: NonBlank
    session_scope: SessionId | None = None
    expected_status: Expected
    gold_units: tuple[Locator, ...] = ()  # "s02:slides:9" (D-30)
    assessment_id: Ident | None = None
    canary: Name | None = None
    split: Literal["tuning", "heldout"]
    corpus_version: Name

    @model_validator(mode="after")
    def _rule_invariants(self) -> GoldenCase:
        if self.expected_status not in ALLOWED_STATUS[self.rule]:
            raise ValueError(f"{self.rule} cases cannot expect {self.expected_status}")
        if self.rule == "B1" and not self.gold_units:
            raise ValueError("B1 cases need gold_units")
        if self.rule == "B3" and self.assessment_id is None:
            raise ValueError("B3 cases need an assessment_id")
        if self.rule in ("B8", "B10") and self.canary is None:
            raise ValueError(f"{self.rule} cases need a canary")
        if len(set(self.gold_units)) != len(self.gold_units):
            raise ValueError("gold_units must be unique")
        return self


class ConfigError(Exception):
    """A config file could not be loaded. The message names the file and the field."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe loader with YAML 1.2 core scalars: only true/false are booleans and only plain
    decimal, 0o and 0x numbers are ints, so `on`, `yes` and `1:30` stay text (verifier N1.1)."""


_UniqueKeyLoader.yaml_implicit_resolvers = {
    first: [
        (tag, rx)
        for tag, rx in rs
        if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:int")
    ]
    for first, rs in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_UniqueKeyLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)
_UniqueKeyLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
    list("-+0123456789"),
)


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ConfigError(f"duplicate key at line {key_node.start_mark.line + 1}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)

M = TypeVar("M", bound=BaseModel)


def load_yaml_model(path: Path, model: type[M]) -> M:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except IsADirectoryError:
        raise ConfigError(f"{path}: is a directory, not a file") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read ({exc.strerror})") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not UTF-8 text") from None
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    except yaml.YAMLError as exc:
        # position only: the parser's message quotes the offending line, which may hold
        # personal data, so it is never repeated
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(f"{path}: invalid YAML{where}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    try:
        # validated in JSON mode, so YAML lists become tuples and dates parse from text
        # under the strict models; YAML's own dates and times are written back as ISO text
        as_json = json.dumps(data, default=_iso)
        return model.model_validate_json(as_json)
    except (TypeError, ValueError) as exc:
        if not isinstance(exc, ValidationError):
            raise ConfigError(f"{path}: holds a value that is not plain data") from None
        first = exc.errors()[0]
        known = _field_names()
        # name only the schema's own field names; any other key is user content (D-29)
        field = (
            ".".join(str(p) if isinstance(p, int) or p in known else "<key>" for p in first["loc"])
            or "(top level)"
        )
        raise ConfigError(f"{path}: field {field}: {first['msg']}") from None


def _field_names() -> frozenset[str]:
    """Every field name of every schema model (computed when an error is reported)."""
    names: set[str] = set()
    todo: list[type] = [Model]
    while todo:
        cls = todo.pop()
        names |= set(getattr(cls, "model_fields", {}))
        todo.extend(cls.__subclasses__())
    return frozenset(names)


def _iso(value: object) -> str:
    if isinstance(value, dt.date | dt.datetime | dt.time):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def load_app_config(path: Path) -> AppConfig:
    return load_yaml_model(path, AppConfig)


def load_manifest(path: Path) -> Manifest:
    return load_yaml_model(path, Manifest)


def load_assessments(path: Path) -> AssessmentList:
    return load_yaml_model(path, AssessmentList)
