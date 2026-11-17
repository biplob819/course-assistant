# Threat model

> **Draft (OWNER D-72).** Every control names the file, test or decision that proves it.
> Test paths are under `tests/`; `::` names a test function.

## Assets

| Asset | Where | Source |
| --- | --- | --- |
| Assessment briefs and solutions | Never indexed; beside held-out data | Tech Spec §6.2; `scripts/corpus_guard.py` |
| Learner conversations, escalations, consent | App store (SQLite) | Tech Spec §7; `PRIVACY.md` |
| Held-out golden split | Git-ignored `golden/heldout/`, manifest committed | Tech Spec §6.2; D-54 |
| Rules and canary (`ANSWER_RULES`) | Code | Tech Spec §5.1, §5.7 |
| Demo passcodes | Salted hash in the host secret store | OWNER D-48 |
| Spend (owner's OpenAI project) | Provider; daily KV counter | Tech Spec §8; D3.1 |

## Actors

Learner (honest or trying to get graded work done); outside visitor to the demo (with or
without their own key); prompt injector (via a question or via document text); curious
contributor reading the public repo; the builder tuning the system (a risk to held-out
discipline, not an attacker).

## Threats, controls, proof, residual risk

| Threat | Control | Proof | Residual risk |
| --- | --- | --- | --- |
| **Prompt injection** (in a question, in an earlier turn, or in retrieved text) | Fixed rules; learner and retrieved text go in as escaped data inside delimiters (B10); router flags injection; leak scan every turn | `tests/answer/test_answer.py::test_question_and_window_travel_as_escaped_json_in_data_delimiters`; `tests/answer/test_route.py::test_injection_fixtures_route_as_injection`; `tests/answer/test_ask.py::test_off_topic_and_injection_get_fixed_text_and_injection_is_flagged`; injection 1.000 on both halves (`docs/measurements/e2.5-report.md`) | Novel attacks outside the 20 + 60 paraphrased B10 cases (`DATA_CARD.md`); release value `PENDING (D-71)` |
| **Solution leakage** (direct or decomposed over many turns) | Scope check against the assessment list before answering, integrity check over the whole conversation (up to 20 turns) after, fail closed on doubt (D-8); solutions never indexed | `tests/answer/test_integrity.py` (e.g. `::test_a_conversation_over_the_size_cap_fails_closed_with_no_call`); `tests/harness/test_corpus_guard.py`; hint cache off (D-70) | First run: 4 of 56 held-out integrity probes failed, all multi-turn on one brief with a shared cached hint (`docs/measurements/e2.5-report.md`). Open until the release run: `PENDING (D-71)` |
| **System prompt / canary leak** | Deterministic leak scan for canary, pids and rules fragments, with stated disguise scope; dual-judge leak check offline | `tests/answer/test_ask.py::test_the_scan_finds_the_canary_a_pid_and_rules_fragments` and the canary disguise tests; D-51 | Three or more combined disguises are outside the scan (D-51), left to the judges |
| **Learner data exposure** (to the provider, other learners, the digest, traces) | Pseudonymous pids, `store=false`, traces hold no text (D-14), deletion with purge deadline, digest k ≥ 5 with consent only, label leak test | `PRIVACY.md`; `specs/B2.2-privacy/` (PRV-01..04, 18 verifier passes); `tests/digest/test_digest.py::test_a_topic_with_fewer_than_k_learners_never_appears`; D-58 | Provider abuse-monitoring retention up to 30 days (Tech Spec §7); reworded digest labels bounded only by k = 5 (D-58) |
| **Other learners' data via the model** | B8 rule; leak check | B8 cases in the golden set (Tech Spec §6.1); leak 1.000 held-out, 40/41 public (one likely judge false positive) | Small set: 10 cases a rule (`DATA_CARD.md`, Known gaps) |
| **Passkey abuse** (guessing, sharing, bypass with own key) | 6-12 digit code as salted PBKDF2 hash, constant-time compare, lockout after 5 failures in 15 min per session and IP, global cap for IP-less attempts, revocation by changing the hash, gate before every route including replay and own-key use | `tests/demo/test_demo.py::test_passkeys_are_salted_pbkdf2_hashes_compared_in_constant_time`, `::test_every_live_and_replay_route_requires_the_passkey_even_with_an_own_key`, `::test_changing_the_hash_revokes_every_session_and_removing_it_closes_the_gate`, `::test_rotating_sessions_with_no_ip_hit_the_global_cap`; `tests/app/test_team.py::test_the_learner_passkey_and_a_learner_session_never_open_the_staff_view`; OWNER D-45, D-48 | A shared code works until rotated; five concurrent right codes behind one IP can lock that IP for the window (`specs/D3.1-demo-guard/RUN-STATE.md`) |
| **Spend abuse** | Per-request cap, session rate limit, persistent daily counter (atomic), one cost number (L-39), `max_tool_calls` = 3, separate demo project with a hard provider limit, alarm at $135 of $150 | `tests/demo/test_demo.py::test_file_counter_persists_and_never_passes_the_cap_under_concurrency`, `::test_kv_counter_reserves_atomically_and_gives_back_past_the_cap`, `::test_one_cost_number_the_hold_reserved_is_the_executors_cap_and_settles_to_its_count`; `tests/deploy/test_deploy.py::test_hosted_tool_calls_are_capped_at_the_executors_bound`; `tests/llm/test_llm.py::test_the_spend_alarm_fires_at_135_of_the_150_cap`; OWNER D-64 | Demo daily cap values are placeholders until the owner sets them (`specs/D3.1-demo-guard/RUN-STATE.md`); hosted project limit is an owner step (D3.2 DEM-04, owner attest) |
| **Held-out leakage** (into tuning, the repo, or a rescore) | Held-out files git-ignored, manifest-pinned; exposure scan of tracked files; held-out scored once (tuning log hash chain, `heldout_scored`); bar tuning refuses held-out; used set retired (OWNER D-71) | `tests/golden/test_golden.py::test_no_heldout_question_appears_in_any_tracked_file`; `tests/eval/test_release.py::test_heldout_exposure_guard_reports_ids_only`; `tests/eval/test_calibration.py::test_tuning_log_hash_chain_catches_edits_and_heldout_scoring_blocks_changes`; `tests/eval/test_full_run.py::test_a_resumed_run_never_logs_heldout_scored_again`; `tests/eval/test_quality_d71.py::test_eval_live_refuses_a_held_out_subset_before_any_call`; D-54, D-55 | One case leaked once via a test and was retired (D-54); a deliberate history rewrite by a committer is outside a repo-local check (D-55) |
| **Course material or private terms in the public repo** | Corpus guard and secret scan in pre-commit and CI | `tests/harness/test_corpus_guard.py::test_live_repo_has_no_course_material_or_private_terms`, `::test_pre_commit_runs_both_scanners`; `tests/harness/test_secret_scan.py` | Disguises beyond those tested (`specs/W0.1-harness/RUN-STATE.md`) |
| **Release of a regressed change** | Release gate on held-out: every §6.4 gate, paired Holm regression, dual + certified calibration | `tests/eval/test_release.py`; D-57, D-59 | Weekly run only catches provider drift after the fact (Tech Spec §6.5) |
