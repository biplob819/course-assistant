"""The app store (N1.5): every learner-facing record in one SQLite file, so deletion,
retention and access logging are enforced in one place.

Records are stored as validated model JSON in one table, keyed by kind and id, with indexed
pid, conversation and time columns. Reads validate back into the strict models, so a row
edited outside the app cannot come back as a valid record that breaks an invariant.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from course_assistant.schemas import (
    Answer,
    AuditEvent,
    Consent,
    Conversation,
    DeletionRequest,
    Escalation,
    PauseState,
    Question,
)

M = TypeVar("M", bound=BaseModel)
TEXT_KINDS = ("conversation", "question", "answer", "escalation")


@dataclass(frozen=True)
class RetentionPolicy:
    """Tech Spec §7: text for pilot end + 30 days, consent for pilot end + 1 year,
    access logs for 1 year, deletion requests complete within 7 days."""

    pilot_end: dt.date
    text_days: int = 30
    consent_years: int = 1  # calendar years, so a leap day never shortens the period
    audit_years: int = 1
    deletion_days: int = 7


@dataclass(frozen=True)
class PurgeReport:
    text_rows_deleted: int
    consent_rows_deleted: int
    audit_rows_deleted: int
    overdue_deletions: int
    wal_cleared: bool = True  # False while another reader kept deleted text in the WAL


def _iso(t: dt.datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return t.astimezone(dt.UTC).isoformat()


def _add_years(t: dt.datetime, years: int) -> dt.datetime:
    """Same calendar date `years` later or earlier. 29 February has no match in a common
    year: forwards it becomes 1 March, backwards 28 February, so a retention period is never
    shorter than a full calendar year in either direction."""
    try:
        return t.replace(year=t.year + years)
    except ValueError:
        if years >= 0:
            return t.replace(year=t.year + years, month=3, day=1)
        return t.replace(year=t.year + years, month=2, day=28)


class Store:
    MIGRATIONS: tuple[str, ...] = (
        """create table if not exists records (
               kind text not null, key text not null, pid text, conversation_id text,
               at text not null, json text not null, primary key (kind, key))""",
        "create index if not exists records_pid on records (kind, pid)",
        "create index if not exists records_conversation on records (kind, conversation_id)",
        "create index if not exists records_at on records (kind, at)",
        # a counter that never goes back, so audit and deletion keys are unique for ever,
        # even after purge removes old rows (verifier N1.5)
        "create table if not exists counters (name text primary key, value integer not null)",
    )

    def __init__(self, path: Path | str, timeout: float = 30.0) -> None:
        self._con = sqlite3.connect(str(path), isolation_level=None, timeout=timeout)
        self._con.execute("pragma foreign_keys = on")
        self._con.execute("pragma secure_delete = on")  # deleted text is overwritten, not left
        if str(path) != ":memory:":
            self._con.execute("pragma journal_mode = wal")
        self._migrate()

    def close(self) -> None:
        self._con.close()

    # ---------- schema ----------

    def _migrate(self) -> None:
        # the whole check-and-apply runs under one write lock, so two processes opening a
        # fresh file at once cannot both apply the same migration
        with self._tx():
            self._con.execute(
                "create table if not exists schema_migrations (version integer primary key)"
            )
            done = {r[0] for r in self._con.execute("select version from schema_migrations")}
            for version, sql in enumerate(self.MIGRATIONS, start=1):
                if version not in done:
                    self._con.execute(sql)
                    self._con.execute("insert into schema_migrations values (?)", (version,))

    def _next(self, name: str) -> int:
        self._con.execute(
            "insert into counters values (?, 1) on conflict(name) do update set value = value + 1",
            (name,),
        )
        return self._con.execute("select value from counters where name = ?", (name,)).fetchone()[0]

    def schema_version(self) -> int:
        return self._con.execute("select max(version) from schema_migrations").fetchone()[0]

    @contextmanager
    def _tx(self) -> Iterator[None]:
        self._con.execute("begin immediate")
        try:
            yield
        except BaseException:
            self._con.execute("rollback")
            raise
        self._con.execute("commit")

    # ---------- generic ----------

    def _put(self, kind: str, key: str, model: BaseModel, *, pid: str | None = None,
             conversation_id: str | None = None, at: dt.datetime) -> None:  # fmt: skip
        self._con.execute(
            "insert or replace into records values (?, ?, ?, ?, ?, ?)",
            (kind, key, pid, conversation_id, _iso(at), model.model_dump_json()),
        )

    def _get(self, kind: str, key: str, model: type[M]) -> M | None:
        row = self._con.execute(
            "select json from records where kind = ? and key = ?", (kind, key)
        ).fetchone()
        return None if row is None else self._load(model, row[0])

    def _list(self, kind: str, model: type[M], where: str = "", args: tuple = (),
              order: str = "at, key") -> list[M]:  # fmt: skip
        sql = f"select json from records where kind = ? {where} order by {order}"
        return [self._load(model, r[0]) for r in self._con.execute(sql, (kind, *args))]

    def _for_pid(self, kind: str, model: type[M], pid: str, where: str = "",
                 args: tuple = (), order: str = "at, key") -> list[M]:  # fmt: skip
        """Rows for a learner, checked against the pid inside each record: a row whose key
        column was edited to another learner's pid is refused, not returned."""
        rows = self._list(kind, model, "and pid = ? " + where, (pid, *args), order)
        for r in rows:
            if getattr(r, "learner_pid", pid) != pid:
                raise ValueError(f"stored {model.__name__} pid column does not match its record")
        return rows

    @staticmethod
    def _load(model: type[M], raw: str) -> M:
        try:
            return model.model_validate_json(raw)
        except ValidationError as exc:
            raise ValueError(f"stored {model.__name__} failed validation") from exc

    # ---------- conversations, questions, answers ----------

    def put_conversation(self, c: Conversation) -> None:
        with self._tx():
            existing = self.get_conversation(c.conversation_id)
            if existing is not None and existing.learner_pid != c.learner_pid:
                raise KeyError(f"conversation {c.conversation_id} belongs to another learner")
            self._put("conversation", c.conversation_id, c, pid=c.learner_pid,
                      conversation_id=c.conversation_id, at=c.started_at)  # fmt: skip

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._get("conversation", conversation_id, Conversation)

    def conversations_for(self, pid: str) -> list[Conversation]:
        return self._for_pid("conversation", Conversation, pid)

    def put_question(self, q: Question) -> None:
        """A question joins an existing conversation of the same learner, and can never
        replace another learner's question (verifier N1.5 pass 2)."""
        with self._tx():
            conv = self.get_conversation(q.conversation_id)
            if conv is None or conv.learner_pid != q.learner_pid:
                raise KeyError(f"no conversation {q.conversation_id} for this learner")
            existing = self.get_question(q.question_id)
            if existing is not None and existing.learner_pid != q.learner_pid:
                raise KeyError(f"question {q.question_id} belongs to another learner")
            if existing is not None and existing.conversation_id != q.conversation_id:
                # its answer and escalation are keyed to the first conversation
                raise KeyError(f"question {q.question_id} cannot move to another conversation")
            self._put("question", q.question_id, q, pid=q.learner_pid,
                      conversation_id=q.conversation_id, at=q.asked_at)  # fmt: skip

    def get_question(self, question_id: str) -> Question | None:
        return self._get("question", question_id, Question)

    def questions_for(self, pid: str, conversation_id: str | None = None) -> list[Question]:
        if conversation_id is None:
            return self._for_pid("question", Question, pid)
        return self._for_pid("question", Question, pid, "and conversation_id = ?",
                             (conversation_id,))  # fmt: skip

    def _owner(self, question_id: str) -> tuple[str | None, str | None]:
        row = self._con.execute(
            "select pid, conversation_id from records where kind = 'question' and key = ?",
            (question_id,),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def _require_owner(self, question_id: str) -> tuple[str, str]:
        """Answers and escalations belong to a stored question; without one they would have
        no learner, escape scoping and survive deletion (verifier N1.5)."""
        pid, cid = self._owner(question_id)
        if pid is None or cid is None:
            raise KeyError(f"no stored question {question_id}")
        return pid, cid

    def put_answer(self, a: Answer) -> None:
        with self._tx():  # owner lookup and write in one transaction: no deletion in between
            pid, cid = self._require_owner(a.question_id)
            self._put("answer", a.question_id, a, pid=pid, conversation_id=cid, at=a.created_at)

    def get_answer(self, question_id: str) -> Answer | None:
        return self._get("answer", question_id, Answer)

    # ---------- escalations ----------

    def put_escalation(self, e: Escalation) -> None:
        with self._tx():
            self._put_escalation(e)

    def _put_escalation(self, e: Escalation) -> None:
        pid, cid = self._require_owner(e.question_id)
        self._put("escalation", e.question_id, e, pid=pid, conversation_id=cid, at=e.created_at)

    def set_escalation_status(self, question_id: str, status: str, actor: str, *,
                              at: dt.datetime) -> Escalation:  # fmt: skip
        """Teaching-team status change (material_gap, closed), audited."""
        action = {"material_gap": "flag_gap", "closed": "close"}.get(status)
        if action is None:
            raise ValueError("use reply() to answer; status must be material_gap or closed")
        with self._tx():  # read and write in one transaction: a concurrent reply is not lost
            current = self.get_escalation(question_id)
            if current is None:
                raise KeyError(f"no escalation for {question_id}")
            updated = current.model_copy(update={"status": status})
            self._put_escalation(updated)
            self._audit(actor, action, question_id, at)
        return updated

    def get_escalation(self, question_id: str) -> Escalation | None:
        return self._get("escalation", question_id, Escalation)

    def escalations(self, status: str | None = None) -> list[Escalation]:
        rows = self._list("escalation", Escalation)
        return [e for e in rows if status is None or e.status == status]

    def reply(self, question_id: str, text: str, instructor: str, *, at: dt.datetime) -> Escalation:
        with self._tx():
            current = self.get_escalation(question_id)
            if current is None:
                raise KeyError(f"no escalation for {question_id}")
            change = {"status": "replied", "reply_text": text, "replied_by": instructor}
            updated = current.model_copy(update={**change, "replied_at": at})
            self._put_escalation(updated)
            self._audit(instructor, "reply", question_id, at)
        return updated

    # ---------- consent ----------

    def put_consent(self, c: Consent) -> None:
        with self._tx():
            key = f"{c.learner_pid}:{self._next('consent'):012d}"
            self._put("consent", key, c, pid=c.learner_pid, at=c.given_at)

    def give_consent(self, c: Consent) -> None:
        """Record a consent and its audit event in one transaction (B2.2)."""
        with self._tx():
            key = f"{c.learner_pid}:{self._next('consent'):012d}"
            self._put("consent", key, c, pid=c.learner_pid, at=c.given_at)
            self._audit(c.learner_pid, "give_consent", c.learner_pid, c.given_at)

    def withdraw_consent(self, pid: str, *, at: dt.datetime) -> int:
        """Close every open consent of the learner and audit the withdrawal, in one
        transaction. The rows stay (proof of consent, §7) with `withdrawn_at` set; a consent
        stamped at or after `at` (a clock behind the stored time) is closed one microsecond
        after it was given, so a withdrawal never fails to close a consent (B2.2)."""
        _iso(at)
        closed = 0
        with self._tx():
            rows = self._con.execute(
                "select key, json from records where kind = 'consent' and pid = ?", (pid,)
            ).fetchall()
            for key, raw in rows:
                c = self._load(Consent, raw)
                if c.learner_pid != pid:
                    raise ValueError("stored Consent pid column does not match its record")
                if c.withdrawn_at is None:
                    when = max(at, c.given_at + dt.timedelta(microseconds=1))
                    done = c.model_copy(update={"withdrawn_at": when})
                    self._put("consent", key, done, pid=pid, at=c.given_at)
                    closed += 1
            self._audit(pid, "withdraw_consent", pid, at)
        return closed

    def consents_for(self, pid: str) -> list[Consent]:
        # in the order recorded (keys are pid:counter), so the consent recorded last wins even
        # if its given-at time is earlier (verifier B2.2 pass 1)
        return self._for_pid("consent", Consent, pid, order="key")

    # ---------- pause ----------

    def set_pause(self, scope: str, paused: bool, actor: str, *, at: dt.datetime) -> PauseState:
        state = PauseState(scope=scope, paused=paused, set_by=actor, set_at=at)
        with self._tx():
            self._put("pause", scope, state, at=at)
            self._audit(actor, "pause" if paused else "resume", scope, at)
        return state

    def is_paused(self, session: str | None) -> bool:
        """Paused if "all" or this session is paused. Any read error counts as paused."""
        try:
            for scope in ("all", session):
                if scope is None:
                    continue
                state = self._get("pause", scope, PauseState)
                if state is not None and state.scope != scope:
                    return True  # a row whose key and record disagree: fail closed
                if state is not None and state.paused:
                    return True
            return False
        except Exception:
            return True

    # ---------- audit ----------

    def _audit(self, actor: str, action: str, target: str | None, at: dt.datetime) -> None:
        event = AuditEvent(at=at, actor=actor, action=action, target=target)  # type: ignore[arg-type]
        self._put("audit", f"{_iso(at)}:{self._next('audit'):012d}", event, at=at)

    def audit(self, actor: str, action: str, target: str | None, *, at: dt.datetime) -> None:
        with self._tx():
            self._audit(actor, action, target, at)

    def audit_events(self) -> list[AuditEvent]:
        return self._list("audit", AuditEvent)

    # ---------- deletion and retention ----------

    def delete_history(self, pid: str, conversation_id: str | None = None, *,
                       at: dt.datetime) -> DeletionRequest:  # fmt: skip
        """Remove the learner's questions, answers, escalations and conversations (or one
        conversation) in one transaction. Traces are deleted separately (B2.2)."""
        with self._tx():
            if conversation_id is None:
                covers = tuple(sorted({c.conversation_id for c in self.conversations_for(pid)}))
            else:
                covers = (conversation_id,)
            request = DeletionRequest(learner_pid=pid, conversation_id=conversation_id,
                                      covers=covers, requested_at=at,
                                      app_deleted_at=at)  # fmt: skip
            marks = ",".join("?" * len(TEXT_KINDS))
            if conversation_id is None:
                self._con.execute(f"delete from records where kind in ({marks}) and pid = ?",
                                  (*TEXT_KINDS, pid))  # fmt: skip
            else:
                self._con.execute(
                    f"delete from records where kind in ({marks}) and pid = ? "
                    "and conversation_id = ?", (*TEXT_KINDS, pid, conversation_id))  # fmt: skip
            self._put("deletion", f"{pid}:{self._next('deletion'):012d}", request, pid=pid, at=at)
            self._audit(pid, "delete_history", pid, at)
        self._checkpoint()
        return request

    last_checkpoint_complete: bool = True

    def _checkpoint(self, tries: int = 10) -> bool:
        """Move committed changes out of the WAL into the main file, where secure_delete has
        zeroed deleted text. A reader holding an old snapshot blocks it: retry briefly with a
        short busy timeout (never minutes), and record whether it completed."""
        import time

        old = self._con.execute("pragma busy_timeout").fetchone()[0]
        self._con.execute("pragma busy_timeout = 50")
        try:
            for _ in range(tries):
                busy, _log, _done = self._con.execute("pragma wal_checkpoint(truncate)").fetchone()
                if busy == 0:
                    self.last_checkpoint_complete = True
                    return True
                time.sleep(0.05)
            self.last_checkpoint_complete = False
            return False
        finally:
            self._con.execute(f"pragma busy_timeout = {int(old)}")

    def deletions(self) -> list[DeletionRequest]:
        rows = self._con.execute(
            "select pid, json from records where kind = 'deletion' order by at, key"
        ).fetchall()
        out = []
        for pid, raw in rows:
            d = self._load(DeletionRequest, raw)
            if d.learner_pid != pid:
                raise ValueError("stored DeletionRequest pid column does not match its record")
            out.append(d)
        return out

    def mark_traces_deleted(self, pid: str, *, at: dt.datetime,
                            upto: dt.datetime | None = None) -> None:  # fmt: skip
        with self._tx():
            rows = self._con.execute(
                "select key, json from records where kind = 'deletion' and pid = ?", (pid,)
            ).fetchall()
            for key, raw in rows:
                d = self._load(DeletionRequest, raw)
                if d.learner_pid != pid:
                    raise ValueError("stored DeletionRequest pid column does not match its record")
                # only requests the caller saw, never one created after it read them (pass 1)
                if d.traces_deleted_at is None and (upto is None or d.requested_at <= upto):
                    # never before the request, even if the clock went back (pass 2)
                    done = d.model_copy(update={"traces_deleted_at": max(at, d.requested_at)})
                    self._put("deletion", key, done, pid=pid, at=d.requested_at)
            self._audit("system-1", "purge", pid, at)  # trace deletion for this learner

    def purge(self, now: dt.datetime, policy: RetentionPolicy) -> PurgeReport:
        _iso(now)  # refuses a naive time
        now = now.astimezone(dt.UTC)  # calendar arithmetic in UTC, whatever the caller's zone
        # the pilot ends at the close of its last day (start of the next day, UTC)
        end = dt.datetime.combine(policy.pilot_end, dt.time(), tzinfo=dt.UTC) + dt.timedelta(1)
        text, consent, audit = 0, 0, 0
        with self._tx():
            if now >= end + dt.timedelta(days=policy.text_days):
                marks = ",".join("?" * len(TEXT_KINDS))
                text = self._con.execute(f"delete from records where kind in ({marks})",
                                         TEXT_KINDS).rowcount  # fmt: skip
            if now >= _add_years(end, policy.consent_years):
                consent = self._con.execute("delete from records where kind = 'consent'").rowcount
            # each event is kept until its own anniversary (29 February -> 1 March)
            expired = [
                key for key, at_text in self._con.execute(
                    "select key, at from records where kind = 'audit'")
                if _add_years(dt.datetime.fromisoformat(at_text), policy.audit_years) <= now
            ]  # fmt: skip
            for key in expired:
                self._con.execute("delete from records where kind = 'audit' and key = ?", (key,))
            audit = len(expired)
            # checked before commit, so a failed cross-check rolls the purge back
            overdue = sum(
                1 for d in self.deletions()
                if not d.complete
                and now - d.requested_at > dt.timedelta(days=policy.deletion_days)
            )  # fmt: skip
            self._audit("system-1", "purge", None, now)
        cleared = self._checkpoint()
        return PurgeReport(text, consent, audit, overdue, cleared)
