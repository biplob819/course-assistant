# DPDP Act 2023 mapping, with a UK GDPR column

> **Draft (OWNER D-72).** This maps the assistant's controls to the duties. It is not legal
> advice. Roles: the programme provider is the data fiduciary; the assistant is its processor
> under a written agreement (Tech Spec §7; OWNER D-47). Section numbers are the Act's;
> the Rules' detail (forms, timelines) is not mapped here. Paths are under `tests/` unless
> stated.

| Duty (DPDP 2023) | UK GDPR | Control | Proof | Status |
| --- | --- | --- | --- | --- |
| Notice before or with the consent request (s.5) | Art. 13 | Notice shown until consent to the current version; a new version asks again; states what, why, how long, where and how to delete | `privacy/test_privacy.py::test_notice_until_consent_for_the_current_version`, `::test_a_new_notice_version_asks_again`, `::test_notice_states_what_why_how_long_where_and_how_to_delete` | Met in code. Notice in languages of the Eighth Schedule: **gap** (English only) |
| Consent: free, specific, per purpose (s.6(1)) | Art. 6(1)(a), 7 | Per-purpose consent; an old notice version does not allow processing | `privacy/test_privacy.py::test_consent_is_per_purpose`, `::test_consent_for_an_old_notice_version_does_not_allow_processing` | Met |
| Withdrawal as easy as giving (s.6(4)) | Art. 7(3) | Withdrawal stops processing at once, separate from deletion | `privacy/test_privacy.py::test_withdrawal_stops_processing_at_once_and_is_separate_from_deletion` | Met |
| Purpose limitation for the digest (s.6, s.7) | Art. 5(1)(b) | Digest counts only learners consenting to `improve_course`; k ≥ 5 | `digest/test_digest.py::test_a_topic_with_fewer_than_k_learners_never_appears`; D-58 | Met |
| Processor only under a valid contract (s.8(2)) | Art. 28 | Processor agreement part of sign-off | OWNER D-47 (attest) | Owner process, no code |
| Reasonable security safeguards (s.8(5)) | Art. 32 | Pseudonymous pids, passkey gate, traces with no text, `store=false`, audit with no text | `answer/test_ask.py::test_no_learner_text_reaches_a_trace`; `store/test_store.py::test_audit_rows_hold_no_text`; `docs/trust/threat-model.md` | Met for the controls listed |
| Breach intimation to the Board and each affected principal (s.8(6)) | Art. 33, 34 | Runbook steps only | `docs/trust/incident-runbook.md` | **Gap: owner process, no code** (`docs/trust/known-limits.md`) |
| Erasure when purpose is served or consent withdrawn (s.8(7)) | Art. 5(1)(e), 17 | Retention periods applied by purge; pilot teardown | `store/test_store.py::test_purge_applies_each_retention_period`; Tech Spec §7 (`make teardown`) | Met in code; teardown is an operator step |
| Contact for questions (s.8(9)) | Art. 13(1)(a)-(b) | Fiduciary's contact in the notice | Tech Spec §7 | **Gap**: contact text set at pilot by the fiduciary |
| Right to access a summary (s.11) | Art. 15 | Learner sees their own history and team replies | `app/test_chat.py::test_history_shows_this_learners_turns_and_teaching_team_replies_labelled`; `store/test_store.py::test_history_is_only_the_learners_own` | Partly met: no export of processing summary or recipient list |
| Right to correction and erasure (s.12) | Art. 16, 17 | Delete one conversation or whole history; traces follow; purge flags requests over 7 days | `privacy/test_privacy.py::test_delete_removes_app_rows_and_trace_lines_and_records_completion`, `::test_delete_one_conversation_only`; `store/test_store.py::test_purge_flags_deletion_requests_pending_over_7_days` | Erasure met; correction: **gap** (no edit of stored turns) |
| Grievance redressal (s.13) | Art. 77 (complaint to ICO) | Escalation queue reaches the teaching team | `app/test_chat.py::test_a_handed_over_turn_is_queued_without_a_tap` | **Gap**: no dedicated grievance route or response time |
| Nomination (s.14) | — | None | — | **Gap**: owner process |
| Children: verifiable parental consent, no tracking (s.9) | Art. 8 | Programme is for adults (assumption) | None | **Gap**: no age check; must be confirmed by the fiduciary |
| Cross-border transfer (s.16) | Ch. V | Requests go to the configured OpenAI project | `PRIVACY.md` (Where data goes) | **Gap**: processing location is not stated in the notice; transfer basis is the fiduciary's |
| Logs for accountability | Art. 5(2), 30 | Access log one year, no text (NFR7) | `store/test_store.py::test_teaching_team_actions_and_deletions_are_audited`, `::test_audit_retention_does_not_depend_on_the_callers_timezone` | Met |
