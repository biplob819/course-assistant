"""Request schemas: one learner question through to the answer shown (SCH-02)."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from course_assistant.schemas._base import (
    ConversationId,
    Fraction,
    Ident,
    Model,
    Name,
    NonBlank,
    NonNegFloat,
    NonNegInt,
    Pid,
    PosInt,
    QuestionId,
    Score,
    SessionId,
    ShortHash,
    Timestamp,
)

Category = Literal["course", "graded_work", "admin", "personal", "off_topic", "injection"]
Status = Literal["answered", "not_covered", "hint_only", "handed_over", "error"]


class Conversation(Model):
    conversation_id: ConversationId
    learner_pid: Pid
    session_scope: SessionId | None
    started_at: Timestamp
    closed_at: Timestamp | None = None

    @model_validator(mode="after")
    def _order(self) -> Conversation:
        if self.closed_at is not None and self.closed_at < self.started_at:
            raise ValueError("closed_at is before started_at")
        return self


class Question(Model):
    question_id: QuestionId
    conversation_id: ConversationId
    turn_index: PosInt
    learner_pid: Pid
    session_scope: SessionId | None
    text: NonBlank
    asked_at: Timestamp


class Route(Model):
    question_id: QuestionId
    category: Category
    confidence: Fraction
    standalone_question: NonBlank
    assessment_id: Ident | None = None
    route_version: Name

    @model_validator(mode="after")
    def _assessment_only_for_course_or_graded(self) -> Route:
        if self.assessment_id is not None and self.category not in ("course", "graded_work"):
            raise ValueError("assessment_id is only set for course or graded_work routes")
        return self


class Citation(Model):
    file_id: Name
    unit_index: PosInt
    label: NonBlank
    session: SessionId


class SearchHit(Model):
    file_id: Name
    score: Score
    unit_index: PosInt


class GroundingResult(Model):
    citations_resolve: bool
    top_score: Score
    bar: Score
    sentence_support: tuple[bool, ...]
    passed: bool
    ground_version: Name

    @model_validator(mode="after")
    def _passed_is_consistent(self) -> GroundingResult:
        expected = (
            self.citations_resolve
            and self.top_score >= self.bar
            and len(self.sentence_support) > 0
            and all(self.sentence_support)
        )
        if self.passed != expected:
            raise ValueError(
                "passed must equal: citations resolve, top score >= bar, all supported"
            )
        return self


class Answer(Model):
    question_id: QuestionId
    status: Status
    text: str
    citations: tuple[Citation, ...]
    hits: tuple[SearchHit, ...] = ()
    grounding: GroundingResult | None = None
    integrity_passed: bool | None = None
    model_snapshot: Name
    rules_hash: ShortHash
    corpus_version: Name
    top_search_score: Score
    cost_usd: NonNegFloat
    latency_ms: NonNegInt
    created_at: Timestamp

    @model_validator(mode="after")
    def _status_rules(self) -> Answer:
        if self.status == "answered":
            if not self.citations:
                raise ValueError("an answered status needs at least one citation (B1)")
            if self.grounding is None or not self.grounding.passed:
                raise ValueError("an answered status needs a passed grounding result")
            if self.integrity_passed is not True:
                raise ValueError("an answered status needs integrity_passed")
            if not self.text.strip():
                raise ValueError("an answered status needs text")
        elif self.status in ("not_covered", "handed_over", "error") and self.citations:
            raise ValueError(f"{self.status} answers carry no citations for the learner")
        return self
