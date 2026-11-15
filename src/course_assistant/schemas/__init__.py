"""Typed, deeply immutable records for the course assistant (Tech Spec §3, N1.1)."""

from course_assistant.schemas._base import SCHEMA_VERSION, FrozenDict
from course_assistant.schemas.config import (
    ALLOWED_STATUS,
    AppConfig,
    ConfigError,
    GoldenCase,
    load_app_config,
    load_assessments,
    load_manifest,
)
from course_assistant.schemas.corpus import (
    Assessment,
    AssessmentList,
    CorpusUnit,
    Manifest,
    ManifestEntry,
)
from course_assistant.schemas.people import (
    AUDIT_ACTIONS,
    AuditEvent,
    Consent,
    DeletionRequest,
    DigestRow,
    Draft,
    Escalation,
    PauseState,
)
from course_assistant.schemas.request import (
    Answer,
    Citation,
    Conversation,
    GroundingResult,
    Question,
    Route,
    SearchHit,
)

__all__ = [
    "ALLOWED_STATUS",
    "AUDIT_ACTIONS",
    "SCHEMA_VERSION",
    "Answer",
    "AppConfig",
    "Assessment",
    "AssessmentList",
    "AuditEvent",
    "Citation",
    "ConfigError",
    "Consent",
    "Conversation",
    "CorpusUnit",
    "DeletionRequest",
    "DigestRow",
    "Draft",
    "Escalation",
    "FrozenDict",
    "GoldenCase",
    "GroundingResult",
    "Manifest",
    "ManifestEntry",
    "PauseState",
    "Question",
    "Route",
    "SearchHit",
    "load_app_config",
    "load_assessments",
    "load_manifest",
]
