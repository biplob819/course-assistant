# run state

**Now:** spec

## History
- 2026-11-15 — spec and criteria written.
- 2026-11-15 — node_start GO. Tests first (tests/answer/test_route.py, 36 tests), then route.py and answer/__init__.py. Fixtures per D-36 (118 course, 68 graded_work, 13 injection, 10 admin, 10 personal, 10 off_topic). RTE-05 criterion added (the catalog had it, criteria.md did not). Live run on the public split pending: no API key in this environment; expected cost under $1 on Luna.
- 2026-11-15 — verifier pass 1: RTE-01, 02, 03, 05 CONFIRMED; RTE-04 NOT MET only for the live per-category accuracy (replay, fixtures, retry and fallback confirmed). Should-fix applied: question must be a string of at most 4000 characters; window must be (learner, assistant) string pairs, trimmed from the oldest to window_tokens*4 characters; checks run before any call; the rules now say pretending to be staff is identity (off_topic), not injection. A2.1 stays building until the live run (needs OPENAI_API_KEY; under $1).
