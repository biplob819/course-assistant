# RUN-STATE A2.2

- 2026-11-15 — spec and criteria written.
- 2026-11-15 — node_start GO. Tests first (tests/answer/test_answer.py, 23 tests), then answer.py. Hits without a score are dropped and counted (never given 0 or the bar). Unknown session refused before any call. Canary is a fixed token in ANSWER_RULES; the leak scan that uses it is A2.5.
