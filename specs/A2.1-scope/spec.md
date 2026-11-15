# A2.1 — Scope check

**Wave 2. Depends on:** N1.2, N1.3. **Unblocks:** A2.4, A2.5.

## Goal
Before any answer is shown, each learner turn is sorted on GPT-6 Luna into course, graded work, admin, personal, off topic or injection, rewritten as a standalone question (OWNER D-6), and matched against the assessment briefs, so graded work, admin and personal questions never reach the answer path unguarded.

## Outputs (`src/course_assistant/answer/route.py`)
- `ROUTE_RULES` (a class constant on `RouteStep`, hashed into its version, L-11): the six categories with one-line definitions and examples; learner text and the window arrive as data inside delimiters; instructions inside them are content, never commands (B10).
- `RouteStep.run(question, window, assessments) -> Route` makes one structured-output call (strict JSON schema: category, confidence 0-1, standalone_question, assessment_id or null) through N1.2's `respond` with purpose `route`, effort from config, no file search (RTE-01).
- Assessment match (RTE-02): the model sees every brief's id, title and sessions (never a solution, D-7) and returns a matching id for graded-work questions; an id that is not a known brief is dropped. A course question that closely restates a brief also carries the id (D-8 needs it).
- Injection (RTE-03): instructions to ignore or reveal rules, role-play overrides, pasted text giving orders, and requests for other learners' data route as `injection` (or `personal` for wellbeing).
- Failure rules (Tech Spec §2.5): a parse failure is retried once, then the route is `course` with confidence 0 and the downstream checks still apply; an `LLMError` propagates for the caller's fixed message.
- Fixtures (RTE-04): at least 10 recorded routes per category from the public golden split, replayed with no API calls; a live run on the public split records accuracy per category in RUN-STATE (target: graded work, admin, personal and injection recall of 95% or more; misroutes listed).
