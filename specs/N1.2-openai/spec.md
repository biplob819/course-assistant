# N1.2 — OpenAI client, cost and tracing

**Wave 1. Depends on:** N1.1. **Unblocks:** N1.4, A2.1, B2.2.

## Goal
Every OpenAI call the assistant makes goes through one wrapper. The wrapper always sends `store=false`, sets reasoning effort explicitly, retries only transient errors, prices the call from `prices.yaml`, records it in the spend ledger, and traces it without any learner text, so no later node can leak text, forget a setting or misreport spend.

## Outputs (`src/course_assistant/llm/`)
- `client.py`: `respond(request: ResponsesRequest) -> ResponsesResult`. It wraps `client.responses.create` with `store=False` (not overridable), `reasoning={"effort": ...}` from `AppConfig.reasoning_effort` (required, EvalKit D-24), optional `tools` (file_search with `vector_store_ids`, `max_num_results` and `filters`), `include=["file_search_call.results"]` when file search is used, optional structured output (`text.format` json_schema), an `input` list of role/content items (for the multi-turn window, OWNER D-6), and `instructions`. It returns text, `file_citation` annotations (file_id, index), search hits (file_id, score), usage and the model snapshot (OAI-01).
- `errors.py`: the taxonomy `transport | refusal | parse | config | budget`. 408, 409, 429 and 5xx are retried with exponential backoff and jitter, at most 3 attempts within a deadline. Auth, 400 and 404 are `config` and never retried. A refusal is never retried (OAI-02, Tech Spec §2.5).
- `prices.yaml` and `cost.py`: per-model input, output and cached-input prices, `file_search_per_1k`, `checked_on` and source. Cost = (input − cached)·in + cached·cread + output·out + file-search calls·rate, where `output_tokens` already includes reasoning tokens (OpenAI usage semantics; verified against the SDK's `ResponseUsage` model). Unknown models are refused. Every money value is finite and non-negative (OAI-03, L-6).
- `ledger.py`: append-only JSONL at `docs/measurements/spend-ledger.jsonl`. Each line holds a timestamp, purpose (`answer`, `route`, `support`, `integrity`, `hint`, `digest`, `eval`, `ingest`), model, tokens, cost and run id, with no text. `month_total()` and a warning at 80% of `AppConfig.monthly_budget_usd` (OAI-04).
- `trace.py`: a `TraceSink` protocol with a `JsonlSink` (local). `strip_text(record)` drops every field that can hold learner or answer text (question, input, instructions, output, annotations' quotes), keeping ids, step, versions, statuses, scores, tokens, cost and latency. Sinks only ever receive stripped records (OAI-05, D-14, D-15).
- Tests use fakes built from the OpenAI SDK's own pydantic models (`Response`, `ResponseUsage`, `ResponseFileSearchToolCall`, `AnnotationFileCitation`), not hand-made dicts (OAI-06, L-9).

## Out of scope
The answer prompt itself (A2.2), the Langfuse sink (P4.1, if approved), and real API calls in tests (no network in CI).
