# Known limits

> **Draft (OWNER D-72).** Gathered from scope decisions in `docs/decisions.md` and deferred
> should-fix items in `specs/*/RUN-STATE.md`.

## Scope limits (decided)

| Limit | Source |
| --- | --- |
| Leak scan promises each listed disguise alone and in pairs; three or more combined, or a fragment whose every anchor holds an unmapped letter, is left to the offline dual-judge leak check | D-51 |
| Digest label leak test refuses verbatim copies only; reworded copies, unmapped look-alikes, pids disguised with look-alikes and short ASCII questions inside spaceless text are bounded only by k = 5; a neutral label may be refused (safe failure) | D-58 |
| Tuning log guards accidental and unreviewed edits; a deliberate history rewrite by a committer is outside a repo-local check | D-55 |
| Release gate: app, answer filter and vector store attributes follow `CURRENT` until P4.1 wires them to the serving corpus | D-57 (4) |
| Streaming the answer is not built: both checks read the whole draft before anything is shown | D-61 (2) |
| Hint cache off until integrity labels clear it | D-70 |
| One small English sample course, builder-written questions, small zero-tolerance sets (B8, B9: 10 each) | `DATA_CARD.md`, Known gaps |
| Judging uncalibrated until the model-panel labels are scored; then kappa measures agreement with a model panel, not with a person, and same-vendor model families may share blind spots; a person's labels can replace the panel's later | OWNER D-76 |
| Runtime support check agrees weakly with the judges (kappa 0.15) | Tech Spec §6.4 |
| Provider retention up to 30 days for abuse monitoring unless zero data retention applies | Tech Spec §7 |
| Out-of-app notification of teaching-team replies decided at sign-off; the app holds no email | Tech Spec §7 |
| Image-heavy slides lose meaning as text | Tech Spec §12 |

## Deferred should-fix items

| Item | Source |
| --- | --- |
| N1.2 client edge cases (close per-attempt client if `with_options` raises; ledger after a failing `http.close`; unpriced line when reasoning > output; price unparseable-body calls from usage; refuse a non-finite clock); carried to A2.5 resilience | `specs/N1.2-openai/RUN-STATE.md` |
| B2.2 `_settle` faults (failed read of new requests returns early; failed `reopen_traces` plus failed sink delete; failed `delete_history` leaves rows for purge to flag only via traces); mitigated by audit-and-alert on any `_settle` exception in D3.1 | `specs/B2.2-privacy/RUN-STATE.md`, `specs/D3.1-demo-guard/RUN-STATE.md` |
| Concurrent right passcodes behind one shared IP can count as failures and lock that IP for the window | `specs/D3.1-demo-guard/RUN-STATE.md` |
| Stale rate-limit keys are pruned only by the 10,000-key bound | `specs/D3.1-demo-guard/RUN-STATE.md` |
| Demo daily cap and per-request hold are placeholders ($5.00, $0.50) until the owner sets the budget | `specs/D3.1-demo-guard/RUN-STATE.md` |

Items listed as deferred in D3.1 and then closed (`max_tool_calls`, trusted client IP) are done
in D3.2 (`specs/D3.2-demo-deploy/RUN-STATE.md`; `tests/deploy/test_deploy.py::test_hosted_tool_calls_are_capped_at_the_executors_bound`).

## Owner process, no code

These are duties the code does not implement. They are not presented as controls.

| Item | Source |
| --- | --- |
| Notice to the data fiduciary after an incident (TRU-04) | catalog TRU-04; `docs/trust/incident-runbook.md` |
| 72-hour detailed incident report (TRU-04) | catalog TRU-04 |
| Breach intimation to the Board and affected principals (DPDP s.8(6)) | `docs/trust/dpdp-mapping.md` |
| Processor agreement (DPDP s.8(2)) | OWNER D-47 |
| Grievance route, nomination, age check, notice languages, fiduciary contact | `docs/trust/dpdp-mapping.md` (gaps) |

## Open owner steps

Live demo deploy and attest (D-60, DEM-04); demo replay cache build (`specs/D3.1-demo-guard/tasks.md`);
optional: a person's calibration labels to replace the model panel's (OWNER D-76); fresh held-out run `PENDING (D-71)`.
