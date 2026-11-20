# Evaluation methodology

> Release numbers come from heldout-4, scored once at the D-87 setting (OWNER D-90; `docs/measurements/e2.5-heldout4-report.md`).

## Golden set

- 280 cases across rules B1 to B10, plus 150 adversarial paraphrases of the B3 and B10 probes
  (OWNER D-19, D-31). Composition and expected statuses: Tech Spec §6.1; `DATA_CARD.md`.
- Written before any prompt tuning, so the system it measures did not shape it (Tech Spec §6.2).
- Each B1 case names its gold units; a second reader (fresh verifier) checked every case
  (`specs/E2.1-golden-set/RUN-STATE.md`).
- Planted cases: 10 absent-but-common-knowledge, 10 multi-turn decompositions, 5 window
  injections (Tech Spec §5.6).
- A public B9 supplement of 26 identity cases, written by an isolated author, on the tuning
  side only (D-84). Public half now 240, held-out 215 (`DATA_CARD.md`).

## Held-out discipline

- 50/50 split, stratified by rule and session, fixed seed. Public half 214, held-out 215 after
  one retirement (D-54; `docs/measurements/e2.5-report.md`); the D-84 supplement later grew the
  public half to 240.
- Held-out text lives only in git-ignored `golden/heldout/`; the repo commits a manifest of ids
  and hashes (Tech Spec §6.2).
- Exposure scan fails the build if a held-out question appears in a tracked file
  (`tests/golden/test_golden.py::test_no_heldout_question_appears_in_any_tracked_file`).
- Tuning (bar, support rules, any D-71 change) uses the public half only; tools refuse held-out
  subsets (`tests/eval/test_quality_d71.py`).
- Each held-out set is scored once, then retired, and a fresh set with the same composition is
  scored once at the next frozen setting (OWNER D-67, OWNER D-71;
  `tests/golden/test_heldout_refresh.py`). heldout-1 to heldout-3 are scored and retired;
  heldout-4 was scored once at the D-87 setting (OWNER D-90); a first attempt lost to a
  provider outage was voided and rerun on a fresh cache (OWNER D-89).

## Judges

The design is two judges from two vendors, Claude Opus 5.5 and GPT Sol, and a case passes only
if both agree (D-12). Since OWNER D-80 one judge grades, gpt-6.1-sol alone, while the second
vendor is unfunded; gpt-6.1-terra writes the answers, so the judge never grades its own. The gate
accepts a single-judge run only while `SINGLE_JUDGE_DECISION` names D-80, and notes it on every
verdict (`tests/eval/test_release.py::test_a_single_judge_run_passes_only_under_the_owner_decision`;
`tests/eval/test_course_ta.py::test_a_single_judge_panel_needs_exactly_one_and_reads_one_verdict`).
Lost meanwhile: the cross-family check, self-preference measurement and EvalKit's published
headline. Integrity, identity and leak verdicts are the worst of three samples (D-85).

## Calibration

336 groundedness claims and 100 integrity conversations, labelled blind by a model panel, not
by a person (OWNER D-76; `docs/labelling.md`). Since OWNER D-79 the panel is OpenAI only:
gpt-6.1-terra and gpt-6-luna label, gpt-6.1-terra at high effort breaks a split, and a label
needs two agreeing votes. gpt-6.1-sol's held-out kappa on those labels: 0.88 [0.75, 0.97] on
groundedness and 0.79 [0.00, 1.00] on integrity (OWNER D-80). **Limitations:** kappa measures
agreement with the panel, not with a person; panel and judge share a vendor, so their errors may
lean the same way; the labellers are also the answer and check models; and the integrity
calibration measured one sample per verdict, while the judge now takes the worst of three
(D-85). Identity is monitored on the public half, not certified (OWNER D-86). "Calibrated"
means a certified calibration bound to the run and the current rubric hashes, never a summary
stamp alone (D-57). A person's labels can replace the panel's later with a new scoring.

## Release gate

`scripts/release_gate.py` reads a finished held-out run and passes only if every §6.4 gate
passes, no metric shows a separated paired regression (Holm) against the live baseline, and
judging is dual and calibrated, or single under OWNER D-80 (see Judges). Exit codes 0 pass, 1 gate failed, 2 error, 3 cost cap, 4 judging
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
(Tech Spec §6.4). Headline numbers with CIs (heldout-4): grounded 0.880, bootstrap lower bound 0.80 (target ≥ 0.85, fail); zero-tolerance failure-rate upper bounds integrity ≤ 0.054 (56 cases), injection ≤ 0.068 (44), leak ≤ 0.061 (49), identity ≤ 0.60 (5, too few to certify, D-86); EvalKit publishes no headline from a single-judge run (OWNER D-80).
