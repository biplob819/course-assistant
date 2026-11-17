# Model card: course-assistant

> **Draft (OWNER D-72).** Nothing here is green. Numbers that depend on the final run are
> marked `PENDING (D-71)`. The first-run numbers below are a recorded result, not the release
> result (OWNER D-71).

## Models used and why

| Role | Model | Why | Source |
| --- | --- | --- | --- |
| Answers (with file search) | GPT Sol, `medium` effort | Higher answer rate than Luna: 51 vs 30 of 74 public B1 questions at status level; `low` gave nothing over `medium` | D-61, OWNER D-65, `README.md` (Configuration) |
| Routing, support check (L2), integrity check | GPT Luna | Cheaper, fast checks; run in parallel with the answer call | D-1, D-8, D-9, Tech Spec §5 |
| Hints on graded work | GPT Luna (`hint_model`) | Closes the latency gap on hint turns | D-61, `src/course_assistant/schemas/config.py` |
| Offline judges | Claude Opus 5.5 and GPT Sol (two vendors) | A case passes only if both agree; guards against self-preference | D-12, Tech Spec §12 |

## Chosen setting

Sol `medium` answers, Luna checks and hints, `early_finalize` on, grounding bar 0.4 tuned on the
public half only, hint cache **off** until the integrity labels clear it.
Sources: D-61, OWNER D-65, OWNER D-67, D-70, `docs/measurements/e2.5-report.md` (Notes).
The final frozen setting is `PENDING (D-71)`.

## Intended use

A course-scoped teaching assistant that answers learner questions only from the course's own
materials, with a citation to the slide or page, and hands everything else to the teaching
team (`README.md`, How it works; Tech Spec §2.2). Statuses: `answered`, `not_covered`,
`hint_only`, `handed_over`.

## Out-of-scope uses

- General knowledge questions outside the course: returned as `not_covered` (rule B7; Tech Spec §5.6).
- Completing or drafting graded work: `hint_only` at most (D-8; `tests/answer/test_integrity.py`).
- Admin and personal matters: `handed_over` (rule B4; Tech Spec §6.1).
- Questions about other learners: `not_covered` (B8).
- Speaking as a person or for the institution (B9; `README.md`, Privacy and safety).
- Grading, assessment decisions, or any use without a teaching team behind the queue (B2.1 escalation, `specs/B2.1-escalation/`).
- Courses other than the measured one without their own golden set: the coverage rule refuses go-live (Tech Spec §6.3; `tests/eval/test_release.py::test_coverage_rule_blocks_go_live`).

## Measured results and their status

First full run, 17 Nov 2026, dual judges, **uncalibrated** (OWNER D-66). Source:
`docs/measurements/e2.5-report.md`, Tech Spec §6.4.

| Metric (target) | First run, held-out (215) | Status | Release value |
| --- | --- | --- | --- |
| Grounded answer rate (≥ 0.90, lower bound ≥ 0.85) | 0.693 | fail | `PENDING (D-71)` |
| Citation validity (≥ 0.95) | 1.000 | pass | `PENDING (D-71)` |
| Locator correct (≥ 0.95) | 0.800 | fail | `PENDING (D-71)` |
| Correct handover (≥ 0.95) | 1.000 | pass | `PENDING (D-71)` |
| Integrity (zero tolerance) | 52/56 | fail | `PENDING (D-71)` |
| Injection (zero tolerance) | 1.000 | pass | `PENDING (D-71)` |
| Leak (zero tolerance) | 1.000 | pass | `PENDING (D-71)` |
| Identity (zero tolerance) | 1.000 | pass | `PENDING (D-71)` |
| Latency p95 (≤ 11 s, OWNER D-65) | 9.1 s | pass | `PENDING (D-71)` |
| Support check vs judges, kappa (monitor, ≥ 0.6) | 0.15 [−0.01, 0.33], both halves | below monitor | `PENDING (D-71)` |
| Calibration | uncalibrated | fail | `PENDING (D-71)` |
| Cost per question | $0.0062 | within $150 cap (OWNER D-64) | `PENDING (D-71)` |

Release gate on the first run's held-out records: exit 1 (`docs/measurements/e2.5-report.md`, Notes).

## Known limits

See [`known-limits.md`](known-limits.md). In short: one small English sample course
(`DATA_CARD.md`, Known gaps); judging uncalibrated (OWNER D-66); runtime support check agrees
weakly with the judges (kappa 0.15, Tech Spec §6.4); the deterministic leak scan has a stated
scope (D-51); provider model drift is caught only by the weekly gate (Tech Spec §6.5, §12).
