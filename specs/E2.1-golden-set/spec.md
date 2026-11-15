# E2.1 — Golden set

**Wave 2. Depends on:** N1.1, N1.3. **Unblocks:** E2.2.

## Goal
The 280-case golden set (Tech Spec §6.1) plus the 150-case adversarial extension (OWNER D-19, sized by D-31), written against the sample course before any prompt is tuned (finding H13), so the assistant is measured on cases it was not shaped to pass.

## Outputs
- `golden/public/cases.jsonl` (tuning split, committed) and `golden/heldout/cases.jsonl` (held-out split, git-ignored), one `GoldenCase` per line with `schema_version`.
- `golden/heldout/MANIFEST.json` (committed): held-out case ids, the sha256 of each case line, the corpus version they were written for, and counts by rule. Anyone can check the private file has not changed without seeing it (EvalKit D-48 pattern) (GLD-02).
- `golden/README.md`: composition, splits, how a case is checked, and that held-out cases stay private.
- `scripts/golden_tool.py`: `check` validates every case and the coverage rule; `split` assigns the stratified 50/50 split with a fixed seed; `manifest` writes the held-out manifest.

## Composition (§6.1, OWNER D-6, D-19, D-31)
| Rule | Cases | Notes |
| --- | --- | --- |
| B1 course | 150 | 50 per session; 30 of them follow-up turns with `prior_turns`; each names `gold_units` as locators |
| B2/B7 absent | 40 | from `absent_topics.yaml`; 10 planted common-knowledge questions (B7) |
| B3 graded work | 30 | at least 3 per assessment; 10 multi-turn decompositions |
| B4 admin/personal | 20 | fees, deadlines, extensions, grades, wellbeing |
| B8 other learners | 10 | each with a canary |
| B9 identity | 10 | |
| B10 injection | 20 | each with a canary; 5 planted in an earlier turn |
| Extension (D-19, D-31) | 150 | 3 paraphrases of each B3 (90) and B10 (60) probe, written by Claude (outside the Sol/Luna comparison), all held out |

## Rules
- `gold_units` are locators `sNN:kind:N` (D-30): `slides:N` is slide N, `pre_read:N` is page N, `agenda:1` the agenda. Every locator must exist in the sample course, and the cited unit must actually contain the answer (checked by a second reader).
- B1 questions are answerable from the cited units alone; B2/B7 questions are not answerable from any unit (absent-topic keywords).
- Coverage rule (C7): at least 15 B1 cases per session; at least 3 B3 probes per assessment.
- Split (D-32, D-34, D-35): by merged groups, seed 20261115, half the groups per stratum to each side; each paraphrase goes with its source. Counts are approximate (currently 194 public, 236 held out).
- Second reader (GLD-03): a fresh verifier checks every B1 locator and every expected status; the owner spot-checks 20% (recorded as a task, not a gate).
