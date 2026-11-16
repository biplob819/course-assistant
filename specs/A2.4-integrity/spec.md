# A2.4 — Integrity guard and hints

**Wave 2. Depends on:** A2.1, A2.2. **Unblocks:** A2.5.

## Goal
Stop graded work leaving the assistant, including work assembled over several innocent turns
(finding C4, OWNER D-6, D-8), and give the learner a hint instead of a solution (FR7, B3). Two
modules: `integrity.py` (the post-answer check) and `hint.py` (the hint step and the fixed
fallback hint). Finalize and the wiring into `ask()` are A2.5.

## Outputs

### `src/course_assistant/answer/integrity.py`
- `IntegrityStep(config, responder).run(new_reply, earlier_replies, assessments) ->
  IntegrityResult` (INT-02). One structured-output `respond` call, purpose `integrity`, the check
  model and its configured effort, no file search, `INTEGRITY_RULES` as instructions.
- Data block: one JSON document inside `<data>` delimiters (angle brackets escaped, as A2.1):
  `briefs` (id, title, brief, sessions of every assessment in the list), `earlier_replies` (every
  earlier assistant reply in this conversation, oldest first, plain text) and `new_reply`.
  Learner turns are not sent: the check judges what the assistant has written (D-8), and leaving
  learner text out means a learner cannot speak to the judge.
- Briefs: every brief in the list. §5 point 5 says "every brief for the sessions they touch";
  earlier replies are stored as plain text without citations, so the sessions they touched are
  not reliably known, and a narrower set could miss the brief being assembled. All briefs is a
  superset, and the sample course has nine (D-40).
- Verdict schema: `verdict` in `clear | completes | unsure`, `assessment_id` (a known id or
  null). `clear` with an id, or an unknown verdict, is a parse failure. The check passes only on
  `clear`; `completes` and `unsure` fail (D-8: fail closed). An unknown id on `completes` is
  dropped to null and still fails.
- Parse failure (wrong shape, refusal, `LLMError` kind `parse`): retried once, then the verdict
  is `unreadable` and the check fails. Any other `LLMError` propagates (Tech Spec §2.5).
- Whole conversation (INT-05): `earlier_replies` is never trimmed to `window_turns` or
  `window_tokens`; every reply goes in whole. `MAX_TURNS = 20` assistant replies including the
  new one, and `MAX_CHARS = 120_000` characters over all replies: past either the verdict is
  `too_long` and the check fails with no call, so nothing is ever cut out of what is judged
  (A2.5 caps a conversation at 20 turns and offers a new one; D-40).
- Shape checks before any call: `new_reply` a non-empty string, `earlier_replies` a sequence of
  strings (a generator is read once), `assessments` an `AssessmentList`.
- `IntegrityResult`: `passed`, `verdict` (`clear | completes | unsure | unreadable |
  too_long`), `assessment_id`, `replies_checked`, `calls`, `cost_usd`, `latency_ms`,
  `integrity_version`. `passed` is true exactly when the verdict is `clear`.
- Version: `INTEGRITY_RULES`, the schema and both caps are class constants hashed into
  `integrity_version` (`integrity-<12 hex>`), Tech Spec §4 and L-11.

### `src/course_assistant/answer/hint.py`
- `HintStep(config, corpus, responder).run(assessment, earlier_replies, assessments) -> Hint`
  (INT-01). One `respond` call, purpose `hint`, the answer model and its configured effort,
  `HINT_RULES` as instructions, `vector_store_ids=[config.vector_store_id]`,
  `max_num_results` from config, results included.
- Data block: `{"brief": {"title": ..., "sessions": [...]}}` only, built from those two fields
  (so a subclass or object carrying any other field, such as a solution, cannot add it: D-7).
  Not the brief text, the learner's question, the window or anything else.
- Filter: always `corpus_version == live`; one session gives `and[live, eq session]`, several
  give `and[live, or[eq s1, eq s2, ...]]`. If any of the brief's sessions is not in the live
  corpus, no call is made and the fixed hint is returned.
- Ground L1 for hints: not a refusal, non-empty text, `GroundStep.citations_resolve` (every
  citation and mark a unit of the live corpus, nothing unresolved, at least one citation),
  every citation in one of the brief's sessions, and the top hit score among the brief's
  sessions at least `grounding_bar`. L2 is not run on hints (§2.2 names L1 only).
- Integrity: the hint is checked as the new reply with `earlier_replies` (INT-02 step).
- Fixed hint (INT-03): when L1 or integrity fails, the hint text is `FIXED_HINT` filled with the
  brief's title and its sessions as "Session N" ("Session 1", "Session 1 and Session 2",
  "Session 1, Session 2 and Session 3"), with no citations and no hits. `HINT_RULES` and
  `FIXED_HINT` are class constants hashed into `hint_version`. `HINT_RULES` carries A2.2's canary
  so A2.5's leak scan covers hints too.
- `Hint`: `text`, `citations`, `hits`, `fixed`, `reason` (`grounded | ground_failed |
  integrity_failed | unknown_session`), `integrity` (the `IntegrityResult` or none when no call
  was made), `hint_version`, `cost_usd`, `latency_ms` (sum over the hint and integrity calls).
- `LLMError` of any kind from the hint call, and non-parse errors from integrity, propagate.

## Decomposition fixtures (INT-04)
Built in the test from the public golden split only (`golden/public/cases.jsonl`, every B3 case
with `prior_turns`; 20 cases over 5 briefs). Each is replayed turn by turn: the prior assistant
replies, then an assembling reply. The test proves the plumbing (every earlier reply and the
case's brief reach the request at every turn; a `completes` or `unsure` verdict at the
assembling turn fails and the hint replaces the draft). Whether Luna actually says `completes`
there is a live measurement, which goes through `ask()` and so belongs to A2.5's smoke run
(as D-38 did for GRD-04). Held-out cases are never read.

## Rules
- No solution text in any prompt (D-7): integrity sees briefs only; hints see title and
  sessions only.
- No learner text reaches traces (N1.2 strips); no step adds trace fields with text.
