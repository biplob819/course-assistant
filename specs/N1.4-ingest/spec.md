# N1.4 — Ingest and vector store sync

**Wave 1. Depends on:** N1.2, N1.3. **Unblocks:** A2.2, E2.4.

## Goal
Only manifest files reach the cohort's OpenAI vector store, one file per slide or page with session, kind, locator and version attributes, and the store always matches the manifest: a removed file leaves the store (and OpenAI's file storage) on the next run, and a second run with no change makes no API calls.

## Outputs (`src/course_assistant/ingest/`)
- `split.py` (ING-01): `units_from_manifest(manifest, root)` reads each entry's file with python-pptx (one unit per slide: title, then body, then tables, in shape order; speaker notes only when `include_speaker_notes`) or pypdf (one unit per page), and agendas as one unit. Units under 3 words after the title are dropped and reported (image-only slides). The slide title prefixes every unit's text.
- `corpus.py` (ING-02): `CorpusStore` (a JSON file per corpus version under `corpus_store/`, git-ignored for real cohorts; the sample corpus store is rebuilt, never committed) holding `CorpusUnit`s. `unit_index` is 1-based in a stable order: session, then kind (slides, pre_read, agenda, post_session, faq), then manifest order, then locator number. The corpus version is the sha256 of the sorted unit versions. `resolve(locator)` maps `s02:slides:9` to its unit (D-30).
- `sync.py` (ING-03 to ING-05): `plan(manifest, store_state) -> Plan` (uploads, deletions, unchanged) and `apply(plan, client)` which uploads each new unit as a `.txt` file (`files.create(purpose="assistants")`), attaches it with `vector_stores.files.create(attributes={session, kind, locator, unit_index, manifest_id, version, corpus_version})`, waits for `status == "completed"` (a failed or cancelled file is an error), deletes removed units from the vector store **and** deletes the File object, then reconciles: the store's file list must equal the planned units, or exit 2.
- CLI (ING-06): `course-assistant ingest --config config.yaml [--dry-run]` prints the plan; without `--dry-run` it applies it. A second run with no manifest change makes zero write calls. Exit codes 0, 2 (config, reconcile), as Tech Spec §4.

## Rules
- The manifest's `sha256` must match each file before anything is read (a changed file without a manifest update is refused).
- Units carry no learner data; ingest never reads anything outside the manifest's paths.
- All OpenAI calls go through a thin `VectorStoreAPI` protocol so tests use fakes built from the SDK's own models (L-9); one live run against the sample course is recorded in RUN-STATE with its cost.
