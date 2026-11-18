"""The teaching-team view logic (B2.3, TTV-01 to TTV-05). No Streamlit here: the page only
draws what these functions return, so everything is tested with fakes.

The view sits behind its own passkey (Tech Spec §8): a `PasskeyGate` on `STAFF_PASSKEY_HASH`,
never the learner gate, so a learner's passkey or unlocked session never opens it. Every
`TeachingTeam` method except `unlock`, `sign_out` and `locked_reason` calls the staff gate's
`require` first, before it reads the store, the corpus or the manifest. Every view and action
writes one `AuditEvent` (NFR7) with the staff role id and a generated id or nothing as target:
never question, draft or reply text, and never a learner pid. Failures are logged by exception
type only (D-14).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import math
import secrets
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError

from course_assistant.answer.measure import percentile
from course_assistant.app.chat import MISSING_UNIT, STATUS_LABELS, SourceChip, _session_label
from course_assistant.demo._errors import GuardRefused
from course_assistant.demo.passkey import SECRET_NAME as LEARNER_SECRET
from course_assistant.demo.passkey import PasskeyGate, read_secret, same_hash, verify_passcode
from course_assistant.digest import K_MIN, week_window
from course_assistant.escalation import EscalationError, close, flag_gap, reply, team_queue
from course_assistant.escalation.queue import STATUSES, QueueItem
from course_assistant.ingest.corpus import Corpus
from course_assistant.schemas import AppConfig, Manifest
from course_assistant.schemas._base import RoleId
from course_assistant.schemas.people import Draft
from course_assistant.store import Store

LOG = logging.getLogger("course_assistant.app.team")
STATE_KEY = "ca_team"  # never the learner page's key (ca_chat)
STAFF_SECRET = "STAFF_PASSKEY_HASH"
DEFAULT_ROLE = "instructor-1"
ALL = "all"
ALL_LABEL = "All sessions"
MAX_HITS = 3

SAME_PASSKEY = "The teaching-team page is closed: its passkey must differ from the learners'."
NOT_CONFIRMED = "Tick the confirm box first."
BAD_SCOPE = "Pick a session from the list."
BAD_WEEK = "Pick a week from the list."
BAD_STATUS = "Pick a status from the list."
READ_FAILED = "That could not be read just now. Please try again."
CHANGED = "That question changed since you opened it. Look at the queue again."
GONE = "That question is no longer in the queue."
ESCALATION_REASONS: dict[str, str] = {
    "invalid": "A reply needs visible text, up to 4,000 characters.",
    "not_found": GONE,
    "refused": "That question cannot change that way: it may have a reply or be closed.",
    "changed": CHANGED,
    "failed": "That could not be done just now. Please try again.",
}

# escalation status -> (text icon, text label): never shown by colour alone
QUEUE_STATUS: dict[str, tuple[str, str]] = {
    "open": ("○", "Waiting for a reply"),
    "replied": ("✓", "Replied"),
    "material_gap": ("!", "Flagged: a gap in the materials"),
    "closed": ("✕", "Closed"),
}
STATUS_FILTERS: dict[str, str | None] = {
    "Waiting for a reply": "open",
    "Replied": "replied",
    "Flagged gaps": "material_gap",
    "Closed": "closed",
    "All": None,
}
REASON_LABELS: dict[str, str] = {
    "not_covered": "Not covered by the course materials",
    "graded_work": "About graded work",
    "admin": "An admin question",
    "personal": "A personal matter",
    "learner_request": "The learner asked for the teaching team",
}

_ROLE = TypeAdapter(RoleId)


# ---------- the staff gate ----------


def staff_hash(
    env: Mapping[str, str] | None = None, secrets_store: Mapping[str, object] | None = None
) -> Callable[[], str | None]:
    """A `load_hash` for the staff gate: `STAFF_PASSKEY_HASH`, read at every check. None (the
    gate stays closed) when it is unset or is the learner hash itself."""

    def load() -> str | None:
        staff = read_secret(STAFF_SECRET, env=env, secrets_store=secrets_store)
        learner = read_secret(LEARNER_SECRET, env=env, secrets_store=secrets_store)
        if staff is None or (
            learner is not None and (staff == learner or same_hash(staff, learner))
        ):
            return None
        return staff

    return load


# ---------- what the page draws ----------


@dataclass
class TeamState:
    """One browser session of the teaching-team page."""

    token: str
    role: str = DEFAULT_ROLE

    @classmethod
    def new(cls, role: str = DEFAULT_ROLE) -> TeamState:
        return cls(token=secrets.token_urlsafe(24), role=role)

    def __repr__(self) -> str:
        return "TeamState()"

    __str__ = __repr__


@dataclass(frozen=True)
class QueueRow:
    question_id: str
    question: str
    session_label: str
    reason_label: str
    status: str
    icon: str
    status_label: str
    draft: str | None
    sources: tuple[SourceChip, ...]
    sources_are_hits: bool
    reply: str | None

    @property
    def heading(self) -> str:
        return f"{self.icon} {self.status_label}"

    @property
    def can_reply(self) -> bool:
        return self.reply is None and self.status in ("open", "material_gap")

    @property
    def can_flag(self) -> bool:
        return self.status in ("open", "replied")

    @property
    def can_close(self) -> bool:
        return self.status != "closed"


@dataclass(frozen=True)
class DigestLine:
    session_label: str
    topic: str
    count: int
    gaps: int


@dataclass(frozen=True)
class ManifestLine:
    manifest_id: str
    session_label: str
    kind: str
    path: str
    licence: str
    approved_by: str
    approved_at: dt.date
    speaker_notes: bool
    units: int


@dataclass(frozen=True)
class ManifestView:
    cohort: str
    corpus_version: str
    lines: tuple[ManifestLine, ...]
    units: int
    unlisted_units: int  # live units whose manifest id has no entry (a reconcile mismatch)


@dataclass(frozen=True)
class PauseLine:
    scope: str
    scope_label: str
    paused: bool
    unreadable: bool = False

    @property
    def state_label(self) -> str:
        if self.unreadable:
            return "‖ Paused (its state could not be read)"
        return "‖ Paused" if self.paused else "▶ Running"


@dataclass(frozen=True)
class OpsTile:
    questions: int
    answers: int
    status_mix: tuple[tuple[str, int], ...]  # (status label, count), every status listed
    handed_over: int
    handover_share: float | None
    p95_latency_ms: int | None
    spend_usd: float
    corpus_version: str = ""  # the served corpus id (P4.1), read-only


def _chip(file_id: str, unit_index: int, corpus: Corpus, label: str, session: str) -> SourceChip:
    unit = next((u for u in corpus.units
                 if u.file_id == file_id and u.unit_index == unit_index), None)  # fmt: skip
    if unit is None:  # never guessed from a near match
        return SourceChip(label=label, session=session, file_id=file_id, unit_index=unit_index,
                          text=MISSING_UNIT, found=False)  # fmt: skip
    return SourceChip(label=unit.label, session=unit.session, file_id=unit.file_id,
                      unit_index=unit.unit_index, text=unit.text, found=True)  # fmt: skip


def sources_for(draft: Draft | None, corpus: Corpus) -> tuple[tuple[SourceChip, ...], bool]:
    """The draft's cited units; else its closest search hits (top 3 by score). The bool says
    whether these are hits rather than citations."""
    if draft is None:
        return (), False
    if draft.citations:
        return tuple(_chip(c.file_id, c.unit_index, corpus, c.label, c.session)
                     for c in draft.citations), False  # fmt: skip
    hits = sorted(draft.hits, key=lambda h: (-h.score, h.unit_index))[:MAX_HITS]
    return tuple(_chip(h.file_id, h.unit_index, corpus, "Search result", "")
                 for h in hits), bool(hits)  # fmt: skip


def queue_row(item: QueueItem, corpus: Corpus) -> QueueRow:
    icon, label = QUEUE_STATUS[item.status]
    chips, are_hits = sources_for(item.draft, corpus)
    session = ALL_LABEL if item.session is None else _session_label(item.session)
    return QueueRow(question_id=item.question_id, question=item.question, session_label=session,
                    reason_label=REASON_LABELS[item.reason], status=item.status, icon=icon,
                    status_label=label, draft=None if item.draft is None else item.draft.text,
                    sources=chips, sources_are_hits=are_hits,
                    reply=item.reply_text)  # fmt: skip


def iso_week(day: dt.date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


# ---------- the controller ----------


class TeachingTeam:
    def __init__(self, gate: PasskeyGate, *, store: Callable[[], Store], corpus: Corpus,
                 manifest: Manifest, config: AppConfig, role: str = DEFAULT_ROLE,
                 learner_hash: Callable[[], str | None] | None = None,
                 spend: Callable[[], float] | None = None,
                 now: Callable[[], dt.datetime] | None = None) -> None:  # fmt: skip
        try:
            self.role = _ROLE.validate_python(role)
        except ValidationError:
            raise ValueError("the staff role must be a role id such as instructor-1") from None
        if manifest.cohort != corpus.cohort:
            raise ValueError("the manifest and the corpus belong to different cohorts")
        self.gate = gate
        self.store = store
        self.corpus = corpus
        self.manifest = manifest
        self.config = config
        self.learner_hash = learner_hash
        self.spend = spend
        self.now = now or (lambda: dt.datetime.now(dt.UTC))

    def __repr__(self) -> str:
        return "TeachingTeam()"

    # --- the gate (exempt from require: they are the gate) ---

    def locked_reason(self, state: TeamState) -> str | None:
        try:
            self.gate.require(state.token)
        except GuardRefused as exc:
            return exc.reason
        return None

    def unlock(self, state: TeamState, code: str, ip: str | None = None) -> None:
        self.gate.unlock(code, session=state.token, ip=ip)
        if self._learner_code(code):
            self.gate.lock_session(state.token)
            raise GuardRefused(SAME_PASSKEY)
        try:
            self._audit("staff_unlock")
        except GuardRefused:
            self.gate.lock_session(state.token)  # never open without its audit row
            raise

    def _learner_code(self, code: str) -> bool:
        """The staff code also opens the learner hash (the same code under another salt)."""
        if self.learner_hash is None:
            return False
        try:
            stored = self.learner_hash()
            return bool(stored) and verify_passcode(code, stored)  # type: ignore[arg-type]
        except Exception:
            return False  # an unreadable learner hash keeps the learner page closed, not this

    def sign_out(self, state: TeamState) -> None:
        was_open = self.locked_reason(state) is None
        self.gate.lock_session(state.token)
        state.token = TeamState.new().token
        if was_open:
            with contextlib.suppress(GuardRefused):  # signed out even if the audit fails
                self._audit("staff_sign_out")

    def _require(self, state: TeamState) -> None:
        self.gate.require(state.token)

    def _connect(self) -> Store:
        """The store, or a fixed refusal: a store that will not open never crashes the page
        (verifier B2.3 pass 1). Logged by exception type only."""
        try:
            return self.store()
        except Exception as exc:
            LOG.warning("teaching team: store open failed (%s)", type(exc).__name__)
            raise GuardRefused(READ_FAILED) from None

    def _audit(self, action: str, target: str | None = None) -> None:
        store = self._connect()
        try:
            store.audit(self.role, action, target, at=self.now())
        except Exception as exc:
            LOG.warning("teaching team: audit write failed (%s)", type(exc).__name__)
            raise GuardRefused(READ_FAILED) from None

    # --- the queue (TTV-01) ---

    def queue(self, state: TeamState, status: str | None = "open") -> tuple[QueueRow, ...]:
        self._require(state)
        if status is not None and status not in STATUSES:
            raise GuardRefused(BAD_STATUS)
        try:
            items = team_queue(self._connect(), viewer=self.role, status=status, at=self.now())
        except EscalationError:
            raise GuardRefused(READ_FAILED) from None
        try:
            return tuple(queue_row(i, self.corpus) for i in items)
        except Exception as exc:  # after the audit: a fixed refusal, never a crash (pass 2)
            LOG.warning("teaching team: queue row failed (%s)", type(exc).__name__)
            raise GuardRefused(READ_FAILED) from None

    def _check_seen(self, seen: object) -> str:
        if not isinstance(seen, str) or seen not in STATUSES:
            raise GuardRefused(BAD_STATUS)
        return seen

    # Each action names `seen`, the status the page showed when it drew the row (bound at
    # render time by the page's callback). B2.1 compares it with the current status inside
    # the store's compare-and-set transaction, so a change by another session in between is
    # refused (CHANGED) and a deleted question is refused (GONE), before any write or audit
    # (verifier B2.3 passes 2 and 3).

    def reply(self, state: TeamState, question_id: str, text: str, *, seen: str) -> None:
        self._require(state)
        expect = self._check_seen(seen)
        store = self._connect()
        self._escalation(lambda: reply(question_id, text, instructor=self.role, store=store,
                                       at=self.now(), expect=expect))  # fmt: skip

    def flag_gap(self, state: TeamState, question_id: str, *, seen: str) -> None:
        self._require(state)
        expect = self._check_seen(seen)
        store = self._connect()
        self._escalation(lambda: flag_gap(question_id, instructor=self.role, store=store,
                                          at=self.now(), expect=expect))  # fmt: skip

    def close(self, state: TeamState, question_id: str, *, seen: str) -> None:
        self._require(state)
        expect = self._check_seen(seen)
        store = self._connect()
        self._escalation(lambda: close(question_id, instructor=self.role, store=store,
                                       at=self.now(), expect=expect))  # fmt: skip

    @staticmethod
    def _escalation(fn: Callable[[], Any]) -> None:
        try:
            fn()
        except EscalationError as exc:
            raise GuardRefused(ESCALATION_REASONS[exc.code]) from None

    # --- the digest (TTV-02) ---

    def weeks(self, state: TeamState, n: int = 8) -> list[str]:
        """The current ISO week first, then the `n - 1` before it."""
        self._require(state)
        today = self.now().astimezone(dt.UTC).date()
        return [iso_week(today - dt.timedelta(weeks=i)) for i in range(max(1, n))]

    def digest(self, state: TeamState, week: str) -> tuple[DigestLine, ...]:
        self._require(state)
        try:
            week_window(week)
        except Exception:
            raise GuardRefused(BAD_WEEK) from None
        try:
            rows = self._connect().digest(week)
        except GuardRefused:
            raise
        except Exception as exc:
            LOG.warning("teaching team: digest read failed (%s)", type(exc).__name__)
            raise GuardRefused(READ_FAILED) from None
        k = max(K_MIN, self.config.digest_k)
        self._audit("view_digest")
        return tuple(DigestLine(session_label=_session_label(r.session), topic=r.topic,
                                count=r.count, gaps=r.gaps)
                     for r in rows if r.count >= k and r.week == week)  # fmt: skip

    # --- the manifest (TTV-03) ---

    def manifest_view(self, state: TeamState) -> ManifestView:
        """Read-only: nothing here writes the manifest or the corpus."""
        self._require(state)
        per_entry = Counter(u.manifest_id for u in self.corpus.units)
        listed = {e.manifest_id for e in self.manifest.entries}
        lines = tuple(
            ManifestLine(manifest_id=e.manifest_id, session_label=_session_label(e.session),
                         kind=e.kind, path=e.path, licence=e.licence, approved_by=e.approved_by,
                         approved_at=e.approved_at, speaker_notes=e.include_speaker_notes,
                         units=per_entry.get(e.manifest_id, 0))
            for e in self.manifest.entries
        )  # fmt: skip
        unlisted = sum(n for mid, n in per_entry.items() if mid not in listed)
        self._audit("view_manifest")
        return ManifestView(cohort=self.manifest.cohort, corpus_version=self.corpus.corpus_version,
                            lines=lines, units=len(self.corpus.units),
                            unlisted_units=unlisted)  # fmt: skip

    # --- pause switches (TTV-04) ---

    def _scopes(self) -> list[str]:
        return [ALL, *sorted({u.session for u in self.corpus.units})]

    def pauses(self, state: TeamState) -> tuple[PauseLine, ...]:
        """Each scope's own state, audited `view_pauses`. Fail closed: an unreadable state
        shows as paused, and a store that will not open (or an audit that cannot be written)
        shows every scope as paused."""
        self._require(state)
        labels = [(s, ALL_LABEL if s == ALL else _session_label(s)) for s in self._scopes()]
        unreadable = tuple(PauseLine(scope=s, scope_label=label, paused=True, unreadable=True)
                           for s, label in labels)  # fmt: skip
        try:
            store = self._connect()
        except GuardRefused:
            return unreadable
        out = []
        for line, (scope, label) in zip(unreadable, labels, strict=True):
            try:
                ps = store.pause_state(scope)
            except Exception as exc:  # fail closed: an unreadable state shows as paused
                LOG.warning("teaching team: pause read failed (%s)", type(exc).__name__)
                out.append(line)
                continue
            out.append(
                PauseLine(scope=scope, scope_label=label, paused=ps is not None and ps.paused)
            )
        try:
            self._audit("view_pauses")
        except GuardRefused:
            return unreadable
        return tuple(out)

    def set_pause(self, state: TeamState, scope: str, paused: bool, *,
                  confirmed: bool = False) -> None:  # fmt: skip
        """N1.5's `set_pause`: the pause row and its `pause` or `resume` audit in one
        transaction. Refused without the confirmation or for a scope outside the corpus."""
        self._require(state)
        if confirmed is not True:
            raise GuardRefused(NOT_CONFIRMED)
        if scope not in self._scopes() or not isinstance(paused, bool):
            raise GuardRefused(BAD_SCOPE)
        store = self._connect()
        try:
            store.set_pause(scope, paused, self.role, at=self.now())
        except Exception as exc:
            LOG.warning("teaching team: pause write failed (%s)", type(exc).__name__)
            raise GuardRefused(READ_FAILED) from None

    # --- the ops tile (TTV-05) ---

    def ops(self, state: TeamState) -> OpsTile:
        self._require(state)
        store = self._connect()
        try:
            lo = dt.datetime(1970, 1, 2, tzinfo=dt.UTC)
            questions = store.questions_between(lo, self.now() + dt.timedelta(days=1))
            answers = [a for a in (store.get_answer(q.question_id) for q in questions) if a]
            escalated = {e.question_id for e in store.escalations()}
            extra = 0.0 if self.spend is None else float(self.spend())
            if not math.isfinite(extra) or extra < 0:
                raise ValueError("spend must be a finite non-negative number")
        except Exception as exc:
            LOG.warning("teaching team: ops read failed (%s)", type(exc).__name__)
            raise GuardRefused(READ_FAILED) from None
        mix = Counter(a.status for a in answers)
        order = ("answered", "not_covered", "hint_only", "handed_over", "error")
        status_mix = tuple((STATUS_LABELS[s][1], mix.get(s, 0)) for s in order)
        handed = {a.question_id for a in answers if a.status == "handed_over"}
        sent = len({q.question_id for q in questions} & (escalated | handed))
        latencies = [float(a.latency_ms) for a in answers]
        p95 = int(percentile(latencies, 95)) if latencies else None
        spend = math.fsum([*(a.cost_usd for a in answers), extra])
        self._audit("view_ops")
        return OpsTile(questions=len(questions), answers=len(answers), status_mix=status_mix,
                       handed_over=sent, handover_share=sent / len(questions) if questions
                       else None, p95_latency_ms=p95, spend_usd=spend,
                       corpus_version=self.corpus.corpus_version)  # fmt: skip
