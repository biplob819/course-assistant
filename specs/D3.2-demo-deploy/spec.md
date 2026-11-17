# D3.2 — Demo deploy

**Wave 3. Depends on:** D3.1, B2.5, B2.3. **Unblocks:** T3.1, T3.2, R3.1, P4.1.

## Goal
Make the demo (FR12, US10, Tech Spec §8) deployable to Streamlit Community Cloud in the
owner's `portfolio-demos` workspace: one entry point that serves the learner page (B2.5, with
the D3.1 replay as its default mode) and the teaching-team page (B2.3), the dependency and
config files Community Cloud reads, a names-only secrets template, a startup check that refuses
to serve a misconfigured app, the three items carried here from D3.1 and B2.3, the owner's
step-by-step guide and a post-deploy smoke check.

Everything is proved offline (AppTest, fakes, no network). The deploy itself needs the owner
(their app name, repo connection and secrets): it is its own catalog row, DEM-04, marked
`(owner attest)`, and D3.2 cannot flip green until the owner attests it as `OWNER D-n`
(D-60: the harness now honours an owner-attest mark on a single row).

## Outputs

### `deploy/streamlit_app.py` (DEM-01, DEM-02)
- The Community Cloud entry point (main file path `deploy/streamlit_app.py`). It puts the
  repo's `src/` on `sys.path` when the package is not installed, sets the demo defaults that
  are not secrets when unset (`CA_REPLAY=demo/replay.json`, `CA_MANIFEST=sample_course/
  manifest.yaml`, `CA_APP_DIR=runs/app`), then runs `deploy.startup_problem`. On a problem it
  draws only a locked page with the fixed text `LOCKED` and stops: no page, no widget, no
  secret name or value on the page (the missing names go to the log, never values).
- Otherwise `st.navigation` routes two pages: "Course assistant" (the B2.5 learner page,
  default) and "Teaching team" (the B2.3 page), each behind its own passkey.

### `deploy/requirements.txt` and `.streamlit/config.toml` (DEM-02)
- Community Cloud reads a dependency file from the entry point's folder before the repo root
  (so the root `uv.lock`, which carries the dev group, is not what the demo installs).
  `deploy/requirements.txt` lists exactly the project's core dependencies plus the `app` and
  `openai` extras from `pyproject.toml` (a test keeps them equal), so the demo installs what
  `uv sync --extra app --extra openai` would, and nothing from the dev group.
- `.streamlit/config.toml` (root, where Community Cloud reads it): headless, no usage stats,
  `client.showErrorDetails = "none"` (no traceback, which could hold a value, reaches a
  visitor), minimal toolbar, XSRF protection on, uploads capped at 1 MB, file watcher off,
  logger at `warning`.

### `.streamlit/secrets.toml.example` (DEM-02)
- Names only, every value empty: `OPENAI_API_KEY`, `DEMO_PASSKEY_HASH`, `STAFF_PASSKEY_HASH`,
  `DEMO_KV_REST_URL`, `DEMO_KV_REST_TOKEN`, `PID_SECRET` (required), and the optional
  `DEMO_TRUSTED_PROXY_HOPS`. The real `.streamlit/secrets.toml` is git-ignored (already in
  `.gitignore`; a test runs `git check-ignore`).

### `src/course_assistant/app/deploy.py` (DEM-01, DEM-02, DEM-03; no Streamlit import)
- `REQUIRED` (the six names). `startup_problem(env, secrets_store) -> str | None`: refuses
  when a required secret is missing or blank, when either passkey hash is not a valid
  `pbkdf2_sha256` hash, or when the learner and staff hashes are equal. It returns a
  description that holds names only; the page shows only `LOCKED`.
- `LiveSecrets(load)`: a read-only mapping whose `get` calls `load()` (the page passes
  `lambda: st.secrets`) on every read, so a hash changed in the host secret store takes effect
  at the next check with no restart (B2.3 deferred item). Both pages pass it instead of the
  `dict(st.secrets)` snapshot they took once inside `st.cache_resource`.
- `client_ip(headers, hops)`: the trusted client address for the passkey rate limit (D3.1
  deferred item). With `hops = 0` (the default: no trusted proxy is known) it returns None, so
  the gate counts per session plus its global cap; with `hops = n >= 1` it takes the `n`-th
  entry from the right of `X-Forwarded-For` (the address the trusted proxy appended, never
  the client-supplied left side), and only when it parses as an IP address. `trusted_hops`
  reads `DEMO_TRUSTED_PROXY_HOPS` (0 to 5; anything else is 0) at every call. The pages no
  longer pass `st.context.ip_address`, which on a hosted proxy is the proxy's address shared
  by every visitor (five wrong guesses by anyone would lock everyone out).
- `DailyStores(root, today)`: the nightly demo data reset (DEM-03). A callable returning the
  `Store` of today's (UTC) folder `root/<YYYY-MM-DD>/app.sqlite`; when the day changes, earlier
  day folders are removed, so learner conversations, escalations, consents and audit rows of
  the demo live for one UTC day at most. The learner and team pages share it (one store per
  day for both). The spend counter is not in it (its keys are per day already).

### Learner page replay mode (DEM-01; additive change to green B2.5)
- `wiring.from_env` reads `CA_REPLAY` (a D3.1 demo cache) when set and the file exists and
  passes it to `LiveDemo(cache=...)`; when `CA_CONFIG` is unset, the course (config, corpus,
  briefs) comes from that cache, so the host needs no ingested corpus store. `team_wiring`
  does the same, with the manifest from `CA_MANIFEST`.
- `LearnerChat.replay_options(state)` (curated case ids and questions, empty without a cache)
  and `LearnerChat.replay(state, case_id)` (the stored answer as a `Card`) both check the
  passkey first.
- On the page, when a cache is loaded, a "Mode" radio shows "Worked examples (no live
  call)" first and selected; live questions are the second option. Without a cache the page is
  unchanged. Question and answer text reach the page only through `st.text` (CHT-05).

### Provider `max_tool_calls` (DEM-01; additive change to green N1.2 and D3.1)
- `llm.client.MAX_TOOL_CALLS = 3`; `build_kwargs` sends `max_tool_calls` whenever the call
  carries the file-search tool, and D3.1's `FILE_SEARCH_CALLS_BOUND` is that same constant, so
  the provider can never run more hosted searches than the executor's bound priced in. The
  request model (and so every replay key and golden recording) is unchanged.

### Aggregate-only logs (DEM-02)
- The demo's log lines (live turn, startup problem, store reset) carry mode, status, cost,
  latency, counts and secret names only: never a question, answer, passkey, hash, key, pid or
  IP. Proved by capturing every log record of a live turn, a refused unlock and a startup
  refusal.

### Off switch (DEM-03)
- The off switch is B2.3's pause switch on the shared store: proved end to end through both
  pages (staff pauses "All sessions"; the learner's next live question gets the paused answer
  and no model call). The operator's hard off switch is removing `DEMO_PASSKEY_HASH` (every
  page closes at the next check, D3.1) or rebooting with the startup check failing. Showing it
  on the live app is part of DEM-04.

### `scripts/smoke_demo.py` (DEM-04)
- `uv run --extra app python scripts/smoke_demo.py URL` opens the app's Streamlit stream with
  no passcode, runs the default page once and checks: a "Passkey" password input is shown, and
  no chat input, no "Teaching team" section, no answer card and no locked-startup text. Exit 0
  pass, 1 fail, 2 cannot connect. It sends no passcode and no key. The transport is injected;
  offline tests feed it Streamlit `ForwardMsg` protobufs (a locked page, an open page, a
  startup-locked page).

### `docs/deploy.md` (DEM-04)
The owner's step-by-step guide: the demo OpenAI project with a hard limit; passcode hashes
with `course-assistant passkey-hash`; the hosted KV; `make demo-cache` and committing
`demo/replay.json`; the Community Cloud app (repo, branch, main file, Python version,
secrets); revoking a passcode; the off switch; the post-deploy smoke check; and the attest
line to record as `OWNER D-n`.

### Harness (D-60; additive change to green W0.1)
- A catalog row whose note ends `(owner attest)` (before its trace bracket) is an owner-attest
  row in a code node: converge passes it while it is not green (shown as pending), and it can
  be green only with the proof `attest=OWNER D-n`, where that ruling opens `Attests <node>`
  (the eligibility check refuses otherwise, as for external nodes). Other rows are unchanged.

## Rules
- No network or API call in any test. No real secret value anywhere in the repo; test hashes
  are made from test codes in the test files.
- Fail closed: a missing secret, an equal or malformed hash, or an unreadable store serves no
  page and answers no question.
- No question, answer, passkey, hash, key, pid or IP in a log line, error or page text that is
  not the learner's own page.

## Out of scope
- The live deploy, the live off-switch demonstration and the live `make demo-cache` run
  (owner, DEM-04 and D3.1's pending item); the demo budget figures (owner).
