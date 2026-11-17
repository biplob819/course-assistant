# Incident and kill-switch runbook

> **Draft (OWNER D-72).** Live drills on the hosted demo are an owner step (D3.2 DEM-04,
> `docs/deploy.md` §8 and §9). Steps 1 to 3 are backed by code and tests. Step 4 (notify the
> fiduciary, 72-hour report) is **owner process, no code** (`docs/trust/known-limits.md`).

## Detect

| Signal | Where | Source |
| --- | --- | --- |
| Spend alarm at $135 of the $150 cap | Ledger | `src/course_assistant/llm/ledger.py` (`WARN_AT`); OWNER D-64 |
| Any `_settle` exception | Audit and alert, no text | `specs/D3.1-demo-guard/RUN-STATE.md` |
| Leak scan or injection flag | Answer flags, traces (no text) | Tech Spec §5.7; `tests/answer/test_ask.py` |
| Weekly release gate fails with no change on our side | CI | Tech Spec §6.5 |
| Purge job: a deletion request older than 7 days | `make purge` fails | Tech Spec §7 |
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

## 4. Contain and notify (pilot; owner process, no code)

1. Pause; preserve the audit log (no question text, NFR7).
2. Notify the data fiduciary (the programme provider; Tech Spec §7) without delay.
3. Detailed report within 72 hours: what, when, which pids (counts only), controls that failed,
   the fix and the test that now proves it.
4. Turn each finding into a golden case and a lesson (D-69; `docs/lessons.md`).
5. Unpause only after the release gate passes on the fix (Tech Spec §6.5).

Off-switch test evidence on the live app: `PENDING` (owner attest, D-60).
