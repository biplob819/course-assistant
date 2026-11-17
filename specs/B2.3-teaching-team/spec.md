# B2.3 — Teaching-team view

**Wave 3. Depends on:** B2.1, B2.4 (and B2.2's privacy rules, N1.5's store, audit and pause,
D3.1's passkey gate, B2.5's page patterns). **Unblocks:** D3.2.

## Goal
The page the teaching team uses (US5, US6, US7, Tech Spec §8 and §10): the escalation queue
with question, reason, the assistant's draft and its sources, a reply box, flag gap and close;
the weekly digest; a read-only manifest view with the live corpus version; pause switches per
session and for all, with a confirmation; and an ops tile. It sits behind its **own** passkey
(Tech Spec §8 "behind its own passcode", OWNER D-45 and D-48), so a learner's passkey or
unlocked session never reaches it, and every view and action is audited (NFR7). As in B2.5,
all logic lives in a plain module tested with fakes, and the Streamlit file is a thin view
tested with `AppTest`. Deploying it next to the learner page is D3.2.

## Outputs

### `src/course_assistant/app/team.py` (logic, no Streamlit import)
- `STAFF_SECRET = "STAFF_PASSKEY_HASH"`; `staff_hash(env=None, secrets_store=None)`: a
  `load_hash` for D3.1's `PasskeyGate` that reads `STAFF_PASSKEY_HASH` at every check (so
  changing it revokes every staff session) and returns None (gate closed) when it is unset or
  equal to the learner hash `DEMO_PASSKEY_HASH`.
- `TeamState`: one browser session: `token` (random, the staff gate's session key, never the
  learner page's token, which lives under another session-state key) and `role` (a role id,
  `instructor-1` by default, written to the audit log; never a name). Repr shows nothing.
- `TeachingTeam(gate, *, store, corpus, manifest, config, role="instructor-1",
  learner_hash=None, spend=None, now=None)`: the one object the page calls. **Every method
  except `unlock`, `sign_out` and `locked_reason` calls `gate.require(state.token)` first**,
  before reading the store, the corpus or the manifest; a test enumerates the public methods
  so a new one must refuse too.
  - `unlock(state, code, ip=None)`: D3.1's `PasskeyGate.unlock` on the staff gate. If the
    code also opens the learner hash (`learner_hash`, the same code under another salt) the
    session is locked again and the page stays closed: the staff passkey must differ. A
    success is audited `staff_unlock`; `sign_out` locks the session, gives a new token and is
    audited `staff_sign_out`.
  - `queue(state, status="open")` (TTV-01): B2.1's `team_queue` (audits `view_queue`) as
    `QueueRow`s: question id, question text, session label, reason label, status icon and
    label (text, never colour alone), the kept draft text, its sources as B2.5 `SourceChip`s
    with the exact corpus unit text (a citation no longer in the corpus says so), or the
    closest search hits (top 3 by score) when the draft cites nothing, and the reply, if any.
    No learner pid is read or shown. `status` is one of B2.1's statuses or None for all.
  - `reply(state, question_id, text)`, `flag_gap(state, question_id)`,
    `close(state, question_id)` (TTV-01): B2.1's functions with the staff role as instructor
    (each audited by B2.1); an `EscalationError` becomes a readable refusal and nothing changes.
    Each takes `seen=` (the status the page showed): a question deleted since is refused
    (`GONE`), one whose status changed since is refused (`CHANGED`). The page keys every
    per-row widget by the question id (verifier pass 2). A queue row that cannot be built
    after the `view_queue` audit gives `READ_FAILED`.
  - `weeks(state, n=8)` and `digest(state, week)` (TTV-02): the current ISO week and the
    `n - 1` before it (no audit: it reads nothing from the store); the stored B2.4 rows of a week (N1.5 `Store.digest`), filtered again to
    `count >= max(K_MIN, config.digest_k)` (defence in depth), as `DigestLine`s (session
    label, topic, count, gaps). Audited `view_digest` (target none).
  - `manifest_view(state)` (TTV-03): `ManifestView` with the cohort, the live corpus version,
    one `ManifestLine` per entry (id, session, kind, path, licence, approved by, approved at,
    speaker notes, units in the live corpus) and the live units whose manifest id has no entry.
    Audited `view_manifest`. Nothing in this module writes the manifest or the corpus.
  - `pauses(state)` and `set_pause(state, scope, paused, *, confirmed)` (TTV-04): one
    `PauseLine` for "all" and per live-corpus session (its own row, N1.5
    `Store.pause_state`; a read failure shows as paused), audited `view_pauses`; if the
    store will not open or the audit cannot be written, every scope shows as paused;
    `set_pause` refuses unless
    `confirmed is True` and the scope is "all" or a live session, then calls N1.5's
    `set_pause` (pause row and `pause`/`resume` audit in one transaction).
  - `ops(state)` (TTV-05): `OpsTile` with the stored question count, answers by status, the
    handover share (questions with an escalation, or answered `handed_over`, over questions),
    the nearest-rank p95 of `Answer.latency_ms` (None with no answers), and spend to date in
    USD (`math.fsum` of stored answers' `cost_usd`, plus `spend()` when given, e.g. the spend
    ledger's month total). Numbers only. Audited `view_ops`.

### `src/course_assistant/app/team_wiring.py`
- `from_env()` builds a `TeachingTeam` from named settings only: `CA_CONFIG` (AppConfig YAML,
  corpus store next to it), `CA_MANIFEST` (default `manifest.yaml` next to the config),
  `CA_APP_DIR` (the same app store as the learner page, default `runs/app/`),
  `STAFF_PASSKEY_HASH` (this page's gate), `DEMO_PASSKEY_HASH` (only to refuse a staff
  passkey equal to the learner one), `CA_STAFF_ROLE` (default `instructor-1`). No secret is
  read from a file. `set_factory(fn)` / `controller()` as in B2.5.

### `src/course_assistant/app/team_page.py` (Streamlit, thin)
- Run locally: `uv run --extra app streamlit run src/course_assistant/app/team_page.py` with
  `CA_CONFIG` and `STAFF_PASSKEY_HASH` set.
- Order on every rerun: the staff passkey form (nothing else is drawn until the gate passes)
  → the sidebar (sign out) → a "Section" picker (Queue, Digest, Materials, Pause switches,
  Ops) → only the picked section is read and drawn, so only what is shown is audited.
- Queue: a status picker, then per row the question, reason, status, draft, "Source n"
  expanders holding the unit text, the reply, a reply form ("Reply to the learner",
  "Send reply"), "Flag a gap in the materials" and "Close". Digest: a week picker and one line
  per row, or "No digest for this week.". Materials: the corpus version and one line per
  entry. Pause switches: per scope its state, a "Yes, …" confirm box and a button enabled
  only when it is ticked. Ops: `st.metric` tiles and the status mix.
- Accessibility and literal text, as B2.5 CHT-05: only Streamlit's own widgets with visible
  labels, no `unsafe_allow_html`, no custom HTML; learner, model, teaching-team and course
  text (questions, drafts, replies, unit text, topic labels, manifest paths and licences)
  reach the page only through `st.text`.
- The typed passkey is dropped from the widget state on the next run (B2.5 pattern).

### Changes to green nodes (additive, behaviour unchanged)
- N1.1: `AUDIT_ACTIONS` and `AuditAction` gain `view_ops`, `view_pauses`, `staff_unlock`,
  `staff_sign_out`.
- N1.5: `Store.pause_state(scope) -> PauseState | None` (the scope's own row; the key and
  record must agree).

## Rules
- Every view and action writes exactly one audit row; its target is a question id, a
  session scope (`s01`, ..., `all`) or none. A store that will not open, or an audit that
  cannot be written, gives a fixed refusal (logged by exception type), never a crash; unlock
  is locked again when its audit cannot be written (verifier pass 1).
- No network or API call in any test. No learner pid, question text or reply text in any
  audit row, log line or error (D-14, NFR7); the page logs by exception type only.
- The staff page never uses the learner gate or the learner session-state key; the learner
  page never uses the staff gate.
- Tests read only `golden/public/` and committed fixtures.

## Out of scope
- Hosting, secrets in the host and the replay-default demo (D3.2); SSO and per-person staff
  accounts (P4.1); running the paid `make digest` from the page (the page shows stored rows);
  editing the manifest (it changes only by a reviewed commit and `make ingest`, US7).
