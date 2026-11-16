# B2.5 — Learner chat page

**Wave 3. Depends on:** A2.5, B2.1, B2.2 (and D3.1's passkey gate and guard). **Unblocks:** D3.2.

## Goal
The page a learner (or a demo visitor) uses: pick a session or "All sessions", ask, read an
answer card whose source chips open the exact unit text, see plainly when the materials do
not cover a question and hand it to the teaching team with one button, read the first-use
notice, see and delete their history (US1, US2, US4, US9, Flow A and B). Every action sits
behind D3.1's passkey (OWNER D-45, D-48), and a visitor may bring their own OpenAI key for
the session only (CHT-06). The page is Streamlit (the `app` extra); all logic lives in a
plain module tested with fakes, and the Streamlit file is a thin view tested with
Streamlit's `AppTest`. Deploying it with the teaching page is D3.2.

## Outputs

### `src/course_assistant/app/chat.py` (logic, no Streamlit import)
- `ChatState`: one browser session. `token` (random, the passkey gate's session key),
  `pid` (random 16 hex per browser session, D-13), `scope` (a session id or None for
  "All sessions"), `conversation_id`, `turns` (what this page showed, with A2.5's `Turn`
  kept in memory so "Ask the teaching team" can attach the kept draft, B2.1), and
  `own_key` (an `OwnKey`). Its repr shows no text and no key.
- `OwnKey` (CHT-06): holds a visitor's key in memory only. Its repr and str never show it,
  it refuses to be pickled or copied (so no session-state persistence or cache can write it),
  `clear()` drops it, and it is read only at the moment one request's responder is built.
- `session_options(corpus)`: `["All sessions", "Session 1", ...]` from the live corpus;
  `scope_for(label)` maps a label back to a session id (None for "All sessions"), refusing
  anything else (CHT-01).
- `Card` and `card_for(turn_or_answer, corpus, *, paused=False)` (CHT-02, CHT-03, CHT-05):
  - every card has a status **text** label and a text icon, distinct per status, so status
    is never shown by colour alone: `answered` "Answer from the course materials",
    `not_covered` "Not covered by the course materials", `hint_only` "Hint (graded work)",
    `handed_over` "Sent to the teaching team", `error` "Could not answer", and a paused
    turn "Paused by the teaching team";
  - `answered` and `hint_only` cards carry the AI label ("AI-generated from the course
    materials") and the note "Check the cited slide or page before you rely on this.";
  - each citation becomes a `SourceChip` (label, session, file id, unit index and the unit's
    exact text from the corpus, D-3); a citation no longer in the corpus says so instead of
    guessing;
  - `can_escalate` is true only for a not-covered reply showing A2.5's `NOT_COVERED` text
    (off-topic and injection replies carry no handover offer, §2.2).
- `LearnerChat(gate, demo, *, assistant_for, store, traces, corpus)`: the one object the page
  calls. **Every method except `unlock` and `sign_out` calls `PasskeyGate.require(state.token)`
  first**, before reading the store, the corpus or the key (OWNER D-45: no page answers
  anything without the passkey, own key or not).
  - `unlock(state, code, ip=None)`: D3.1's `PasskeyGate.unlock`.
  - `choose_scope(state, label)` (CHT-01): sets the scope; a different scope starts a new
    conversation (a conversation belongs to one session scope, OWNER D-6).
  - `new_conversation(state)` (CHT-01): A2.5's `Assistant.new_conversation`, which closes
    the learner's open conversations, so the window is cleared (§2 window rules).
  - `needs_notice(state)`, `accept_notice(state, improve_course=False)`,
    `withdraw(state)` (CHT-04): B2.2's notice and consent; `answer_questions` is the purpose
    needed to ask, `improve_course` is optional and separate.
  - `ask(state, question) -> Card` (CHT-02, CHT-03): refuses until the current notice is
    accepted with `answer_questions` (B2.2 `allowed`), then calls D3.1's `LiveDemo.ask` with
    the scope, the conversation and the visitor key if one is held; a handed-over turn is
    queued there (B2.1). A guard refusal comes back as a card with the guard's readable reason;
    any other failure comes back as A2.5's NFR8 message, logged by exception type only.
  - `escalate(state, question_id)` (CHT-03): B2.1's `escalate_turn` on the turn this page
    holds (kept draft attached), else `escalate(..., "not_covered")`; a second tap returns
    the first escalation.
  - `history(state)`, `delete(state, conversation_id=None)`, `replies_waiting(state)`
    (CHT-04): B2.2's history and delete (app rows at once, the trace lines in every sink,
    the 7-day check behind it) and B2.1's reply count. Deleting the open conversation
    starts a new one.
  - `set_own_key(state, key)` / `forget_own_key(state)` (CHT-06): after the passkey only;
    the key is checked for shape (non-blank, no spaces, at most 200 characters) and held in
    the `OwnKey`.
  - `sign_out(state)`: locks the gate session (`lock_session`), clears the own key and the
    turns shown.
- `PerThreadStore(path)`: one SQLite `Store` per thread on the same file (Streamlit runs
  each rerun on a new thread and SQLite connections are bound to their thread).

### `src/course_assistant/app/wiring.py`
- `from_env()` builds a `LearnerChat` from named settings only: `CA_CONFIG` (the AppConfig
  YAML of an ingested course; the corpus store next to it), `CA_ASSESSMENTS` (default
  `sample_course/assessments.yaml`), `CA_APP_DIR` (default `runs/app/`: the app store,
  the trace file and the local spend counter), `DEMO_PASSKEY_HASH` (D3.1), `OPENAI_API_KEY`
  (the owner's key; when unset, only visitors' own keys answer), `DEMO_KV_REST_URL` and
  `DEMO_KV_REST_TOKEN` (the hosted counter when set). It never reads a file for a secret.
  A visitor key builds an OpenAI client for that request only.
- `set_factory(fn)` / `controller()`: the page asks `controller()`; tests install a fake
  factory.

### `src/course_assistant/app/learner_page.py` (Streamlit, thin)
- Run locally: `uv run --extra app --extra openai streamlit run
  src/course_assistant/app/learner_page.py` with `CA_CONFIG` and `DEMO_PASSKEY_HASH` set.
- Order on every rerun: passkey form (nothing else is drawn until the gate passes) → the
  sidebar (own-key form, sign out) → the notice until consent → the session picker and
  "New conversation" → the chat (cards, source chips as expanders holding the unit text,
  "Ask the teaching team") → "Your history" (turns, teaching-team replies under B2.1's
  label, delete one conversation or everything, withdraw consent).
- Accessibility (CHT-05): only Streamlit's own widgets (keyboard operable, forms submit with
  Enter), every input and button has a visible text label, no `unsafe_allow_html`, no
  custom HTML or script, no hidden labels; learner and answer text are shown with Markdown
  escaped, so no text can inject markup.

### `LiveDemo.ask(..., scope=None)` (additive change to green D3.1)
- `LiveDemo.ask` gains a keyword `scope` passed to `Assistant.turn` as the session filter
  (CHT-01 needs it; the default None is today's behaviour, D3.1 tests unchanged).

## Rules
- No network or API call in any test; scripted outputs through N1.2's `respond`, D3.1's
  gate and guard with a file-backed counter, a file-backed store, an in-memory trace sink.
- The own key never reaches a log, a trace, an audit row, the store, any file, an error,
  a repr, a pickle or Streamlit's widget state after submit (the key form clears on submit).
- No question or answer text in a log or trace (D-14); the page logs by exception type only.
- Tests read only `golden/public/` and the packaged golden replay fixture.

## Out of scope
- Hosting, secrets in the host, the teaching page and the replay-default demo mode (D3.2);
  SSO and the pilot host (P4.1).
