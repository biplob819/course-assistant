# N1.5 acceptance criteria

**DB-01** Every record type round-trips through the store as its N1.1 model; migrations are versioned and idempotent; a learner's history query returns only that pid's records.
**DB-02** Each teaching-team action and learner deletion writes exactly one audit event; audit rows contain no free text.
**DB-03** Pause state per session and for all is stored with who set it; `is_paused` fails closed on any read error; setting it is audited.
**DB-04** `delete_history` removes every row for the pid (or one conversation) atomically and records the request; `purge` applies each retention period exactly and flags deletion requests pending more than 7 days.
