# P4.1 acceptance criteria

PIL-01 to PIL-03 are proved only by owner attest (`OWNER D-n` opening "Attests P4.1").
The serving-corpus groundwork below has no catalog row of its own yet; it is proved offline
(tests/serving: AppTest on the real entry and pages, the real wiring from the environment,
N1.2's scripted step client for a live turn; no network).

**PIL-01** Private manifest for one cohort ingested on the agreed host (owner attest).
**PIL-02** Teaching-team cases added to the golden set; teaching-team calibration relabel (owner attest).
**PIL-03** SSO, pid HMAC from platform ids, notice and consent live (owner attest).

Groundwork (D-57 (4), serving corpus):
- G1. A pilot page serves only the version named by `LIVE.json` minus removed units; a newer
  ingest that has not been gated is never served (sessions, Assistant corpus, answer filter
  value); the view equals `eval.release.serving_corpus`.
- G2. A removal is served at the next page run, before any flip; a removal-only flip by
  E2.4's `apply_removals` is served with no restart.
- G3. No `LIVE.json`, or one that cannot be read, in a pilot configuration: the entry shows
  only D3.2's locked page at every run, and a running page locks with fixed text; in demo mode
  the sample course's replay corpus is served with no pointer (tested choice).
- G4. The served version id is on the staff view ("Live corpus version") and in every trace
  line (`version`), as the id only.
- G5. Nothing weakens the passkeys or the startup lock (D3.2): a live pointer opens nothing by
  itself, the learner code never opens the staff view, a missing secret still locks.
