# Behavioral spec

> **Draft (OWNER D-72).** Each line names the test that proves it (paths under `tests/`).
> Held-out results per rule come from the fresh run: `PENDING (D-71)`. Rules B1 to B10: Tech Spec §5, §6.1.

## Before anything else

| Behaviour | Test |
| --- | --- |
| No question is answered, and no page is read, without the passkey, even with a visitor's own key | `demo/test_demo.py::test_every_live_and_replay_route_requires_the_passkey_even_with_an_own_key`; `app/test_chat.py::test_no_method_answers_or_reads_without_the_passkey_even_with_a_key` |
| The privacy notice shows until the learner consents to the current version | `privacy/test_privacy.py::test_notice_until_consent_for_the_current_version` |
| A paused scope gets the fixed paused message, with no model call and nothing stored | `answer/test_ask.py::test_a_paused_scope_gives_the_paused_message_with_no_call_and_nothing_stored` |
| Bad input is refused before any call or store | `answer/test_ask.py::test_bad_input_is_refused_before_any_call_or_store` |
| Another learner's conversation id is refused before any call | `answer/test_ask.py::test_another_learners_conversation_id_is_refused_before_any_call` |

## By route (router and answer start in parallel, D-9)

| Route | Behaviour | Test |
| --- | --- | --- |
| all | Route and answer start together; the answer is discarded unless the route is `course` | `answer/test_ask.py::test_route_and_answer_start_together_on_two_threads`, `::test_the_answer_is_discarded_unless_the_route_is_course` |
| course | Answer only with citations; L1 fail gives `not_covered` with no check calls; an unsupported sentence gives `not_covered` | `answer/test_ask.py::test_l1_failing_gives_not_covered_with_no_check_calls_and_keeps_the_draft`, `::test_an_unsupported_sentence_gives_not_covered` |
| graded work (B3) | A grounded or fixed hint, never the draft; unknown brief gives the generic hint with no call | `answer/test_ask.py::test_graded_work_gives_a_grounded_or_fixed_hint_and_never_the_draft`, `::test_graded_work_with_no_known_brief_gives_the_generic_hint_with_no_call` |
| off-topic, injection (B7, B10) | Fixed text; injection flagged | `answer/test_ask.py::test_off_topic_and_injection_get_fixed_text_and_injection_is_flagged`; `answer/test_route.py::test_injection_fixtures_route_as_injection` |
| admin, personal (B4) | Handed over and queued without a tap | `app/test_chat.py::test_a_handed_over_turn_is_queued_without_a_tap` |
| identity (B9) | Pretending to be staff is the identity route | `answer/test_route.py::test_pretending_to_be_staff_is_identity_in_the_rules` |
| unparseable route | Falls back to `course`; the checks decide | `answer/test_ask.py::test_a_route_reply_that_will_not_parse_falls_back_to_course_and_the_checks_decide` |

Finalize is total over every route and check combination: `answer/test_ask.py::test_finalize_is_total_over_every_route_and_check_combination`, `::test_finalize_matches_the_table_row_by_row`.

## By status

| Status | What the learner gets | What it never does | Test |
| --- | --- | --- | --- |
| `answered` | Answer, source chips with the exact unit text, AI label, "check the cited slide" note | Show an uncited or unsupported claim | `answer/test_ask.py::test_an_answered_turn_carries_citations_grounding_and_integrity`; `app/test_chat.py::test_an_answered_card_has_chips_with_the_exact_unit_text_ai_label_and_check_note` |
| `not_covered` | Fixed message and "Ask the teaching team" | Answer from outside knowledge | `answer/test_ask.py::test_an_unsupported_sentence_gives_not_covered` |
| `hint_only` | A hint and the session to revisit | Complete or draft a brief, across the whole conversation | `answer/test_ask.py::test_integrity_failing_gives_a_hint_on_the_verdicts_brief`; `answer/test_ask.py::test_the_window_is_the_last_six_turns_and_integrity_reads_every_reply` |
| `handed_over` | "Sent to the teaching team", queue entry | Answer admin or personal matters itself | `app/test_chat.py::test_a_handed_over_turn_is_queued_without_a_tap` |
| `error` | The fixed NFR8 message | Become a quiet status | `answer/test_ask.py::test_any_llm_error_on_a_needed_call_gives_the_nfr8_message`, `::test_a_non_llm_exception_propagates_and_is_never_a_quiet_status` |

Every status has its own text label and icon: `app/test_chat.py::test_every_status_has_its_own_text_label_and_icon`.

## Every reply

| Behaviour | Test |
| --- | --- |
| A reply holding the canary, a pid or 9+ words from the rules becomes `not_covered` and is flagged (scope: D-51) | `answer/test_ask.py::test_a_leaking_answer_becomes_not_covered_and_is_flagged`, `::test_eight_words_from_the_rules_are_not_a_leak_but_nine_are`, `::test_a_leaking_hint_becomes_not_covered` |
| Learner text and the window travel as escaped data in delimiters | `answer/test_answer.py::test_question_and_window_travel_as_escaped_json_in_data_delimiters` |
| No learner text reaches a trace | `answer/test_ask.py::test_no_learner_text_reaches_a_trace` |
| The window holds only this learner's turns of this conversation | `answer/test_ask.py::test_the_window_holds_only_this_learners_turns_of_this_conversation` |

## Results per rule

| Rule | Held-out result |
| --- | --- |
| B1 grounded / locator | `PENDING (D-71)` (first run 0.693 / 0.800, `docs/measurements/e2.5-report.md`) |
| B2, B4, B7 handover | `PENDING (D-71)` (first run 1.000) |
| B3 integrity | `PENDING (D-71)` (first run 52/56) |
| B8 leak, B9 identity | `PENDING (D-71)` (first run 1.000, 1.000) |
| B10 injection | `PENDING (D-71)` (first run 1.000) |
