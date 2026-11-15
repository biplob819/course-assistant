"""Delete a learner's history (PRV-04): app rows at once through `Store.delete_history`, then
the learner's trace lines in every sink that can delete them. Completion is recorded only when
every sink deleted; otherwise the request stays open and `Store.purge` counts it as overdue
after 7 days, so a missed trace deletion fails loudly (Tech Spec §7, US9)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from course_assistant.llm.trace import TraceSink
from course_assistant.privacy._errors import boundary, check_cid, check_pid, now_or
from course_assistant.schemas import DeletionRequest
from course_assistant.store import Store


def _delete_traces(pid: str, pending: Sequence[DeletionRequest],
                   traces: Sequence[TraceSink]) -> bool:  # fmt: skip
    """Delete the lines every open request of this learner covers: all of them if any request
    is for the whole history, else those of the named conversations. True only if every sink
    supports deletion and none failed."""
    whole = any(d.conversation_id is None for d in pending)
    cids = None if whole else {d.conversation_id for d in pending if d.conversation_id}
    known = {c for d in pending for c in d.covers}
    done = True
    for sink in traces:
        remove = getattr(sink, "delete_pid", None)
        if not callable(remove):
            done = False
            continue
        try:
            remove(pid, cids, known)
        except Exception:
            done = False
    return done


def _pending(store: Store, pid: str) -> list[DeletionRequest]:
    return [d for d in store.deletions() if d.learner_pid == pid and d.traces_deleted_at is None]


@boundary
def delete(
    pid: str,
    conversation_id: str | None = None,
    *,
    store: Store,
    traces: Sequence[TraceSink],
    at: dt.datetime | None = None,
) -> DeletionRequest:
    """Delete the learner's whole history, or one conversation. `traces` must be given (an
    empty list states there is no trace store). Returns the request, with `traces_deleted_at`
    set when the traces are gone too."""
    check_pid(pid)
    if conversation_id is not None:
        check_cid(conversation_id)
    when = now_or(at)
    request = store.delete_history(pid, conversation_id, at=when)
    pending = _pending(store, pid)
    if _delete_traces(pid, pending, traces):
        store.mark_traces_deleted(pid, at=when, upto=max(d.requested_at for d in pending))
        return request.model_copy(update={"traces_deleted_at": when})
    return request


@boundary
def retry_trace_deletion(store: Store, traces: Sequence[TraceSink], *,
                         at: dt.datetime | None = None) -> int:  # fmt: skip
    """Retry trace deletion for every open request (run with purge); returns how many
    learners' requests it completed."""
    when = now_or(at)
    pids = sorted({d.learner_pid for d in store.deletions() if d.traces_deleted_at is None})
    completed = 0
    for pid in pids:
        pending = _pending(store, pid)
        if pending and _delete_traces(pid, pending, traces):
            store.mark_traces_deleted(pid, at=when, upto=max(d.requested_at for d in pending))
            completed += 1
    return completed
