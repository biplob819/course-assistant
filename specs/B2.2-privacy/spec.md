# B2.2 — Notice, consent, history, delete

**Wave 2. Depends on:** N1.5, N1.2. **Unblocks:** B2.4, B2.5, T3.1.

## Goal
A learner sees a plain notice before first use, gives consent per purpose, can see their history and delete it (or one conversation), and deletion reaches the traces within 7 days (US9, NFR2, DPDP).

## Outputs (`src/course_assistant/privacy/`)
- `notice.py` (PRV-01): `NOTICE` text with a version string; `needs_notice(pid, store)` is true when the learner has no consent for the current notice version, so a new notice version asks again.
- `consent.py` (PRV-02): `give(pid, purposes, store)`, `withdraw(pid, store)`, `allowed(pid, purpose, store)`; withdrawal stops processing at once (`allowed` false), separate from deletion; both audited.
- `history.py` (PRV-03): `history(pid, store)` returns the learner's conversations with questions, answers and escalation replies, nothing of anyone else's.
- `delete.py` (PRV-04): `delete(pid, conversation_id=None, store, traces)` removes app rows via `Store.delete_history` and deletes the learner's trace lines from every `TraceSink` that supports deletion (`JsonlSink.delete_pid`); `Store.mark_traces_deleted` records completion; `purge` (N1.5) flags requests older than 7 days that are still incomplete.
