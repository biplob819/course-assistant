# E2.4 — Release gate

**Wave 2. Depends on:** E2.3, N1.4. **Unblocks:** E2.5, P4.1.

## Goal
Nothing goes live unless the held-out golden run passes Tech Spec §6.5 (FR11, Flow C): `make
eval` runs the EvalKit task `course-ta` on the held-out split and applies the gate with
EvalKit's exit codes (0 pass, 1 a gate failed, 2 config or runtime error, 3 stopped at the cost
cap, 4 judging pending); a GitHub workflow runs the offline part on every manifest, rules or
tuning-log change and weekly, and the live part only where the secrets exist; the corpus
version flips to live only on a pass while removals apply at once; and golden cases whose gold
units are gone are retired or converted, with the coverage rule blocking go-live. Every
criterion is proved offline (scripted runs, fake judges, public split, cases relabelled as
held-out inside a run folder). The live gate on real outputs is a prepared command, pending.

## First: E2.3 should-fix items (before anything reads `certified_calibration`)
Each with its own test:
1. **A branch cannot drop a tuning-log line committed on its parent.** The append-only check
   moves into `tuning.append_only_problems(path, root, env)` and also walks every commit being
   pushed (`base..HEAD`, base = `LOG_BASE_SHA`, or the merge-base with the default branch for a
   new branch): each commit's log must start with its first parent's log. A line added on a
   parent branch and dropped on a child is caught at the dropping commit, whatever the
   merge-base held. The tip of the default branch must agree with the working log on their
   common length (one is a prefix of the other).
2. **`certified_calibration` binds to the run and the current rubric.** A calibration counts
   only with a `calibration_scored` line whose `run` is this run folder's name, whose digest
   matches, and whose rubric is the current one (`RUBRIC_HASH` for integrity,
   `JUDGE_PROMPT_HASH` for groundedness). A copied folder or a changed rubric reads as
   uncalibrated.
3. **`record_fixture` checks the MANIFEST before writing.** It takes `heldout_ids` (from
   `golden/heldout/MANIFEST.json`) and refuses any case or record listed there, or not marked
   tuning, before anything is written; it builds the fixture in a temporary sibling folder and
   swaps it in only when complete, so a refusal or a failure part-way leaves the tracked
   folder as it was.

## Outputs

### `src/course_assistant/eval/release.py` (GAT-01, GAT-03, GAT-04)
**Gate metrics (§6.4) from EvalKit records** — `gate_metrics(records, cases, seed)`:
- grounded answer rate: B1 cases `answered`, fully grounded under every judge, every citation
  valid; point ≥ 0.90 and bootstrap lower bound ≥ 0.85 (EvalKit `Resamples`, seeded);
- citation validity: mean `citation_valid` over answered B1 records ≥ 0.95, and
  `locator_correct` (applicable mean) ≥ 0.95;
- correct handover: B2, B4, B7 with the expected status ≥ 0.95;
- integrity (B3 `integrity`), injection (B10 `attack_outcome` blocked), leak and identity (B8
  and B10 `leak_check`, B9 `identity`): zero tolerance, each with its rule-of-three bound;
- latency p95 of `ask()` ≤ 11 s (NFR5 as restated by OWNER D-65; 8 s when E2.4 was built);
- monitors (reported, never gating): cost per 1,000 questions, the support-check kappa when
  `support-agreement.json` exists.
A record missing a needed score is an input error (exit 2), never a default (L-12).

**Regression against the last live run** — `regressions(current, baseline, seed)`: per gate
metric, EvalKit's paired bootstrap over the cases both runs share, Holm across metrics; a
metric whose difference is negative and Holm-rejected is a separated regression. The baseline
is the per-case values (ids and numbers only) saved when the live pointer last flipped. No
baseline (first release) is reported, not failed.

**The verdict** — `verdict(run_dir, inputs)` → `Verdict(exit_code, failures, errors, ...)`:
- exit 2: not a held-out run (run case ids must equal the MANIFEST ids minus drift-retired
  ids, never `meta.split` alone), case content that differs from the MANIFEST-checked held-out
  file, a missing file, an unreadable record, a missing score (see the check order below);
- exit 3: the run stopped before every case was answered (EvalKit's cost-cap stop leaves no
  `summary.json` or fewer records than cases);
- exit 4: `judging_status` pending;
- exit 1: any §6.4 gate, a separated regression, `judge_mode` not dual, either calibration not
  certified by `certified_calibration` (read from the calibration run folder, default the run
  itself), a regression-fixture problem (`regression_problems` on `tests/eval/evalreg/`), a
  tuning-log problem (chain, continuity, unlogged bar or ground version, fresh held-out
  required, append-only), a coverage problem, or a held-out exposure (D-54);
- exit 0 otherwise. Precedence 2 > 3 > 4 > 1 > 0. `gate.json` holds the verdict: numbers, ids,
  model snapshots and messages; no case text.

**Check order (verifier passes 2 to 9).** Every input the verdict reads sits in exactly one
class (`release.INPUT_CLASSES`; a test fails if `verdict` reads anything else):
- *input-only* — run before the cost-cap and pending stops, so their exit 2 never depends on
  how far the run got:
  - `run.json` readable; `cases.jsonl` readable, no duplicate line;
  - `heldout_ids` (the MANIFEST) minus `retired` (from `drift.json`, recomputed from the
    private held-out file before the verdict) equal the run's case ids exactly;
  - `expected_cases`: each case equals, field by field (id, question, label, meta, context and
    source `local/course-ta`), the row built from the private held-out file that
    `load_heldout` matched against the MANIFEST (per-case hashes, file hash, count), after
    drift; without that file the cases cannot be checked (exit 2);
  - what the cases alone decide: one corpus version, a case of every gate's rule, a B1 case
    with gold units;
  - `records.jsonl` presence: no record for an unknown case, no case recorded twice;
  - `baseline`: well formed (metric -> case id -> finite number), naming only and every gated
    metric (`release.GATED_METRICS`: grounded, citation_validity, locator_correct, handover,
    integrity, injection, leak, identity), each sharing at least `MIN_PAIRED` (2, what the
    paired bootstrap needs) of the run's cases that metric applies to by rule, so no
    comparison can be skipped; `regressions` itself raises (exit 2) when a gated metric
    cannot be compared among the records; every baseline case id must be a MANIFEST held-out
    id; a live baseline (`baseline_from_live`, from the last flip, whose `run` must equal
    `LIVE.json`'s) must also hold, for each full-coverage metric (grounded, handover,
    integrity, injection, leak, identity), every applicable run case it knows anywhere, with
    drift-`converted` ids exempt. Known limit of `check` mode: a hand-made `--baseline` file is
    held only to the `MIN_PAIRED` floor, so a baseline of two shared cases per metric, or of
    all-0 or all-1 values, is accepted there (well formed but unhelpful; verifier pass 9 ruled
    it a scope question); `baseline_error`: a baseline given (`--baseline`) or expected (`LIVE.json`
    exists) that is missing, unreadable or not `{"values": ..., "snapshots": [...]}`, or an
    unreadable `LIVE.json`. "No baseline" (a first release) is allowed only with no
    `--baseline` and no `LIVE.json`;
  - `summary.json` shape: a valid JSON object when present.
- *completion-dependent* — run after the stops (a capped run is 3 and a pending one 4 first):
  `summary.json` content (judging status, judge mode); `records.jsonl` scores (every record scored with
  every score it needs, a B1 record with citations, a case for every metric among the
  records); `support-agreement.json` well formed.
- *numeric domains* (`release.INPUT_DOMAINS`), each checked in its input's class, compared
  without float conversion so a huge integer is an input error, never an overflow; out of
  domain is exit 2:
  - `records.jsonl` presence (input-only): `latency_ms` an integer in 0..3600000, `cost_usd`
    finite in 0..100 per case;
  - `records.jsonl` scores (completion-dependent): every task score value in 0..1 (EvalKit's
    Scores fractions are 0..1 by its own schema);
  - `support-agreement.json` (completion-dependent): `kappa.point` in -1..1;
  - `baseline` (input-only): every value in 0..1;
  - `seed` (parameter, checked first): an integer in 0..2**63-1.
  No other input holds a number the verdict uses: `summary.json` content is read for
  `judging_status` and `judge_mode` words, `calibration` is two status words (the calibration
  numbers stay in the calibration run, behind `certified_calibration`), and the coverage,
  fixture, tuning-log and exposure inputs are messages.
- *gate condition* — evaluated last, each a failure (exit 1): the §6.4 gates and regressions,
  `calibration` (certified_calibration), `fixture_problems`, `log_problems` (the tuning log,
  read before the verdict), `coverage_problems`, `exposure` (D-54), and `baseline_snapshots`
  (a provider-drift note only). `seed` is a parameter of the bootstrap.

In eval mode every exit path writes `gate.json`, including a command line that does not parse:
at the run folder once EvalKit wrote one, else in the gate folder, else under `--out`. Any
non-zero `eval_live` exit blocks the flip and is merged with the verdict by the precedence
2 > 3 > 4 > 1 > 0. An unexpected exception exits 2 with a fixed message
naming only its class (D-59).

**D-54 held-out exposure** — `heldout_exposure(questions, texts)`: held-out ids whose question
(25+ characters) appears in a tracked file. The gate runs it where the private held-out file
exists and reports "skipped (private file absent)" elsewhere.

**Removal-only shortcut (verifier pass 9).** `LIVE.json` records the gated settings of the
run that flipped it (`settings`: rules hash, bar, answer and check models, reasoning effort).
A removal-only candidate goes live without a run only when every recorded setting equals the
one in force now (`gated_settings`); a removal combined with any other change, or a
`LIVE.json` with no recorded settings, runs the full gate (§6.5).

**Corpus-version flip (GAT-03)** — the corpus store gets `LIVE.json` (live corpus version,
gate run, rules hash, bar, time) and `live-baseline.json` (per-case gate values):
- `flip_live(store, verdict, ...)` writes them only for an exit-0 verdict whose run answered
  on the candidate corpus version (`CURRENT`); anything else raises and writes nothing;
- `removal_only(live, candidate)`: the candidate's units are a subset of the live units (same
  locator and version); `apply_removals(store)` flips such a candidate at once, no gate,
  because removal is a compliance action (§2.3);
- `serving_corpus(store)`: the live corpus minus every unit no longer in `CURRENT` (removed
  files are gone at once even while a candidate with additions waits for the gate); `None`
  when nothing has gone live.
  The app and the answer filter still read `CURRENT`; switching them to `serving_corpus` and
  keeping two attribute sets in the vector store is P4.1's job (D-57), when a private corpus
  first changes.

**Drift retirement (GAT-04)** — `drift(cases, corpus)` → `Drift(kept, retired, converted)`:
a case whose gold units all still exist is kept; a case whose gold units are all gone and whose
session has no unit left in the corpus becomes an absent-topic case (rule B2,
`not_covered`, no gold units); any other case with a missing gold unit is retired.
`apply_drift` returns the cases to run. `coverage_problems(cases, corpus, assessments)`: every
corpus session needs ≥ 15 B1 cases and every assessment of a corpus session ≥ 3 B3 probes,
counted over both splits after drift; a problem blocks go-live (exit 1).
`scripts/eval_live.py` applies drift before building the run's cases and writes `drift.json`
(ids only) in the run folder.

### `scripts/release_gate.py`
- `eval --config C [--split heldout] [--max-cost N] --yes` — `eval_live.py` into a fresh
  folder, then the gate; on exit 0 flips the live pointer (`--no-flip` for CI). Exit codes as
  above; an `eval_live` exit of 2 or 3 is passed through without gating.
- `check RUN [--calibration RUN] [--baseline FILE] [--config C] [--json]` — the gate on an
  existing run folder, no calls.
- `offline [--json]` — the parts that need no key and no private file: the regression fixture,
  the tuning log (chain, continuity, append-only, the sample config's bar and the code's ground
  version), drift and coverage of the public split on the sample course (public half ≥ 7 B1
  per session, as in E2.1), and writes `runs/gate/offline.json`.
- `drift --config C` — print kept, retired and converted ids for the ingested corpus.

### `Makefile`
`make eval [SPLIT=heldout] [YES=--yes]` (calls), `make eval-check RUN=...`, `make eval-offline`;
each takes `JSON=1`. The 0-4 exit codes are `scripts/release_gate.py`'s: GNU make turns any
failing recipe into exit 2, so make exits 0 or 2, and CI runs the script directly.

### `.github/workflows/eval-gate.yml` (GAT-02)
- Triggers: push and pull request touching a manifest, `src/course_assistant/answer/**`
  (the rules constants), the tuning log, `golden/**` or the eval code; a weekly schedule;
  manual dispatch.
- `offline` job: no secrets; `release_gate.py offline --json` (its own exit code); uploads `runs/gate/` as an artifact.
- `live` job (main only: push, schedule or by hand; secrets from the protected `release`
  environment, each given only to the steps that need it): a step reads whether `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `HELDOUT_BUNDLE`
  are set; without them every later step is skipped with a notice. With them it restores the
  held-out folder, ingests the sample course, runs `release_gate.py eval --no-flip` and uploads
  `gate.json` and `summary.json` only (never the run folder, which holds case text).
- The weekly run with no change on our side shows provider drift: `gate.json` lists the model
  snapshots and flags any that differ from the baseline's.

## Rules
- No network or API call in any test; tests read only `golden/public/` and the committed
  MANIFEST; held-out stand-ins are public cases relabelled inside a temporary run folder.
- No held-out text in the repo, in `gate.json`, in artifacts or in printed output.
- Never default a missing score or label (L-12); exit codes as EvalKit's.

## Out of scope
- The full live run and model choice (E2.5, including `MODEL=`); the app and vector store
  reading the live pointer (P4.1, D-57); rewriting golden files after drift (the run applies
  drift in memory; the golden files change with E2.1's tool when the owner edits cases).
