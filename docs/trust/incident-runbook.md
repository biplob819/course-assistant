# Incident and kill-switch runbook

> **Draft (OWNER D-72).** Four steps: detect, pause, notify the data fiduciary, and a detailed
> report within 72 hours. Steps 1 to 3 below (pause, passkey, spend) are backed by code and
> tests. Step 4 (notify the fiduciary, the 72-hour report) is **owner process, no code**
> (`docs/trust/known-limits.md`). Live drills on the hosted demo are an owner step (D3.2
> DEM-04, `docs/deploy.md` §8 and §9). The DPDP and UK GDPR duties behind step 4 are in
> `docs/trust/dpdp-mapping.md` (breach intimation row).

## Detect

| Signal | Where | Source |
| --- | --- | --- |
| Spend alarm at $135 of the $150 cap | Ledger | `tests/llm/test_llm.py::test_the_spend_alarm_fires_at_135_of_the_150_cap`; OWNER D-64 |
| Any `_settle` exception | Audit and alert, no text | `tests/demo/test_demo.py::test_any_settle_exception_is_audited_and_alerted_without_text` |
| Leak scan or injection flag | Answer flags, traces (no text) | `tests/answer/test_ask.py::test_a_leaking_answer_becomes_not_covered_and_is_flagged`, `::test_off_topic_and_injection_get_fixed_text_and_injection_is_flagged`; Tech Spec §5.7 |
| Weekly release gate fails with no change on our side (provider drift) | CI | Tech Spec §6.5 |
| Purge job: a deletion request older than 7 days | The purge flags it (`Store.purge`; scheduled run is a pilot step, P4.2) | `tests/store/test_store.py::test_purge_flags_deletion_requests_pending_over_7_days` |
| Learner or teaching-team report | Escalation queue | `specs/B2.1-escalation/` |

## 1. Pause (kill switch)

1. Teaching team page, **Pause switches**: tick the confirm and press **Pause all sessions**, or
   pause one session (`docs/deploy.md` §8; `src/course_assistant/app/team.py` `set_pause`).
2. Every new question gets the fixed paused message with no model call and nothing stored
   (`tests/answer/test_ask.py::test_a_paused_scope_gives_the_paused_message_with_no_call_and_nothing_stored`).
3. A failed pause read counts as paused (`tests/answer/test_ask.py::test_a_pause_read_failure_fails_closed`).
4. The switch needs the staff passkey and is audited (`tests/app/test_team.py::test_pause_switches_list_all_and_each_session_and_need_the_confirm`; B2.3 TTV-04).

## 2. Passkey revocation

1. `uv run course-assistant passkey-hash` with a new code (reads without echo, prints only the hash).
2. Replace `DEMO_PASSKEY_HASH` in the host secret store (`docs/deploy.md` §9).
3. Every unlocked session is revoked at its next check; removing the hash closes the gate
   (`tests/demo/test_demo.py::test_changing_the_hash_revokes_every_session_and_removing_it_closes_the_gate`).

## 3. Spend cap

1. Guard refuses past the daily cap; counter persists across restarts (`tests/demo/test_demo.py`; `docs/deploy.md`).
2. Lower the daily cap, or remove the owner's key so only visitor keys work (RPL-02).
3. Last resort: the demo OpenAI project's hard monthly limit (RPL-03, `specs/D3.1-demo-guard/spec.md`).

## 4. Notify the data fiduciary and report (pilot; owner process, no code)

The operator is the processor; the programme provider is the data fiduciary and decides what
goes to the Data Protection Board and to learners (Tech Spec §7; `PRIVACY.md`, Roles).

1. Pause (step 1); preserve the audit log, which holds no question text (NFR7).
2. Notify the data fiduciary without delay: what was seen, when, and that the assistant is
   paused. Record the time; the 72 hours run from when the breach became known.
3. Detailed report to the fiduciary within 72 hours: what happened, when, how many learners
   (pid counts only, never text), which controls failed, the fix, and the test that now
   proves it. The fiduciary uses it for its own report to the Board and to affected learners
   (DPDP s.8(6); UK GDPR Art. 33 and 34).
4. Turn each finding into a golden case and a lesson (D-69; `docs/lessons.md`).
5. Unpause only after the release gate passes on the fix (Tech Spec §6.5).

## Off-switch evidence

- In code: the pause switch, the paused message and fail-closed reads are tested (step 1).
- On the live app: `PENDING (D3.2 owner attest)`. The hosted demo is not deployed yet; the
  owner attests the drill under D3.2 DEM-04 (D-60).
