# course-assistant build graph

Source of truth for build-node dependencies. Parsed by `scripts/harness_lib.py`.
A node may start only when every node in its `Depends on` cell is green (all its rows in `catalog.tsv` are green).
Edge rules: no cycles; a node inherits the constraints of what it depends on; `Verified by` must pass before a node flips green.
External nodes have `owner attest` in `Verified by`; their rows take the single proof `attest=OWNER D-n` (D-17).
Generated from the Tech Spec §11.2 on 2026-11-15; edit here from now on and keep the Tech Spec in sync.


## Wave 0 — harness, sign-off gate and EvalKit upstream

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **W0.1** | Repo and harness | — | node_converge + verifier | uv project, CI, harness scripts ported with self-tests, records seeded (D-1 to D-20, inherited lessons), corpus guard and secret scan gates | — |
| **G0.1** | Sign-off (external) | W0.1 | owner attest | Proposal sent; signed scope note, processor agreement and pilot host recorded as `OWNER D-n` | NFR1 |
| **X1.1** | EvalKit v0.2 upstream (built in the EvalKit repo) | W0.1 | node_converge + verifier | Case-aware system adapter, task plugin scorers in runs, slice reporting; `evalkit-harness 0.2.0` released | US8 |

## Wave 1 — core

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **N1.1** | Schemas and config | W0.1 | node_converge + verifier | §3 models, `AppConfig` loader, fixtures, round-trip contract tests | US1, US7 |
| **N1.2** | OpenAI client, cost and tracing | N1.1 | node_converge + verifier | Responses wrapper (retry, error taxonomy, `store=false`), `prices.yaml`, cost per call, spend ledger, trace interface with JSONL sink and text stripping | NFR3, NFR6, NFR8 |
| **N1.3** | Sample course and manifest | N1.1 | node_converge + verifier | 3 sessions (slides and pre-reads, CC BY 4.0), public manifest, assessment briefs, private solutions | US10 |
| **N1.4** | Ingest and vector store sync | N1.2, N1.3 | node_converge + verifier | Splitter, corpus store, upload with attributes, diff, delete file and File object, reconcile, `make ingest` | US7 |
| **N1.5** | App store, audit and pause | N1.1 | node_converge + verifier | SQLite repository for questions, answers, escalations, consent, audit, pause; retention and `make purge` | US9, NFR7 |

## Wave 2 — answer path

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **A2.1** | Scope check | N1.2, N1.3 | node_converge + verifier | Route step on Luna, assessment match, injection category, fixtures | US2, US3 |
| **A2.2** | Answer step | N1.4 | node_converge + verifier | Responses with file search, session and corpus-version filters, results included, citations to `Citation`, `ANSWER_RULES` hashed | US1, US4 |
| **A2.3** | Grounding check | A2.2 | node_converge + verifier | L1 deterministic, L2 Luna support check, connective allow-list, bar from config | US2 |
| **A2.4** | Integrity guard and hints | A2.1, A2.2 | node_converge + verifier | Hint step, post-answer integrity check, fixed fallback hint, decomposition fixtures | US3 |
| **A2.5** | `ask()` orchestration and resilience | A2.1, A2.3, A2.4, N1.5 | node_converge + verifier | Parallel route and answer, Finalize table, leak scan, pause, NFR8 message, replay injection, 30-question smoke run with measured latency and cost | US1–US4 |

## Wave 2 — people and privacy

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **B2.1** | Escalation queue | N1.5, A2.5 | node_converge + verifier | `escalate`, `reply`, statuses, timestamps, reply waiting in history, flag material gap | US5 |
| **B2.2** | Notice, consent, history, delete | N1.5, N1.2 | node_converge + verifier | First-use notice (versioned), consent record, history, delete with trace purge and the 7-day check | US9 |
| **B2.3** | Teaching-team view | B2.1, B2.4 | node_converge + verifier | Streamlit page: queue, reply, digest, manifest view, pause switches, ops tile, passcode | US5, US6, US7 |
| **B2.4** | Weekly digest | B2.1, B2.2 | node_converge + verifier | Topic labels on Luna, k = 5 suppression, consent purpose filter, leak test | US6 |
| **B2.5** | Learner chat page | A2.5, B2.1, B2.2 | node_converge + verifier | Streamlit chat: session picker, answer card, source chips with unit text, AI label, handover, notice, history | US1, US2, US4, US9 |

## Wave 2 — evaluation

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **E2.1** | Golden set | N1.1, N1.3 | node_converge + verifier | 280 cases (§6.1), splits, held-out manifest, second-reader review, coverage rule | US8 |
| **E2.2** | EvalKit task `course-ta` | E2.1, A2.5, X1.1 | node_converge + verifier | Task YAML, system adapter, corpus-as-context, locator, integrity, leak and identity scorers, slices | US8 |
| **E2.3** | Calibration and bar tuning | E2.2 | node_converge + verifier | 100-label calibration for groundedness and integrity, `grounding_bar` tuned on the tuning split, regression fixture | US8 |
| **E2.4** | Release gate | E2.3, N1.4 | node_converge + verifier | `make eval` gate (§6.5), CI on manifest and rules changes, weekly schedule, corpus-version flip, drift retirement | US7, US8 |
| **E2.5** | Full run and model choice | E2.4 | node_converge + verifier | Golden set on Sol and Luna, published report, answer-model decision, measured cost model | US8 |

## Wave 3 — demo, trust pack and open-source release

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **D3.1** | Demo replay and guard | A2.5, B2.1 | node_converge + verifier | `make demo-cache`, replay file, guard with persistent KV counter, demo OpenAI project with spend limit | US10 |
| **D3.2** | Demo deploy | D3.1, B2.5, B2.3 | node_converge + verifier | Both pages in `portfolio-demos`, replay default, passcodes, off switch shown working | US10 |
| **T3.1** | Trust pack | B2.2, E2.5, D3.2 | node_converge + verifier | Behavioral spec, data card, privacy notice, DPDP and UK GDPR mapping, incident runbook (72-hour report), off-switch test evidence | NFR2, NFR7 |
| **T3.2** | Project page and walkthrough | T3.1, E2.5, D3.2 | node_converge + verifier | Case study under the nine architecture areas on Cloudflare Pages, walkthrough GIF (D-75) | — |
| **R3.1** | Open-source packaging and v0.1.0 | T3.1, D3.2 | node_converge + verifier | PyPI `course-assistant` by trusted publishing, `init` and `doctor`, docs (quickstart on the sample course, bring your own course, evaluate your course), community files, privacy statement, eval GitHub Action example, tag v0.1.0 | US7, US8, US10 |

## Wave 4 — pilot (after sign-off)

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **P4.1** | Pilot cohort onboarding | G0.1, E2.4, D3.2 | owner attest | Private manifest ingested, teaching-team cases added, pilot host, SSO, Langfuse decision, calibration relabel by the teaching team, app and retrieval wired to the live corpus pointer (`serving_corpus`, two vector-store attribute sets; D-57) | US7, US8 |
| **P4.2** | Pilot operations | P4.1 | owner attest | Four weeks: weekly gate runs, purge job, incident drill, metrics | US5, US6, US8 |
| **P4.3** | Pilot report and teardown | P4.2 | owner attest | Pilot report to the programme owners, `make teardown` with proof that the store and files are gone | NFR4 |
