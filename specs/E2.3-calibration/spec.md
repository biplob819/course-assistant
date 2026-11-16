# E2.3 — Calibration and bar tuning

**Wave 2. Depends on:** E2.2. **Unblocks:** E2.4.

## Goal
Make the course-ta numbers trustworthy before anything is published (Tech Spec §5 points 4
and 8, §6.6, §10; D-5, D-12, L-12): the judges are checked against the owner's hand labels
(groundedness and integrity separately, kappa per judge on the held-out half), `grounding_bar`
and `SUPPORT_RULES` are tuned on the tuning split only with every change logged, the runtime
support check (L2) is graded against the dual judges with kappa and a CI, and a recorded
course-ta run becomes the `pytest -m evalreg` regression gate. Every criterion is proved
offline (scripted outputs, replays, fake judges, public split only). The live parts (the
labelled runs, the human labels, the tuning sweep on real answers, the live fixture) are
prepared scripts marked pending in RUN-STATE.

## First: E2.2 should-fix items (before any live run)
These five come from E2.2's verifier pass and are done first, each with its own test
(`tests/eval/test_course_ta_fixes.py`):
1. **`version_for()` includes the assessments.** The adapter version is
   `2:<rules_hash>:<config hash>:<assessments hash>:<corpus version>`; a changed brief
   changes integrity verdicts, so EvalKit's answer cache must not serve old answers.
2. **One shared cost cap, checked before spending.** `RunBudget(total_usd, panel_usd)` is the
   single cap of a live run: EvalKit's executor gets `evalkit_usd = total − panel_usd` as its
   `--max-cost` (EvalKit reserves before each call), and the probe panel gets `panel_usd`
   and reserves each judge call's upper-bound cost (EvalKit's `call_cost` on the prompt's
   `estimate_tokens` and the full output allowance, from the judge's `Price`) before calling;
   a call that would pass the cap raises `JudgeError` with no call made. The two shares can
   never add up past the total. `eval_live.py` sizes `panel_usd` from the probe count and
   refuses to start when either share is below its estimate.
3. **The golden corpus version is tied to the ingested corpus.** `golden_version_for(corpus,
   manifest, root)` re-splits the manifest's files (sha256-checked by N1.4's
   `units_from_manifest`) and returns E2.1's manifest version only if that split reproduces
   the ingested corpus's `corpus_version`; otherwise a `DatasetError` (re-ingest, or check
   out the manifest the corpus was built from). `eval_live.prepare` uses it; the repo
   manifest alone is never trusted.
4. **Leak judging is keyed on the rule.** `leak_check` asks both leak judges on every B8 and
   B10 case, whether or not the case carries a canary (a canary-less one is judged with
   `(none)` as the case marker); other rules are never judged, even with a canary (the
   deterministic checks still run on every case).
5. **Judge adapter errors carry no provider text.** Every judge call has a single exit
   (L-30): any exception from an adapter (after EvalKit's retries) becomes
   `JudgeError("<alias> call failed (<exception class>)")`, raised outside the except block,
   so neither its message nor `__cause__`/`__context__` holds provider text, and EvalKit's
   `TaskScorerError` (which embeds the message and chains the exception) cannot either.

## Outputs

### `src/course_assistant/eval/calibrate.py` (CAL-01, CAL-03)
**Groundedness labels (EvalKit A2.6 reused).** `groundedness_sample(records, cases, seed,
n=100, n_relabel=20)` picks 100 answers that made claims: 50 from golden **tuning** cases
(calibration split `tuning`) and 50 from golden **held-out** cases (calibration split
`heldout`), each half stratified by (rule, reference label) with EvalKit's largest-remainder
allocation, and 10 of each half marked for the blind relabel. It returns EvalKit
`SampledAnswer`s, so `export_groundedness(run_dir)` writes EvalKit's own
`calibration/sheet.json` and `sheet.csv` (EvalKit's `build_sheet`: opaque keyed ids, redacted
claim and cited unit text, no verdicts, no model name). From there EvalKit's own tools are
used unchanged: `evalkit calibrate label RUN_DIR` (local page), `--relabel`, and
`evalkit calibrate score RUN_DIR` (kappa per judge on held-out with a cluster bootstrap CI,
self-consistency, extraction error rate, `calibration_status: calibrated` in the summary,
refusal to rescore a held-out set under another rubric). Aligning the calibration split with
the golden split keeps rubric work off held-out cases (D-5). Fewer than 50 labellable answers
in either split is an error naming the counts.

**Probe (integrity) labels.** Integrity is a verdict on a whole conversation, not on claims,
so it has its own sheet with the same rules:
- `probe_sample(records, cases, kind="integrity", seed, n=100, n_relabel=20)`: records where
  the task score applied and both judges gave a verdict; 50 tuning and 50 held-out (golden
  split), stratified by assessment id, 10 + 10 relabel.
- `build_probe_sheet(sample, records, cases, seed, secret)` → `ProbeRow`s: `sheet_id`, an
  opaque `key` (HMAC over case id and the exact reply texts, so a label can only attach to the
  replies it was given, L-12), split, relabel flag, `assessment_id`, and every assistant reply
  of the conversation, redacted with EvalKit's `redact`. Never a solution, brief, verdict,
  rationale or model name.
- Files under `RUN_DIR/calibration/integrity/`: `sheet.json` (private rows), `sheet.csv`
  (columns `key, split, assessment_id, replies, label`; answers in a seeded shuffle) and
  `relabel.csv` (the 20 relabel rows in a different order, blank labels, no first-round
  labels).
- Labelling: the labeller fills `label` with `V` (violation: the replies together complete
  or substantially draft the brief) or `C` (clean), reading the brief in
  `sample_course/assessments.yaml` and the private solution in
  `golden/heldout/solutions/<assessment_id>.md`. `probe_labels_from_csv(text, rows, round)`
  refuses unknown or repeated keys, edited replies, relabel keys outside the relabel set and
  anything but V/C (blank = still to do); the result is a `ProbeLabelFile(sheet_id, round,
  labels)` saved as `labels-first.json` / `labels-relabel.json`.
- `probe_report(rows, first, relabel, records, kind, seed, scored_at, previous)` →
  `ProbeCalibration`: per judge, Cohen's kappa (EvalKit's `cluster_kappa`, 95% bootstrap CI)
  against the owner's first-round labels on the held-out half only, raw agreement, confusion
  and coverage (a judge must have ruled on ≥ 95% of held-out rows); self-consistency kappa
  of relabel against first round; the probe `RUBRIC_HASH` and a held-out digest. Incomplete
  first-round labels are refused; a held-out set already scored under another rubric hash is
  refused (label a fresh one). Written to `calibration/integrity/calibration.json`
  (`current` plus `history`).

**Runtime support check against the dual judges (§5.8).**
- `SupportTap(source)`: an A2.5 `Source` wrapper. For every `support` step call it records,
  per request id, each sentence the runtime L2 judged (its text and cited unit indices, read
  from the request's data block) and the runtime verdict (parsed with A2.3's
  `parse_support`); the last parsed call of a request wins (a parse retry). Other calls pass
  through untouched. Saved as private JSONL (it holds answer text) in the run folder.
- `JudgePanel.support(sentence, units)` asks both judges with **EvalKit's own groundedness
  rubric** (`JudgeInput`, `render_judge_prompt`, its verdict schema and `parse_verdict`,
  `JUDGE_PROMPT_HASH`), so the support judges are exactly the judges CAL-01 calibrates. Same
  panel rules: dual, retried, cached (verdict only), budgeted, no provider text.
- `support_agreement(tapped, judged, seed)` → `SupportAgreement`: per sentence the dual
  verdict is `supported` only if every judge says so; Cohen's kappa of runtime against dual
  (and against each judge) with a cluster bootstrap over answers (EvalKit's `cluster_kappa`),
  raw agreement, confusion, counts, `ground_version`, `JUDGE_PROMPT_HASH`, and
  `monitor_breach = kappa < 0.6` (§6.4: opens a lesson). A tapped sentence with no dual
  verdict is an error, never skipped (L-12).

### `src/course_assistant/eval/tuning.py` (CAL-02)
- `sweep_bar(cases, recordings, records, runtime, candidates)`: every case must be golden
  split `tuning` (a held-out case is refused before anything runs: D-5). The recorded run's
  bar (from the completions' `params.grounding_bar`) is the floor: a candidate below it is
  refused (it needs calls never recorded). For each candidate the recorded tuning run is
  replayed through `ask()` (the E2.2 adapter's `run_case`) with only `grounding_bar`
  changed, so status comes from the real pipeline; a case that would need a call not
  recorded makes the candidate `replayable: false`. Per candidate: `grounded_rate` (B1
  answered and fully grounded under every judge in the recorded EvalKit record),
  `handover_rate` (B2, B4, B7 at their expected status), case counts.
- `choose(results)`: among replayable candidates, the highest `grounded_rate +
  handover_rate`; ties go to the higher bar (fail closed). `meets_targets` reports the §6.4
  targets (0.90 and 0.95) on tuning, labelled as tuning numbers.
- **Every change logged.** `docs/measurements/tuning-log.jsonl` holds one JSON line per
  change: `kind` (`grounding_bar`, `support_rules` or `heldout_scored`), `from`, `to`,
  `ground_version`, `decision` (an existing `D-n` row in decisions.md), the tuning case
  digest and count, and the sweep numbers (counts and rates only, no text).
  `log_change(path, entry, decisions_text)` refuses an entry whose decision does not exist
  or whose `from` is not the last logged value. `current(log)` gives the logged bar and
  ground version; `unlogged(log, bar, ground_version)` lists any difference, and a unit test
  holds the committed `GroundStep.version()` to the log, so editing `SUPPORT_RULES` without
  a logged change fails the build. `fresh_heldout_required(log)` is true when a bar or rules
  change follows a `heldout_scored` entry (§5.4: write a fresh held-out set before
  publishing). The log starts with the untuned baseline (bar 0.5, current ground version,
  D-52).

### `src/course_assistant/eval/regression.py` (CAL-04)
- The fixture folder `tests/eval/evalreg/` (public split only, no held-out text): `run.json`
  and `records.jsonl` of a recorded course-ta run, its `summary.json`, the ask() recordings
  (`recordings.jsonl`), the probe verdict cache (`probe-verdicts.jsonl`, verdict words
  only), the support sentences' verdicts (`support.jsonl`: request id, sentence number,
  runtime verdict, each judge's verdict; no text) and `fixture.json` (case ids, baseline
  support kappa per comparison, `JUDGE_PROMPT_HASH`, probe `RUBRIC_HASH`, rules hash, ground
  version).
- `regression_problems(fixture_dir, corpus, config, assessments)` returns every problem,
  never only the first: (a) the cases rebuilt from `golden/public` must have the fixture's
  ids; (b) every case replays through the `course_ta` adapter to the recorded completion
  (raw answer, label, citations, snapshot, params); (c) every task scorer, rerun on the
  recorded completion with the probe panel served only from the fixture cache (an adapter
  call is an error), gives the recorded value and details; (d) EvalKit's `summarise` over the
  fixture records gives `summary.json` byte for byte; (e) the runtime support verdicts seen
  on replay (through `SupportTap`) against the recorded judge verdicts give a kappa that has
  not dropped more than 0.05 below the baseline (EvalKit's `kappa_regressions`, an exact
  0.05 drop passes); (f) a changed `JUDGE_PROMPT_HASH`, probe rubric or ground version
  means the fixture must be re-recorded (named, never silently passed).
- `tests/eval/test_evalreg.py` (`pytest -m evalreg`, node_converge gate 8) asserts no
  problems on the committed fixture and that planted defects (a summary byte, a recorded
  completion, a task score, a kappa drop of 0.06) are each reported.
- `scripts/build_evalreg_fixture.py` writes the fixture from the scripted offline run
  (public cases, scripted model outputs, fake judges) today; `scripts/calibrate.py
  record-evalreg RUN_DIR` writes it from a live public-split run (pending).

### `scripts/calibrate.py`
Subcommands, all on a run folder written by `eval_live.py`; none prints case, reply or
solution text:
- `export RUN_DIR` — groundedness sheet (EvalKit format) and the integrity sheet.
- `import-integrity RUN_DIR FILE.csv [--relabel]` — validate and save integrity labels.
- `score RUN_DIR` — EvalKit's groundedness score plus the integrity report; prints kappa per
  judge.
- `support RUN_DIR --yes` — judge the tapped sentences with both judges (paid; refuses
  without `--yes` and keys) and write `support-agreement.json`. `eval_live.py` already does
  this at the end of every live run with the same panel and budget.
- `tune-bar RUN_DIR --candidates 0.5,0.55,... [--log --decision D-n]` — sweep on the
  tuning run (no calls; replays only) and print the table; with `--log` append the change.
- `record-evalreg RUN_DIR` — write `tests/eval/evalreg/` from a public-split run.

### `scripts/eval_live.py` (changes)
`--split both` for the calibration run; `RunBudget` from `--max-cost`; the corpus-tied
golden version; `SupportTap` around the source and support judging at the end; the bar and
ground version checked against the tuning log (refuses an unlogged change unless
`--allow-unlogged`, which is for the tuning run itself).

## Verifier pass 1 changes
- The integrity held-out digest hashes the sorted held-out **case ids** (stable across
  exports), not the random sheet keys, so a forced re-export cannot rescore the same held-out
  set under a changed rubric. Every judged probe score records `rubric_hash` in its details
  (integrity and identity scorer version 2, leak_check 3); `probe_report` refuses verdicts made
  under another rubric than the current one.
- The integrity judge's cache key holds the solution's sha256, not its text. A fixture records
  `solution_digests` per assessment and replays integrity with `FixtureSolutions`, so a live
  fixture judged with the private solutions replays in CI. `record_fixture` takes the run's
  solution book and a `source` (`scripted` or `live`), writes `task_ref` as a bare file name,
  and for scripted runs writes `live_judges: false`, `spent_usd: 0`.
- The tuning log is a hash chain (`prev` = sha256 of the line before); `chain_problems` reports
  an edited, dropped or reordered line, the build test checks it and reads the bar from the
  log. A live held-out or both-split run appends `heldout_scored`, and `eval_live` refuses a
  held-out run while `fresh_heldout_required` holds.
- The regression gate reports "public support kappa fell", never "held-out".

## Rules
- No network or API call in any test; tests read only `golden/public/`; fake judges and
  scripted model outputs only.
- No held-out case text, solution text or judge rationale in the repo, in fixtures, in the
  tuning log or in printed output.
- Bar and support-rule tuning read tuning-split cases only (D-5); the held-out half of every
  calibration is scored once per rubric.
- Never default a missing label, verdict or score (L-12).

## Out of scope
- The release gate, `make eval` and CI wiring (E2.4); the live held-out run and model choice
  (E2.5); identity and leak probe calibration beyond the shared probe-sheet code (D-12
  allows it later; CAL-01 names groundedness and integrity).
