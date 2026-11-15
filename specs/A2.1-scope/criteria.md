# A2.1 acceptance criteria

**RTE-01** `RouteStep` calls `respond` once with purpose `route`, a strict JSON schema, no file search, the prompt attached to the class and hashed into `route_version`, and returns a valid `Route` with a standalone question.
**RTE-02** For graded-work questions the route carries a known assessment id; an unknown id is dropped; briefs only (no solution text) reach the prompt.
**RTE-03** Injection patterns (ignore or reveal rules, role-play overrides, pasted orders, other learners' data) route as injection; learner and window text sit inside data delimiters in the request.
**RTE-04** Recorded fixtures (at least 10 per category) replay with no API calls; a parse failure retries once and then falls back to course with confidence 0; a live run's per-category accuracy is recorded.
**RTE-05** Each turn is rewritten as a standalone question from the window (the window reaches the request as data); the rewrite is what the route carries for the assessment match and the trace, and the fallback route keeps the learner's own words.
