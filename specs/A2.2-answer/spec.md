# A2.2 — Answer step

**Wave 2. Depends on:** N1.4. **Unblocks:** A2.3, A2.4.

## Goal
Answer a course question from the cohort's own materials only: one Responses call on the answer model with file search over the cohort vector store, filtered to the live corpus version (and the picked session), with every citation mapped back to the slide or page it came from.

## Outputs (`src/course_assistant/answer/answer.py`)
- `AnswerStep(config, corpus, responder).run(question, window, session=None) -> Draft` (ANS-01): one `respond` call with purpose `answer`, the answer model and its configured effort, `vector_store_ids=[config.vector_store_id]`, `max_num_results=config.max_num_results` (8), `include=["file_search_call.results"]` (N1.2 always sets it with file search).
- Filters (ANS-02): `corpus_version == corpus.corpus_version` always; when a session is picked, `and session == <id>`; one compound `and` filter, never a session filter alone.
- Citations (ANS-03): each `file_citation` maps through the corpus (`CorpusUnit.file_id`) to a `Citation` (file id, unit index, label, session), deduplicated in first-cited order. A citation to a file not in the live corpus is kept out of `citations` and counted in `unresolved` (Ground L1 fails on it in A2.3). Search hits map the same way to `SearchHit`s; an unmapped hit is dropped and counted.
- Rules (ANS-04): `ANSWER_RULES` (B1–B10 in plain words) is a class constant with a canary line (`CANARY`, a fixed random token), hashed into `rules_version`; the question and window go in as one JSON document inside `<data>` delimiters (as in A2.1, angle brackets escaped); earlier assistant replies go in as plain text with no citations (Tech Spec §2, window rules).
- `Draft`: text, citations, hits, unresolved count, refusal flag, rules_version, cost and latency from the `ResponsesResult`. The draft is never shown by this step; Finalize (A2.5) decides.

## Rules
- No outside knowledge: the rules say to answer only from retrieved course text, and to say it is not covered otherwise; A2.3 enforces it.
- `LLMError` propagates (the caller shows the NFR8 message); no retry here beyond N1.2's.
- No learner text reaches traces (N1.2 strips; this step adds no trace fields with text).
