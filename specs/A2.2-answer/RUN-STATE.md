# RUN-STATE A2.2

- 2026-11-15 — spec and criteria written.
- 2026-11-15 — node_start GO. Tests first (tests/answer/test_answer.py, 23 tests), then answer.py. Hits without a score are dropped and counted (never given 0 or the bar). Unknown session refused before any call. Canary is a fixed token in ANSWER_RULES; the leak scan that uses it is A2.5.
- 2026-11-15 — verifier pass 1: ANS-01..04 all CONFIRMED. Should-fix applied: hits with non-finite or negative scores dropped and counted (N1.2 already refuses NaN at parse; this guards unvalidated results); window replies must be plain (learner, assistant) strings, refused before any call; unresolved citations counted once per file; Corpus refuses duplicate file_id; spec names AnswerDraft.
- 2026-11-15 — verifier pass 2: ANS-01..04 all CONFIRMED (corpus change does not break N1.4). One-line hardening after it: a generator window is read once into a tuple before the shape check (pass 2 should-fix; test added). GREEN. Note for A2.3: hits are not deduplicated by file_id.
