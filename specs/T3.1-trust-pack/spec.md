# T3.1 — Trust pack

**Wave 3. Depends on:** B2.2, E2.5, D3.2. **Unblocks:** T3.2, R3.1.

## Goal
Publish the trust documents (NFR2, NFR7): behavioral spec linked to cases and results, data card
and privacy notice, DPDP mapping with a UK GDPR column, incident runbook with the 72-hour
report, off-switch test evidence. Drafted early under OWNER D-72; nothing turns green until
E2.5 and D3.2 are green and the D-71 run exists.

## Outputs
- `docs/trust/model-card.md` — models, chosen setting, intended and out-of-scope use, results with status, limits.
- `docs/trust/threat-model.md` — assets, actors, threats with control, proof and residual risk.
- `docs/trust/evaluation-methodology.md` — golden set, held-out discipline, judges, calibration, gate, regression, tuning log.
- `docs/trust/known-limits.md` — scope decisions and deferred should-fix items.
- `docs/trust/incident-runbook.md` — detect, pause, revoke, spend cap, notify, 72-hour report.
- `docs/trust/behavioral-spec.md` (TRU-01), `docs/trust/dpdp-mapping.md` (TRU-03).
- Existing, to link: `PRIVACY.md`, `DATA_CARD.md` (TRU-02).

## Rules
Every factual claim points to a file, test or decision (L-8/L-16). Numbers from the final run
read `PENDING (D-71)` until it exists. No course material, learner data or held-out text.
