# RUN-STATE X1.1

- 2026-11-16 — built in EvalKit as B4.1 (D-59); its fresh-context verifier confirmed all five criteria and byte-identical v0.1 output against ca22b5c; breaking changes for some v0.1 tasks documented in EvalKit changelog and docs/versioning.md. Owner said "Publish"; tag v0.2.0 pushed, release workflow green (checks, publish, smoke on three OSes); PyPI shows 0.2.0. Pinned here (>=0.2,<0.3) with a 5-test import contract. GREEN. Deferred EvalKit should-fix: slice value collisions, an OverflowError that names no scorer, a price warning for system aliases.
