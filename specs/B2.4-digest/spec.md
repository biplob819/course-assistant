# B2.4 — Weekly digest

**Wave 2. Depends on:** B2.1, B2.2 (and N1.5's store, N1.2's `respond`). **Unblocks:** B2.3.

## Goal
Each week the teaching team sees the most common confusions by session: topic labels with
counts and no learner identifiers (US6, FR9). Only learners who consented to
`improve_course` are counted (finding H8); a topic with fewer than k = 5 distinct learners is
dropped (finding H7); labels are written by Luna from the questions and checked so they can
never quote a question or carry an id (B8, finding C6). A flagged material gap appears in the
digest as a count on its topic row (PRD Flow B step 3, B2.1's `material_gap`).

## Outputs

### `src/course_assistant/digest/` (`weekly.py`, `_errors.py`)

#### Inputs and the purpose filter (DIG-01)
- `weekly_digest(week, *, store, responder, config, sessions, k=None, at=None,
  write=True) -> tuple[DigestRow, ...]`. `week` is an ISO week (`2026-W48`); its window is
  Monday 00:00 UTC to the next Monday 00:00 UTC. `sessions` is the course's session ids (the
  corpus sessions). `responder` is N1.2's `respond` with client, prices, ledger and sink bound
  (A2.1 pattern).
- Questions are read from the app store by the time they were asked (N1.5
  `Store.questions_between`, added here). A question counts only when:
  1. its learner may be processed for `improve_course` now (B2.2 `privacy.allowed`: an open
     consent to the current notice version naming the purpose; anything else, including a
     read failure, is no). Withdrawal stops counting at once; a learner who never consented
     is never counted. The check runs **before** any text is sent anywhere, so a
     non-consenting learner's text never reaches the model;
  2. it is a course question: its stored answer is `answered`, `hint_only`, or `not_covered`
     with the fixed `NOT_COVERED` text. Off-topic, injection, handed-over (admin and personal),
     error and unanswered turns are left out (personal matters stay in the queue only);
  3. it still exists: deleted questions (B2.2 delete, whole history or one conversation) and
     purged ones are gone from the store, so a run after a deletion never counts them.
- Session: the question's session scope; else the session its answer cites most (first on a
  tie); else the model chooses one of `sessions` for it.

#### Topic labels on Luna (DIG-01)
- `TopicStep`: one structured-output call per batch on the check model (Luna), purpose
  `digest`, no file search, `TOPIC_RULES` and `JSON_SCHEMA` hashed into `version()` (L-11).
  Batches are per session (questions with no session form their own batches), at most
  `MAX_BATCH` (100) questions and `MAX_BATCH_CHARS` (60,000) characters each.
- The batch travels as one JSON document inside `<data>` delimiters with angle brackets
  escaped (B10): the fixed session or null, the allowed sessions, and the questions as
  `{"n": i, "text": ...}`. No pid, question id, conversation id or time is ever sent.
- The reply must be `{"labels": [{"n", "session", "topic"}]}` with each `n` exactly once and
  `session` one of `sessions` (duplicate JSON keys refused, A2.4 pass 1). A reply that will not
  parse, or a refusal, is retried once; still unreadable, or any `LLMError`, fails the whole
  run (`DigestError("failed")`) and nothing is written: no partial digest.
- Rows group by session and normalised label (the leak scan's word normalisation, joined by
  single spaces); the shown label is the group's most common original form (ties: the first
  in sort order).

#### k = 5 suppression (DIG-02)
- `count` is the number of **distinct learners** in a row, not questions. A row with
  `count < k` is dropped. `k` defaults to `config.digest_k`; any k below `K_MIN` (5) is
  refused (`DigestError("invalid")`).
- `DigestRow.gaps` (added here, default 0, never above `count`): the distinct counted
  learners in the row whose question in the week has an escalation with status
  `material_gap`. It is shown only on rows that pass k.
- `Store.put_digest(week, rows, k=..., at=...)` (added here) replaces the week's rows in one
  transaction and refuses any row below k or of another week; `Store.digest(week)` reads them.
  Rows are counts only and are kept for the pilot length (§7), not removed by a learner's
  delete; a re-run after a deletion recounts without the deleted questions.
- Rows are ordered by session, then count (highest first), then label.

#### Leak test (DIG-03)
- `label_problem(label, texts) -> str | None` refuses a label that: is blank or longer than
  `MAX_LABEL_WORDS` (10) words or `MAX_LABEL_CHARS` (80) characters; shares a run of 8 words
  with any question of the week (all stored questions of the window, consenting or not) or
  with `TOPIC_RULES`, compared after the leak scan's normalisation (look-alikes, case,
  accents); or that the A2.5 leak scan flags (canary, pid pattern, rules text); or that holds
  a question or conversation id, an email address or a 16-hex id.
- A refused label is never shown: its questions are not counted (fail closed).

## Rules
- The digest never holds question text, a pid, a question or conversation id or a time
  finer than the week: a `DigestRow` is week, session, label, count, gaps.
- Traces: `respond` writes its allowlisted record (purpose `digest`, no text, D-14).
- Errors are one public `DigestError` with fixed text (`invalid`, `failed`) raised outside any
  except block (L-30), so question text never reaches a caller through a message or context.
- No network call in tests (fakes through the real `respond`); `golden/heldout/` is never
  read. The `make digest` target (a paid call) is wired with the teaching-team view (B2.3).
