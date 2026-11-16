# A2.5 — `ask()` orchestration and resilience

**Wave 2. Depends on:** A2.1, A2.3, A2.4, N1.5. **Unblocks:** B2.1, B2.5, E2.2, D3.1.

## Goal
Wire the green steps into one learner turn (Tech Spec §2.2): pause check, conversation window,
route and answer in parallel, grounding and integrity, Finalize, the post-answer leak scan, the
hint step, and the NFR8 fixed message, with every outside call replayable by request id and
step so the whole graph runs in CI with no API calls (§10), and a live smoke script that
measures latency and cost (§2.6, §9) when the owner runs it.

## Outputs

### `src/course_assistant/answer/ask.py`
- `Assistant(config, corpus, assessments, store, responder, *, sink=None, now=..., timer=...,
  new_id=...)`. `responder` is a plain `Responder` (N1.2's `respond` with its client, prices,
  ledger and sink bound) or a `Replay` (below). `now` gives aware UTC datetimes, `timer` a
  monotonic clock for latency, `new_id` 16 hex characters for request and conversation ids;
  all three are injectable so a replay is exact.
- `Assistant.ask(question, learner_pid, session=None, conversation_id=None, *,
  request_id=None) -> Answer` (Tech Spec §4; synchronous, because N1.2's `respond` is, and the
  parallel start uses two threads). `Assistant.turn(...)` takes the same arguments and returns
  a `Turn`: the `Answer`, the conversation id, the request id, whether the answer was
  discarded, the leak flag, the injection flag, the handover flag, the kept not-covered draft
  (for B2.1's queue, never shown) and one `StepTiming` (step, latency, cost, calls, error kind)
  per step that ran. `new_conversation(learner_pid, session=None)` closes the learner's open
  conversations and starts a new one (the "New conversation" button).
- Input checks before anything is stored or called: the question is a non-blank string of at
  most `RouteStep.MAX_QUESTION_CHARS`, the pid is 16 hex, the session (when given) is in the
  live corpus, a given conversation exists, is this learner's and has the same session scope;
  anything else raises `ValueError` (a caller bug, never a status). A request id already
  used for a stored question is refused, so an earlier turn is never replaced (D-42).

#### Pause (ASK-04)
- `store.is_paused(session)` first (N1.5 fails closed on any read error). Paused: the Answer
  has status `not_covered` and the fixed `PAUSED_MESSAGE`, no call is made and nothing is
  stored (the off switch takes nothing in).

#### Conversation and window (ASK-07)
- `conversation_id=None` starts a new conversation. A conversation idle for 2 hours or more
  (`IDLE`, from its last question, or its start), or already closed, or already holding
  `MAX_TURNS` (20, D-40) questions, is closed (`closed_at` = last activity + 2 h for idle, now
  otherwise) and the turn goes into a new conversation of the same learner and scope.
- The window is built from `store.questions_for(pid, conversation_id)` only (the store refuses
  another learner's rows), with each question's stored answer, oldest first, skipping turns
  whose answer is missing or `error`. Earlier replies are the stored answer text with citation
  markers removed (ASCII `[n]` and `【…】`), never `Citation` objects. Then the last
  `config.window_turns` (6) turns, and from the oldest end whole turns are dropped until the
  window is at most `config.window_tokens` (3,000) tokens, counted as characters / 4 (the
  route step's rule); a turn is never cut.
- Integrity reads every earlier reply of the conversation (not only the window), as A2.4 needs.

#### Parallel start (ASK-01)
- Route (`RouteStep.run`) and answer (`AnswerStep.request` → responder → `AnswerStep.draft`)
  start together on two threads with the raw question and the window (§2 parallel-start
  bullet: the rewrite is for the assessment match and the trace). `ask()` waits for both, so
  nothing runs after it returns, the discarded call's cost is counted (D-9) and its error is
  traced, not lost. Unless the route is `course` the draft is discarded: it never reaches
  Ground, integrity, the learner or the Answer (`Turn.discarded` is true). An `LLMError` from a
  discarded answer call does not change the status.

#### Course path, then Finalize (ASK-02)
- Ground L1 is evaluated first with A2.3's public checks (not a refusal, text, citations
  resolve in the filter, top score ≥ bar). L1 failing: `GroundStep.assess` returns with no
  call and integrity is not run. L1 passing: `GroundStep.assess` (L2) and `IntegrityStep.run`
  (the draft plus every earlier reply) run at the same time (§2.6).
- `decide(route, ground_passed, integrity_passed, api_error, leaked) -> Status` is the
  Finalize table as one pure, total function over every combination:

| Route | Checks | Status |
| --- | --- | --- |
| any | an `LLMError` on a call the outcome needs | `error` (NFR8 message) |
| `course` | ground passed, integrity passed, no leak | `answered` |
| `course` | ground failed or not run | `not_covered` (`NOT_COVERED` text, draft kept for the queue) |
| `course` | ground passed, integrity failed | `hint_only` |
| `graded_work` | — | `hint_only` (answer discarded) |
| `admin`, `personal` | — | `handed_over` (`HANDED_OVER` text, handover flag) |
| `off_topic` | — | `not_covered` (`OFF_TOPIC` text, no handover offer) |
| `injection` | — | `not_covered` (`INJECTION` text, injection flag in the trace) |
| any shown model text | leak scan hits | `not_covered` (`NOT_COVERED` text, leak flag) |

- Hint: the brief is the integrity verdict's `assessment_id`, else the route's; `HintStep.run`
  with every earlier reply. With no known brief the hint is the fixed `GENERIC_HINT` (no call).
  A grounded hint keeps its citations; a fixed hint has none.
- A route reply that will not parse is A2.1's fallback (`course`, confidence 0): the checks
  still decide.

#### Leak scan (ASK-03)
- Every model-written text that would be shown (an answered draft, a grounded hint) is
  scanned: A2.2's `AnswerStep.CANARY` (case-insensitive), any 16-hex token (the pid pattern,
  D-13), and any run of more than 8 consecutive words (9 or more, lower-cased letter/digit
  words) found in any rules text (`ROUTE_RULES`, `ANSWER_RULES`, `SUPPORT_RULES`,
  `INTEGRITY_RULES`, `HINT_RULES`). A hit replaces the text with `NOT_COVERED`, status
  `not_covered`, no citations, and sets the leak flag. The fixed texts are scanned in a test
  and never hit.

#### Errors (ASK-04, §2.5)
- Any `LLMError` on a call the outcome needs (route, a kept answer, support, integrity, hint)
  gives status `error` with `ERROR_MESSAGE` (NFR8: the fixed message with the support route).
  The error kind (transport, parse, config) goes in the trace; its message never does. Any
  other exception propagates (never turned into a quiet status). Steps' own parse retries are
  theirs (A2.1, A2.3, A2.4).

#### Records and trace (D-14)
- Stored: the conversation (new or closed), the `Question` (turn index = stored questions in
  the conversation + 1) and the `Answer`, for every status but paused.
- `Answer`: `model_snapshot` of the call whose output decided the status (answer, hint or
  route; `config.answer_model` when no call was made), `rules_hash` (16 hex over every step's
  version and the fixed texts), `corpus_version` live, `top_search_score` of the shown draft
  or hint (0.0 otherwise), `cost_usd` the sum over every call including the discarded answer
  (each cost checked finite and non-negative, L-6), `latency_ms` the larger of the measured
  wall time and the critical path of the step latencies (route ‖ answer, then L2 ‖ integrity,
  then hint), `created_at` from `now`.
- One trace record per step and one for `finalize`, emitted through `strip_text` before the
  sink (L-28): ids, step, route, status, scores, counts, flags, cost, latency, error kind.
  Never question, window, draft, hint or rewrite text.

#### Replay (ASK-05)
- `Replay(recordings)`: a responder that returns the recorded `ResponsesResult` (or raises the
  recorded `LLMError`) for each call by `(request_id, step, n)`, where step is the request's
  purpose (`route`, `answer`, `support`, `integrity`, `hint`) and `n` counts calls of that
  purpose in the request (retries, the hint's own integrity call). Each recording also holds the
  sha256 of the request; a different request, a missing recording or an unused one raises
  `ReplayError` (never an `LLMError`, so it cannot hide as status `error`).
- `Recorder(responder)`: wraps a live or fake responder and records the same keys (used by the
  fixture builder and the smoke script).

#### Golden replay fixture
- `src/course_assistant/answer/replay/golden.json` (packaged, so `course-assistant
  eval-replay` runs from the wheel): 40 requests built by `scripts/build_replay_fixture.py`
  from the public golden split only (questions and prior turns of `golden/public/cases.jsonl`,
  routes from `tests/answer/fixtures/routes.jsonl`), the sample course's corpus structure
  (labels, sessions, file ids, unit versions; unit text replaced by a placeholder, so no
  course text ships in the wheel) and the sample assessment briefs. Model outputs are scripted
  through N1.2's `respond` over SDK-model fakes and recorded; the stored `Answer`s are what
  `ask()` returned. Every status and every route appear, plus a route parse fallback, a
  discarded-answer error, the leak scan's three forms, a paused request and errors on each
  needed step.
- `course-assistant eval-replay [--fixture PATH] [--json]` replays every request into a fresh
  in-memory store and exits 0 when every `Answer` matches exactly, 1 on a mismatch, 2 on a
  fixture or replay error. Gate 10 runs it in the clean venv with no API key (D-22); since
  the answer path imports N1.2's client, the gate installs the wheel with the `openai` extra
  for that command (the `--version` command still runs on the bare install).

### `scripts/smoke_live.py` (ASK-06, live, run by the owner)
- Refuses without `--yes` and an `OPENAI_API_KEY`; prints the cost estimate first and stops
  over `--max-cost` (default $5). Needs an ingested vector store and corpus store
  (`course-assistant ingest --config CONFIG`).
- Runs, through `Assistant` with a `Recorder` and a ledger at `runs/ledger.jsonl`:
  1. 30 public questions stratified over rules (smoke): p50 and p95 latency per step and per
     question, cost per question (mean, p50, p95), status against expected.
  2. The public planted B7 questions (5 in the public split; the other 5 are held out and are
     run by the E2 held-out eval, never read here): each must be `not_covered` (GRD-04, D-38).
  3. The public multi-turn B3 cases: the case's prior turns are seeded, the assembling turn is
     asked live; caught means `hint_only` (INT-04, D-40).
  4. The public B1 follow-up cases: recall@8 against `gold_units` for the raw question with
     the window against the route's standalone rewrite with no window (§2 parallel-start
     bullet).
- Writes `runs/smoke-<stamp>.json` (git-ignored) with every number and the recordings' ids;
  the figures are then written into Tech Spec §2.6 and §9 by hand, with a decision logged.

## Rules
- No learner text in traces or logs (D-14); no solution text anywhere (D-7).
- No network or API call in any test; `golden/heldout/` is never read.
- Nothing fails silently: every call's error is either the status `error` or traced as a
  discarded call, and replay errors are loud.
