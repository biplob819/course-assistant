# N1.5 — App store, audit and pause

**Wave 1. Depends on:** N1.1. **Unblocks:** A2.5, B2.1, B2.2.

## Goal
One small repository layer over SQLite holds every learner-facing record (conversations, questions, answers, escalations, consent, deletion requests, pause state and audit events) so that deletion, retention and access logging are enforced in one place, not spread across the app.

## Outputs (`src/course_assistant/store/`)
- `db.py`: `Store(path)` opens SQLite with WAL, foreign keys on, and versioned migrations (`schema_migrations` table). Records are stored as validated JSON of the N1.1 models plus indexed key columns (pid, conversation id, question id, created_at). Reads validate back into models (DB-01).
- Repositories: `conversations`, `questions`, `answers`, `escalations`, `consents`, `deletions`, `pause`, `audit`, each with typed put, get and list methods. A learner's records are listed only by their own pid (DB-01).
- `audit.record(actor, action, target)` is called by every teaching-team action and every learner deletion. It holds no text, because `AuditEvent` cannot (DB-02).
- `pause.is_paused(session)`: `all` or the session. Any read error returns paused (fail closed). Setting a pause writes an audit event (DB-03).
- `retention.purge(now, policy)` deletes conversation text after the pilot end plus 30 days, audit events after 1 year, and consent records after the pilot end plus 1 year. It completes pending deletion requests in the app store and reports any older than 7 days. `delete_history(pid, conversation_id=None)` removes questions, answers, escalations and conversations for that pid (or one conversation) in one transaction and records the `DeletionRequest` (DB-04, US9).

## Out of scope
Trace deletion (B2.2 drives the trace sink), the pilot database (agreed at sign-off), and the UI.
