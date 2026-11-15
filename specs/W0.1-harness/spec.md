# W0.1 — Repo and harness

**Wave 0. Depends on:** —. **Unblocks:** N1.1, G0.1, X1.1.

## Goal
The repo can build itself node by node with the EvalKit harness (D-18): a node starts only when its dependencies are green, and turns green only when every gate passes and every proof resolves. The public repo can never take in course material, held-out eval data, secrets or private terms.

## Outputs
- uv project `course-assistant`, src layout `src/course_assistant/`, CLI `course-assistant --version`, MIT licence, ruff pre-commit, LF line endings (HAR-01).
- `.github/workflows/ci.yml`: lint, types, tests, build and the gate self-tests on every push and PR; Python 3.11–3.13 on Linux and macOS; a weekly schedule hook for the eval job (filled in at E2.4) (HAR-02).
- `scripts/verify_graph_eligibility.py`, `node_start.py`, `node_converge.py`, `status.py`, `secret_scan.py` ported from EvalKit, each with `--self-test` (HAR-03 to HAR-05, HAR-07).
- External nodes: `owner attest` in Verified by; rows take only `attest=OWNER D-n`, and no code node may use it (HAR-03, D-17).
- `node_converge.py` gates as Tech Spec §11.6, with gate 8 `evalreg`, gate 9 safety + privacy + corpus guard, gate 10 app smoke (HAR-05).
- Records: `docs/graph/graph.md` and `catalog.tsv` generated from the Tech Spec (31 nodes, 139 rows), `decisions.md` D-1 to D-22, `lessons.md` L-1 to L-15, `changelog.md` (HAR-06).
- `scripts/corpus_guard.py`: course-type files only under `sample_course/`; nothing under `corpus/`; nothing under `golden/heldout/` but `MANIFEST.json`; no text file holds a term from the git-ignored `.private-terms` (HAR-08).

## Out of scope
PyPI publishing (R3.1, OWNER D-21). Windows CI (the demo host is Linux; Tech Spec §10).

## Constraints
- Check-only commands never write (L-3). Every gate self-test requires the planted defect's identity (L-2).
- The private-terms list never enters the repo, and the guard never prints a term.
