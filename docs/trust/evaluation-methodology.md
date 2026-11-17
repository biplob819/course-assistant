# Evaluation methodology

> **Draft (OWNER D-72).** Release numbers are `PENDING (D-71)`.

## Golden set

- 280 cases across rules B1 to B10, plus 150 adversarial paraphrases of the B3 and B10 probes
  (OWNER D-19, D-31). Composition and expected statuses: Tech Spec §6.1; `DATA_CARD.md`.
- Written before any prompt tuning, so the system it measures did not shape it (Tech Spec §6.2).
- Each B1 case names its gold units; a second reader (fresh verifier) checked every case
  (`specs/E2.1-golden-set/RUN-STATE.md`).
- Planted cases: 10 absent-but-common-knowledge, 10 multi-turn decompositions, 5 window
  injections (Tech Spec §5.6).

## Held-out discipline

- 50/50 split, stratified by rule and session, fixed seed. Public half 214, held-out 215 after
  one retirement (D-54; `docs/measurements/e2.5-report.md`).
- Held-out text lives only in git-ignored `golden/heldout/`; the repo commits a manifest of ids
  and hashes (Tech Spec §6.2).
- Exposure scan fails the build if a held-out question appears in a tracked file
  (`tests/golden/test_golden.py::test_no_heldout_question_appears_in_any_tracked_file`).
- Tuning (bar, support rules, any D-71 change) uses the public half only; tools refuse held-out
  subsets (`tests/eval/test_quality_d71.py`).
- The held-out set is scored once; the first set was scored on 17 Nov and is retired as a set;
  a fresh set is written and scored once at the frozen setting (OWNER D-67, OWNER D-71;
  `tests/golden/test_heldout_refresh.py`).

## Two-vendor judges

Claude Opus 5.5 and GPT Sol judge groundedness, integrity, injection, leak and identity; a case
passes only if both agree (D-12; `README.md`, Evaluation). The runtime support check (Luna) is
graded against the dual verdict each run with kappa and a CI (Tech Spec §5.8).

## Calibration

100 groundedness and 100 integrity items, labelled by the owner, plus a blind relabel of 20 + 20
(OWNER D-66; `docs/labelling.md`). "Calibrated" means a certified calibration bound to the run
and the current rubric hashes, never a summary stamp alone (D-57). Every report carries
`uncalibrated` until then. Status: `PENDING (D-71)`.

## Release gate

`scripts/release_gate.py` reads a finished held-out run and passes only if every §6.4 gate
passes, no metric shows a separated paired regression (Holm) against the live baseline, and
judging is dual and calibrated. Exit codes 0 pass, 1 gate failed, 2 error, 3 cost cap, 4 judging
pending (Tech Spec §6.5; D-57, D-59; `tests/eval/test_release.py`). CI runs it on manifest or
rules changes and weekly; the live job never runs for pull requests (D-59).

## Regression

The baseline is the per-case gate values saved when the live pointer flips; comparisons are
paired bootstrap with Holm (D-57; `tests/eval/test_release.py::test_regression_against_the_live_baseline_is_paired_with_holm`).
Example of a non-separated comparison: the chosen setting against the bar-0.3 tuning run
(`docs/measurements/e2.5-report.md`).

## Tuning log

`docs/measurements/tuning-log.jsonl` is an append-only hash chain; CI checks it continues from
the base commit, and calibration is certified only through it (D-55, OWNER D-68;
`tests/eval/test_calibration.py::test_append_only_check_compares_with_the_ci_base_and_fails_closed_in_ci`).

## Keeping the set relevant

Verifier findings, flagged wrong answers and failed live cases become draft public cases;
course changes retire affected cases (drift); a fresh held-out set each term (D-69; Tech Spec §6.3).

## Statistics

Bootstrap CIs over cases; zero-tolerance gates reported with their rule-of-three bound
(Tech Spec §6.4). Headline numbers with CIs: `PENDING (D-71)`.
