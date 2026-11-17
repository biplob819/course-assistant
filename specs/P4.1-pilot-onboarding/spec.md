# P4.1 — Pilot cohort onboarding

**Wave: pilot. Depends on:** G0.1, E2.4, D3.2. **Unblocks:** P4.2.

## Goal
Onboard one pilot cohort (Tech Spec §11, graph row P4.1): the private manifest ingested on the
agreed host, teaching-team cases and calibration relabel, SSO with the pid HMAC from platform
ids, notice and consent live, the Langfuse decision, and the app and retrieval wired to the
live corpus pointer (`serving_corpus`, two vector-store attribute sets; D-57 (4)).

P4.1 is an owner-attest node: PIL-01 to PIL-03 need the owner, the programme and the pilot
host, and are proved only by `OWNER D-n`. The serving-corpus wiring is code and is built first
as **groundwork** while D3.2 waits for its live deploy (DEM-04, owner attest): node_start
refuses P4.1 until D3.2 is green, so no catalog row moves and nothing flips green here.

## Groundwork: the app serves only the gate-approved corpus (D-57 (4))

### `src/course_assistant/ingest/serving.py`
- `ServedCorpus(store)()`: the live corpus named by `<store>/LIVE.json` (written only by the
  release gate, E2.4 `flip_live` / `apply_removals`) minus every unit, by locator and version,
  that is no longer in `CURRENT`. Read at every call (cached per (pointer, `CURRENT`) pair, as
  corpus files are content-addressed), so a flip or a removal reaches a running app at the
  next page run with no restart. A newer ingest is never served until the pointer names it.
- `ServingError` (fixed text, no path, no content) when there is no pointer, the pointer is
  not JSON, names no sha256 version, names a version the store does not hold, holds another
  cohort, or nothing of the live corpus is left.
- The view equals `eval.release.serving_corpus` (a test keeps them equal); it is rebuilt here
  because the eval module imports EvalKit, which the demo host does not install (D3.2's import
  closure test). `eval/release.py` is unchanged.

### `src/course_assistant/app/wiring.py` (learner page)
- `Serving`: the corpus served now. Pilot (`CA_CONFIG` set): `ServedCorpus` on the course's
  corpus store. Demo (`CA_CONFIG` unset, the demo cache present): the cache's replay corpus.
  **Choice:** the hosted demo on the sample course serves the replay corpus and needs no
  `LIVE.json` (its corpus never changes while the app runs and is the one its stored answers
  were recorded on); any pilot course needs the pointer.
- `ServedChat(LearnerChat)`: `locked_reason` (the page's first call on every run) re-reads
  the served corpus; with none, it returns the fixed `SERVING_LOCKED` and every method that
  passes the passkey check then refuses with it. The passkey gate is checked exactly as before.
- Each request's `Assistant` gets the corpus served at that moment, so session options, the
  answer filter value, citation resolution and source chips all use the served version.
- `VersionedSink`: every trace line carries the served corpus version id in the allowlisted
  `version` key (sha256 hex; `strip_text` keeps it) and nothing else about the corpus.
- `from_env` raises `WiringError(NO_LIVE)` for a pilot course with no servable corpus;
  `serving_problem(env)` gives the startup check a description with no path and no secret.

### `src/course_assistant/app/team_wiring.py` (staff page)
- `ServedTeam(TeachingTeam)`: the same served corpus, re-read each run; the Materials
  section's "Live corpus version" line shows the served id. No servable corpus, or one of
  another cohort than the manifest: the fixed `SERVING_LOCKED` after the staff passkey check.

### `deploy/streamlit_app.py`
- After D3.2's startup check and course check, `wiring.serving_problem` at every run: a pilot
  course with no or an unreadable pointer gets only D3.2's locked page (`deploy.LOCKED`).

## Not in this groundwork (reported, outside this lane)
- Retrieval with two vector-store attribute sets: `ingest.sync` tags each file with one
  `corpus_version` (`CURRENT`), and the answer filter (`answer/answer.py`, `answer/hint.py`)
  matches `corpus_version == <served version>`. While a candidate that adds or changes units
  waits for the gate, that filter matches no file, so answers fail closed (not covered),
  never ungated (a removal-only candidate is unaffected: its version is the served view's). Serving answers during that window needs a second attribute set written by
  sync and read by the answer filter: a change to `answer/` (another lane).
- The served version on the Ops tile itself: shown today in the staff Materials section;
  a line in the Ops section needs `team.py`/`team_page.py`.

## Rules
- No network or API call in any test. Fixed texts only: no path, pointer content, secret or
  course text in a page, log line or error. Nothing weakens D3.1/D3.2's passkeys or startup
  lock: the serving check only adds refusals.
