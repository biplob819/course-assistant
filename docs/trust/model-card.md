# Model card: course-assistant

> Release numbers come from heldout-4, scored once at the D-87 setting (OWNER D-90). Public-half
> numbers are tuned on, so they are not a release result.

## Models used and why

| Role | Model | Why | Source |
| --- | --- | --- | --- |
| Answers (with file search) | gpt-6.1-terra, `low` effort | The judge is gpt-6.1-sol, so the answer model must differ; `medium` was no better and slower | OWNER D-80, D-81 |
| Routing, support check (L2), integrity check, hints | gpt-6-luna | Cheaper, fast checks; run in parallel with the answer call | D-1, D-8, D-9, D-61, Tech Spec §5 |
| Offline judge | gpt-6.1-sol alone (single judge) | The second vendor is unfunded; the dual judges (D-12) return with a fresh calibration | OWNER D-80 |
| Offline extractor | gpt-6-luna | Replaces the unfunded Claude extractor | OWNER D-80 |

## Chosen setting

gpt-6.1-terra at `low` answers, gpt-6-luna checks and hints, grounding bar 0.4 tuned on the
public half only, hint cache **off** (D-70). Rules hash `bbddb60809a20b3e`: route rules
`route-c63e31a87740`, hint rules `hint-dadb39174cbe`, the D-85 handover text and three probe
samples. Frozen on the public half as `runs/e27/d87-frozen.yaml` (D-87).

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

One judge, gpt-6.1-sol (OWNER D-80); calibration against model-panel labels (OWNER D-79).
heldout-3 was scored at the earlier D-81 setting and is retired (D-83, D-88); the public half
grew from 214 to 240 cases in between (D-84), so the columns are not directly comparable.

| Metric (target) | Public half, 240, D-87 | heldout-3, D-81 setting (D-83) | Release value, heldout-4 |
| --- | --- | --- | --- |
| Grounded answer rate (≥ 0.90, lower bound ≥ 0.85) | 0.973 | 0.893, fail | 0.880 (66/75, lower bound 0.80), **fail** |
| Citation validity (≥ 0.95) | 1.000 | 1.000 | 1.000 |
| Locator correct (≥ 0.95) | 0.973 | 0.973 | 0.964 |
| Correct handover (≥ 0.95) | 1.000 | 1.000 | 1.000 (30) |
| Integrity (zero tolerance) | 1.000 | 56/56 | 56/56 |
| Injection (zero tolerance) | 1.000 | 44/44 | 44/44 |
| Leak (zero tolerance) | 1.000 | 49/49 | 49/49 |
| Identity (zero tolerance; monitored, not certified, D-86) | 1.000 | 3/5, fail | 5/5 (worst of three samples, D-85) |
| Latency p95 (≤ 11 s, OWNER D-65) | 13.6 s, fail (provider latency, D-87) | 9.2 s | 11.03 s, **fail** by 27 ms |
| Cost per question | not yet reported | $0.0058 | $0.0061 |

Per-rule results and the public case ids behind them: [`behavioral-spec.md`](behavioral-spec.md).
The release gate on heldout-4 exits 1 on grounded and latency; the owner shipped v0.1.0 with both
as documented limits and the targets unchanged (OWNER D-90).

Reports: `docs/measurements/e2.5-heldout4-report.md`, `docs/measurements/e2.5-heldout3-report.md`,
`docs/measurements/e2.5-report.md`.

## Known limits

See [`known-limits.md`](known-limits.md). In short: one small English sample course
(`DATA_CARD.md`, Known gaps); one judge, calibrated against an OpenAI-only model panel rather than a person (OWNER D-79, D-80); identity monitored, not certified (OWNER D-86); runtime support check agrees
weakly with the judges (kappa 0.15, Tech Spec §6.4); the deterministic leak scan has a stated
scope (D-51); provider model drift is caught only by the weekly gate (Tech Spec §6.5, §12).
