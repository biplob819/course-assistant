# E2.4 tasks

- [x] 1. Spec and criteria written
- [x] 2. node_start GO
- [x] 3. Tests first (E2.3 should-fix items first, each with a test)
- [x] 4. Build
- [x] 5. Converge twice
- [ ] 6. Verifier
- [ ] 7. Proofs, green, commit
- [ ] Live (owner, pending): `make eval CONFIG=<ingested config.yaml> YES=--yes` on the held-out split after E2.3's calibration is certified; secrets in the protected `release` environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `HELDOUT_BUNDLE`) if the live CI job should run
