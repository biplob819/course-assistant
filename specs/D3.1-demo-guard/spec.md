# D3.1 — Demo replay and guard

**Wave 3. Depends on:** A2.5, B2.1. **Unblocks:** D3.2.

## Goal
Give the demo (FR12, D-16, Tech Spec §8) everything it needs before it is deployed: a replay
file of about 20 curated public questions answered through the real `ask()` path (so the page
can show real answers with no API call), the live-mode guard that reuses EvalKit's pattern
(session-token rate limit, per-request cap, persistent daily spend counter, one cost number,
L-39), the passkey gate that OWNER D-45 and OWNER D-48 put in front of every page and route,
and the operator steps for the separate demo OpenAI project with a hard spend limit (NFR6).
Every criterion is proved offline with replays and fakes. Generating the real replay file is
a live run: this node ships the script and leaves the run pending (owner, before D3.2).
It also closes the item B2.2 deferred to here: any exception inside `Assistant._settle` is
audited and alerted, never only swallowed.

## Outputs

### `src/course_assistant/demo/cache.py` (RPL-01)
- `CURATED`: 20 public golden case ids with the status each is expected to show, at least
  two per learner-visible status (`answered` 6, `not_covered` 7, `hint_only` 4,
  `handed_over` 3). `error` is not curated: it cannot be produced on purpose by a live
  model, and the demo shows failures as guard refusals instead. Ids come from
  `golden/public/` only (never the held-out split); `curated_cases(public_cases)` reads the
  question, session scope and earlier turns from the public file and refuses an id that is
  missing or not in the public split.
- The demo cache reuses A2.5's replay format (`golden.Fixture`, format 1): config, corpus,
  briefs and per question the recordings and the expected `Answer`. Request ids and learner
  pids are derived from the case id (`r-` + sha256, 16 hex), so a cache replays by case.
- `build_cache(cases, *, config, corpus, assessments, source_for, now)`: each curated
  question runs through `golden.run_case` (a fresh store, prior turns seeded, the real
  `Assistant`) with its calls passing a `Recorder`; the recordings are then replayed through
  `ask()` to produce the stored `Answer` (so what is stored is exactly what a replay gives).
  A case whose status is not the one expected, or whose recordings are not all used, is left
  out and named in the `BuildReport`; the build fails (`DemoCacheError`) unless at least two
  cases remain per status. Nothing in the report holds question or answer text.
- `load_cache(path)`, `verify_cache(fixture)` (every case replays with no API call to its
  stored `Answer`, every recording used) and `DemoCache.answer(case_id)` for the page.
- `estimate_usd(n, config, prices)`: printed before any live call, from the guard's
  per-request hold (one cost number).

### `scripts/demo_cache.py` and the `make demo-cache` target (RPL-01, live, pending)
- `uv run --env-file .env python scripts/demo_cache.py --config CONFIG.yaml --yes` builds
  `demo/replay.json` from the ingested sample-course corpus with the live `respond` (spend
  to `runs/ledger.jsonl`, the ledger's monthly budget), prints the case count and estimate
  first, refuses without `--yes` or `OPENAI_API_KEY` or over `--max-cost`, and never prints
  question or answer text. `--dry-run` prints the plan with no key and no call; `--check`
  replays an existing cache offline (exit 1 on a mismatch). `make demo-cache` runs it.
  The demo cache holds sample-course text, which is public (CC BY 4.0, §8).

### `src/course_assistant/demo/passkey.py` (RPL-04)
- `hash_passcode(code, *, salt=None, iterations=600_000)` gives
  `pbkdf2_sha256$<iterations>$<salt b64>$<digest b64>` (stdlib PBKDF2-HMAC-SHA256, 16-byte
  random salt). A passcode is 6 to 12 digits (OWNER D-48: a simple numeric passcode).
  `verify_passcode(code, stored)` derives with the stored salt and iterations and compares
  with `hmac.compare_digest`; a malformed stored hash fails closed (a `PasskeyConfigError`,
  never a pass).
- `PasskeyGate(load_hash, *, max_failures=5, window_s=900, clock)`. `load_hash` reads the
  hash at every check from the secret store (`DEMO_PASSKEY_HASH` through `read_secret`,
  environment first, then a Streamlit-style secrets mapping); it is never in the repo.
  - `unlock(code, *, session, ip)`: refuses with `GuardRefused` when no hash is set (the gate
    is closed, fail closed), when the session or the IP has had `max_failures` failed attempts
    within `window_s` (checked before the passcode, so a locked caller learns nothing, even
    with the right code), or on a wrong code (counted against both the session and the IP).
    On success the session is unlocked and bound to the hash's fingerprint.
  - `require(session)`: every page and route calls it before doing anything; it refuses unless
    the session was unlocked against the hash that is set now. Changing the secret revokes
    every unlocked session at once; removing it closes the gate.
  - The failure memory is bounded (the oldest idle keys are forgotten first); refusals are fixed
    texts with no code, hash or salt in them.
- `course-assistant passkey-hash` (CLI): reads the passcode twice with `getpass` (no echo),
  refuses a non-numeric or short one, and prints only the hash line to paste into the secret
  store. It writes no file and makes no network call.

### `src/course_assistant/demo/spend.py` (RPL-02)
- `SpendCounter` protocol: `reserve(day, micro_usd, cap_micro_usd) -> bool`,
  `settle(day, reserved, actual)`, `spent(day)`. Money is integer micro-dollars, so the
  counter has no float drift at the cap.
- `FileSpendCounter(path)`: a JSON file with an exclusive lock around each read-modify-write,
  persistent across restarts; the test fake and local development counter.
- `KVSpendCounter(kv, prefix)`: the hosted KV store (D-16) through a `KVClient` protocol
  (`incrby(key, n) -> int`, `get(key) -> int | None`, `expire(key, seconds)`). A reservation
  is one atomic `INCRBY`; if the new total passes the cap it is given back with a second
  `INCRBY` and refused, so concurrent requests can never hold past the cap together (at worst
  one is refused early). Keys expire after three days.
  `RestKV(url, token, post=...)` speaks the Redis-over-REST protocol of the hosted KV
  (`DEMO_KV_REST_URL`, `DEMO_KV_REST_TOKEN`); its transport is injected, so tests make no call.

### `src/course_assistant/demo/guard.py` (RPL-02)
- `GuardConfig(request_hold_usd=0.50, daily_cap_usd=5.00, rate_limit=5, rate_window_s=3600)`:
  placeholder amounts until the owner sets the demo budget; D3.2 reads them from the secret
  store.
- `Guard(gate, spend, config, *, clock, today)`; `allow(estimate_usd, session_token, *,
  own_key=False) -> Ticket`, in order: the passkey (`gate.require`), the per-request cap
  (`estimate_usd` over `request_hold_usd` is refused), the session-token rate limit, and,
  on the owner's key only, the daily reservation of exactly `estimate_usd`. A visitor's own
  key (OWNER D-45) still passes the passkey, the cap and the rate limit, and holds nothing
  against the owner's budget. A spend-store outage refuses readably (never its message) and
  gives the rate-limit slot back; a refused reservation does too.
- `CappedSource(source, ticket, prices)`: the executor. Before each outside call it reserves
  that call's bound (`call_bound_usd`: request bytes as tokens, which over-counts every
  script, plus the retrieved-chunk allowance and file-search calls when the call searches,
  `max_output_tokens` out, times N1.2's `MAX_ATTEMPTS`) and refuses the call (an `LLMError`
  of kind `config`, so the turn ends with status `error`, nothing spent) if the request's
  held total would pass the ticket. After the call it counts the actual cost plus a full
  single-attempt bound for each retried attempt; a failed call counts its whole bound
  (L-39: cancelled and timed-out calls are never free). An actual cost above its bound is
  alerted.
- `Guard.settle(ticket, actual_usd)` returns the unused part; if the store is down the whole
  hold stays (overcounted, never undercounted). The guard, the counter and the executor use
  one number: the ticket's hold.

### `src/course_assistant/demo/live.py` (RPL-02, RPL-04)
- `LiveDemo(gate, guard, assistant_for, responder_for, store, prices)`; `ask(session,
  question, pid, *, own_key=None)`: `gate.require` first, then `guard.allow`, then one
  `Assistant.turn` through a `CappedSource` on the owner's responder or one built for the
  visitor's key for this request only (never stored, logged or kept in a repr), then
  `guard.settle` with the executor's total, then B2.1's `queue_handover` for a handed-over
  turn. `replay(session, case_id)` serves the demo cache behind the same passkey.
  Logs are aggregate only: time, mode, status, cost, latency.

### `Assistant._settle` audit and alert (B2.2 deferred item)
- Every exception inside `_settle` (the unreadable deletion keys at the start, the failed read
  of new requests, the trace delete, each reopen and the history delete) calls the
  Assistant's `alert` hook with the step name and the exception type (never its message),
  logs a warning on `course_assistant.alert`, and writes a best-effort `AuditEvent`
  (`settle_failed`, actor `system-1`, target the learner pid). An alert or audit that fails
  itself never hides the turn's answer. Behaviour is otherwise unchanged (golden replay
  identical).

### Operator steps (RPL-03, owner, before D3.2)
1. In the OpenAI dashboard create a separate project `course-assistant-demo`; set its
   monthly budget to a hard limit (the owner's figure; suggested $20) and an alert at 80%.
2. Create a project API key for that project only, and store it as `OPENAI_API_KEY` in the
   demo host's secret store (never the main project's key, never in the repo).
3. Set `DEMO_PASSKEY_HASH` from `course-assistant passkey-hash`; set `DEMO_KV_REST_URL` and
   `DEMO_KV_REST_TOKEN` for the hosted KV; `PID_SECRET` as in §8.
4. Record the project name, limit and date in `docs/decisions.md` (owner attest). The code
   side is proved here: the live paths read only these names, and the guard's daily cap
   sits under the project's hard limit.

## Rules
- No network or API call in any test; tests use replays, scripted outputs through N1.2's
  real `respond` and fakes (file-backed counter, in-memory KV, injected REST transport).
- No passcode, hash, salt, visitor key, question or answer text in a log, trace, audit row,
  error or report. Tests read only `golden/public/`.
- Fail closed: no hash, a malformed hash, a locked caller or an unreachable spend store
  answers no question.

## Out of scope
- The Streamlit pages, deploy and secrets in the host (D3.2); the live `make demo-cache` run
  and the OpenAI project itself (owner, pending); the demo budget figures (owner).
