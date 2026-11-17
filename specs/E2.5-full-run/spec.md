# E2.5 — Full run and model choice

**Wave 2. Depends on:** E2.4. **Unblocks:** T3.1, T3.2.

## Goal
Run the golden set live on the candidate answer settings, choose the answer model and its
settings (quality first, then cost per grounded answer; FRN-02), publish the report (US8), and
replace the §9 planning estimates with measured cost (FRN-03, NFR6). Close or re-scope D-44:
NFR5 asks for 8 s at p95 and the A2.5 smoke run measured 17.3 s.

## First: the D-44 latency candidates (offline, tests first)
Each sits behind config and is tested offline with fakes; none weakens a safety gate.

- **(a) Lower Sol effort.** Already config: `reasoning_effort` per model. Nothing to build.
- **(b) L2 and integrity in parallel.** Already built in A2.5 (`_run` runs them on two
  threads). The smoke run's slowest turns show the remaining serial wait: for a non-course
  route `ask()` still waits for the discarded answer call before finalizing, and a hint turn
  then adds the hint call after it. New `AppConfig.early_finalize` (default `false`): when the
  route is not `course`, `ask()` finalizes without waiting for the discarded answer call (D-9:
  it is never grounded, checked, stored or shown). The call finishes in the background; its
  spend still passes through the same `Source`, so the ledger and the demo guard count it, but
  it is left out of the turn's `cost_usd` and steps, deterministically (whether or not it had
  finished). `latency_ms` then counts route, then hint, for such a turn.
- **(c) Luna for the answer.** Already config (`answer_model`). New `AppConfig.hint_model`
  (default `null` = the answer model), so hints can run on another model than answers; its
  effort must be named like every other model's.
- **(d) Stream the answer, shown only after the checks pass.** Not built: the checks read the
  whole draft (L2 judges every cited sentence, integrity the whole reply), so the reply cannot
  be shown before the full draft exists and both checks return; streaming changes no
  time-to-shown under that rule. Recorded in the decision.

## Outputs

### `src/course_assistant/schemas/config.py`
`AppConfig.hint_model: Name | None = None`, `AppConfig.early_finalize: bool = False`; the
explicit-effort validator covers the hint model.

### `src/course_assistant/answer/hint.py`, `ask.py`
Hint requests use `hint_model or answer_model`. `_run` with `early_finalize` returns on a
non-course (or failed) route without joining the answer future; `_Calls.ignore("answer")`
drops the discarded call's result and any later one, under the lock.

### `src/course_assistant/eval/fullrun.py` (FRN-01 to FRN-04)
- `run_summary(run_dir, ledger_rows)`: from an EvalKit course-ta run folder, the §6.4 gate
  values (`release.case_values` and `release.gates`), latency p50 and p95, and the measured
  cost from the ledger rows whose `run_id` is the run folder's (answers, checks, hints by
  purpose) plus EvalKit's judging spend and the probe judges' spend. Ids and numbers only.
- `compare(a, b)`: paired bootstrap with Holm (`release.regressions`) over the cases both runs
  share, per gated metric.
- `choose(settings)`: the FRN-02 rule. A setting qualifies when its latency p95 is at most
  8 s and its grounded, integrity, injection, leak and identity values are not below the
  reference setting's; among those the lowest cost per grounded answer wins; quality
  (grounded rate) breaks ties before cost. No setting qualifies: none is chosen and the
  reason is returned (the owner decides a trade-off, D-44).
- `cost_model(per_question_usd, run_usd, ...)`: monthly projection (questions a month times
  measured cost per question, plus full runs a month times measured run cost) against the
  $150 cap (OWNER D-37) and its 80% alarm.
- `sample_for_reading(record_ids, n=20, seed)`: a fixed, rule-stratified sample of public
  record ids to read end to end (FRN-04, L-14). Held-out records are never sampled for
  quoting.
- `render_report(...)`: the published report (Markdown) from summaries; ids and numbers only.

### `scripts/smoke_live.py`
`--latency-only` runs part 1 (the 30 public questions) only, `--public-all` runs every public
case instead (earlier turns seeded, statuses counted per rule), and `--label` names the setting
in the run id and result file; each live smoke run is ledgered under its own run id.

### `scripts/full_run_report.py`
Reads the run folders and `runs/ledger.jsonl`, prints the summaries and the comparison, and
writes `docs/measurements/e2.5-report.md` (aggregates only, no case text). With no judged run,
`--cost-from LABEL --run-usd X` projects the month from a smoke row's ledger cost per question.

### Records
Tech Spec §2.6 and §9 get the measured numbers; a new decision closes or re-scopes D-44 and
logs the answer-model choice (FRN-02); RUN-STATE holds the exact spend per run.

## Spend rules
Total live spend for the node at most $60 (owner approval for this node); every run ledgered
to `runs/ledger.jsonl`; each run's `--max-cost` at most what remains of the $60. Keys are only
read from the environment by existing code, never printed.

## Rules
- No held-out text anywhere (report, RUN-STATE, decisions, printed output).
- Never default a missing score (L-12). Read 20 records before trusting a headline (L-14).
- The gate verdict is recorded as it comes; exit 1 or 2 is expected until E2.3's calibration
  is certified by human labels.

## Out of scope
Human calibration labels (E2.3, owner); the live CI job secrets (E2.4, owner); flipping the
live pointer (the gate must pass first); the app's spend tile (D3.2's files).
