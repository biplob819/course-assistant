# N1.2 acceptance criteria

**OAI-01** `respond` always sends `store=False` and an explicit reasoning effort, includes `file_search_call.results` whenever file search is used, passes filters and the input item list through unchanged, and returns text, citations (file id and index), search hits with scores, usage and the model snapshot, parsed from SDK response objects.
**OAI-02** Transient errors (408, 409, 429, 5xx, connection, timeout) are retried with backoff, at most 3 attempts and within a deadline; config errors (401, 403, 400, 404, missing key) and refusals are never retried; every outcome is mapped to the taxonomy, and the key never appears in any error text.
**OAI-03** `prices.yaml` covers every model in use with its source and check date; cost counts uncached, cached and output tokens (reasoning included once) and file-search calls; unknown models, negative or non-finite values are refused.
**OAI-04** Every priced call appends one ledger line with no text; the month total is correct across lines; crossing 80% of the monthly budget raises a warning.
**OAI-05** Trace records reach a sink only after `strip_text`; a test that feeds learner text through every field proves none of it reaches the JSONL file.
**OAI-06** Test fakes are built from the OpenAI SDK's own models, so field names and nesting match the real SDK.
