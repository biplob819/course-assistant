"""Corpus schemas: the approved manifest, the units ingest makes, and assessment briefs (SCH-01)."""

from __future__ import annotations

import datetime as dt
import posixpath
from typing import Literal

from pydantic import Field, model_validator

from course_assistant.schemas._base import (
    Ident,
    Model,
    Name,
    NonBlank,
    PosInt,
    RoleId,
    SessionId,
    Sha256,
    Versioned,
)

Kind = Literal["slides", "pre_read", "agenda", "post_session", "faq"]


class ManifestEntry(Model):
    manifest_id: Ident
    path: Name  # relative to the corpus root
    session: SessionId
    kind: Kind
    sha256: Sha256
    licence: Name
    approved_by: RoleId  # a role id, e.g. "owner-1", never a name or email (D-28)
    approved_at: dt.date
    include_speaker_notes: bool = False

    @model_validator(mode="after")
    def _relative_path(self) -> ManifestEntry:
        p = self.path
        if any(ord(c) < 32 for c in p) or p.startswith("~"):
            raise ValueError("path must not hold control characters or start with ~")
        if p.startswith(("/", "\\")) or ".." in p.replace("\\", "/").split("/") or ":" in p:
            raise ValueError("path must be relative to the corpus root, without '..'")
        return self

    @property
    def norm_path(self) -> str:
        """The path compared for uniqueness: forward slashes, no empty or '.' parts."""
        return posixpath.normpath(self.path.replace("\\", "/"))


class Manifest(Versioned):
    cohort: Ident
    entries: tuple[ManifestEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Manifest:
        ids = [e.manifest_id for e in self.entries]
        paths = [e.norm_path for e in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("manifest_id values must be unique")
        if len(set(paths)) != len(paths):
            raise ValueError("paths must be unique")
        return self


class CorpusUnit(Model):
    """One slide or one page (D-2)."""

    unit_index: PosInt  # 1-based, stable order
    file_id: Name
    vs_file_id: Name
    manifest_id: Ident
    session: SessionId
    kind: Kind
    locator: NonBlank  # "slide 14" | "p. 2" | "agenda"
    label: NonBlank  # "Session 3 slides, slide 14"
    text: str
    version: Sha256
    corpus_version: Sha256


class Assessment(Model):
    """A graded assessment brief. Briefs only: there is no solution field, and the model
    refuses one under any name, so a solution cannot ride along into a prompt (D-7)."""

    assessment_id: Ident
    title: NonBlank
    brief: NonBlank
    sessions: tuple[SessionId, ...] = Field(min_length=1)


class AssessmentList(Versioned):
    assessments: tuple[Assessment, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> AssessmentList:
        ids = [a.assessment_id for a in self.assessments]
        if len(set(ids)) != len(ids):
            raise ValueError("assessment_id values must be unique")
        return self
