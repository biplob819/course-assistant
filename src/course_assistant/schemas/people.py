"""People and privacy schemas: handover, consent, deletion, digest, pause, audit (SCH-03)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from course_assistant.schemas._base import (
    ConversationId,
    Model,
    NonBlank,
    Pid,
    PosInt,
    QuestionId,
    RoleId,
    SessionId,
    Timestamp,
    Version,
)
from course_assistant.schemas.request import Citation, SearchHit

Reason = Literal["not_covered", "graded_work", "admin", "personal", "learner_request"]
Purpose = Literal["answer_questions", "improve_course"]
WeekId = Annotated[str, StringConstraints(pattern=r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")]
ACTOR_PATTERN = r"^([0-9a-f]{16}|(instructor|ta|owner|system)-[0-9]{1,4})$"
# audit targets are generated ids only: a question, conversation, escalation or manifest id
# (prefix + hex), a session, "all", or a pid, so a sentence cannot be written as a "slug"
TARGET_PATTERN = r"^((q|c)-[0-9a-f]{12,32}|m-[a-z0-9-]{1,40}|s\d\d|all|[0-9a-f]{16})$"
AUDIT_ACTIONS = (
    "view_queue",
    "reply",
    "flag_gap",
    "close",
    "pause",
    "resume",
    "view_digest",
    "view_manifest",
    "give_consent",
    "withdraw_consent",
    "delete_history",
    "purge",
    "settle_failed",
)
AuditAction = Literal[
    "view_queue",
    "reply",
    "flag_gap",
    "close",
    "pause",
    "resume",
    "view_digest",
    "view_manifest",
    "give_consent",
    "withdraw_consent",
    "delete_history",
    "purge",
    "settle_failed",
]


class Draft(Model):
    """The assistant's draft for a handed-over question: text plus the sources it used.
    Shown to the teaching team only, never to the learner unless an instructor sends it."""

    text: str
    citations: tuple[Citation, ...] = ()
    hits: tuple[SearchHit, ...] = ()


class Escalation(Model):
    question_id: QuestionId
    reason: Reason
    draft: Draft | None = None
    status: Literal["open", "replied", "material_gap", "closed"]
    reply_text: NonBlank | None = None
    replied_by: RoleId | None = None  # a role id, never a name
    created_at: Timestamp
    replied_at: Timestamp | None = None

    @model_validator(mode="after")
    def _reply_rules(self) -> Escalation:
        reply = (self.reply_text, self.replied_by, self.replied_at)
        if any(v is not None for v in reply) and any(v is None for v in reply):
            raise ValueError("reply_text, replied_by and replied_at are set together or not at all")
        if self.status == "replied":
            if any(v is None for v in reply):
                raise ValueError("a replied escalation needs reply_text, replied_by and replied_at")
        elif self.status == "open" and any(v is not None for v in reply):
            raise ValueError("an open escalation has no reply yet")
        if self.replied_at is not None and self.replied_at < self.created_at:
            raise ValueError("replied_at is before created_at")
        return self


class Consent(Model):
    learner_pid: Pid
    notice_version: Version
    purposes: tuple[Purpose, ...] = Field(min_length=1)
    given_at: Timestamp
    withdrawn_at: Timestamp | None = None

    @field_validator("purposes")
    @classmethod
    def _unique(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError("purposes must be unique")
        return v

    @model_validator(mode="after")
    def _order(self) -> Consent:
        if self.withdrawn_at is not None and self.withdrawn_at <= self.given_at:
            raise ValueError("withdrawn_at must be after given_at")
        return self


class DeletionRequest(Model):
    learner_pid: Pid
    conversation_id: ConversationId | None = None  # None deletes the whole history
    # the conversation ids the request removed, kept so trace lines can still be found after
    # the app rows are gone (ids only, never text; verifier B2.2 pass 2)
    covers: tuple[ConversationId, ...] = ()
    requested_at: Timestamp
    app_deleted_at: Timestamp | None = None
    traces_deleted_at: Timestamp | None = None

    @model_validator(mode="after")
    def _order(self) -> DeletionRequest:
        for t in (self.app_deleted_at, self.traces_deleted_at):
            if t is not None and t < self.requested_at:
                raise ValueError("a completion time is before requested_at")
        return self

    @property
    def complete(self) -> bool:
        return self.app_deleted_at is not None and self.traces_deleted_at is not None


class DigestRow(Model):
    week: WeekId
    session: SessionId
    topic: NonBlank  # a model-written label, never a quote
    count: PosInt


class PauseState(Model):
    scope: Annotated[str, StringConstraints(pattern=r"^(all|s\d\d)$")]
    paused: bool
    set_by: RoleId
    set_at: Timestamp


class AuditEvent(Model):
    """Access log entry (NFR7). No free text: the action is a fixed vocabulary and the target
    an id, so question text cannot be written here (finding H9)."""

    at: Timestamp
    actor: Annotated[str, StringConstraints(pattern=ACTOR_PATTERN)]  # a pid or a role id
    action: AuditAction
    target: Annotated[str, StringConstraints(pattern=TARGET_PATTERN)] | None = None
