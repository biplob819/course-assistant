"""The teaching-team view over its own staff gate, next to B2.5's learner chat rig: learners
ask and escalate through the real chat (D3.1 gate and guard, scripted outputs through the
real `respond`), and the team reads and acts on the same file-backed store (B2.3)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from course_assistant.app.team import TeachingTeam, TeamState
from course_assistant.demo import passkey
from course_assistant.schemas import Manifest, ManifestEntry
from tests.answer.test_ask import CONFIG, CORPUS
from tests.app.rig import ChatRig
from tests.demo.test_demo import FAST, OTHER_CODE, SALT, Secret, Tick

STAFF_CODE = OTHER_CODE
STAFF_HASH = passkey.hash_passcode(STAFF_CODE, salt=SALT, iterations=FAST)
NOW = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.UTC)  # ISO week 2026-W47
WEEK = "2026-W47"


def manifest() -> Manifest:
    """Entries for two of the corpus's files and one with no live units; the corpus's
    Session 2 pre-read has no entry (a reconcile mismatch the view must show)."""

    def entry(mid: str, session: str, kind: str) -> ManifestEntry:
        return ManifestEntry(manifest_id=mid, path=f"course/{mid}.pdf", session=session,
                             kind=kind, sha256="a" * 64, licence="CC-BY-4.0",  # type: ignore[arg-type]
                             approved_by="owner-1", approved_at=dt.date(2026, 11, 15))  # fmt: skip

    return Manifest(schema_version="1.0", cohort="sample", entries=(
        entry("m-s01-slides", "s01", "slides"), entry("m-s02-agenda", "s02", "agenda"),
        entry("m-s03-slides", "s03", "slides")))  # fmt: skip


class TeamRig:
    def __init__(self, tmp: Path, *, owner: dict[str, list[Any]] | None = None,
                 config: Any = CONFIG, spend: Any = None) -> None:  # fmt: skip
        self.learners = ChatRig(tmp, owner=owner)
        self.staff_secret = Secret(STAFF_HASH)
        self.clock = Tick()
        self.gate = passkey.PasskeyGate(self.staff_secret, clock=self.clock)
        self.now = NOW
        self.team = TeachingTeam(self.gate, store=self.learners.stores, corpus=CORPUS,
                                 manifest=manifest(), config=config,
                                 learner_hash=self.learners.secret, spend=spend,
                                 now=lambda: self.now)  # fmt: skip
        self.state = TeamState.new()

    def store(self) -> Any:
        return self.learners.store()

    def unlock(self, state: TeamState | None = None) -> TeamState:
        st = state or self.state
        self.team.unlock(st, STAFF_CODE)
        return st

    def escalated(self, question: str = "Is there a session on pricing models?") -> str:
        """A learner asks a question the materials do not cover and taps "Ask the teaching
        team"; the question id."""
        st = self.learners.ready(self.learners.state)
        card = self.learners.chat.ask(st, question)
        assert card.question_id is not None and card.can_escalate
        self.learners.chat.escalate(st, card.question_id)
        return card.question_id

    def audits(self) -> list[Any]:
        return self.store().audit_events()
