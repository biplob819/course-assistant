# X1.1 — EvalKit v0.2 upstream

**Wave 1. Depends on:** W0.1. **Unblocks:** E2.2.

## Goal
EvalKit gains what course-assistant's evaluation needs (OWNER D-11): a system-under-test adapter that receives the whole case, task-declared plugin scorers that run inside `evalkit run` and reach the report, and per-slice reporting. Built in the EvalKit repo under its own harness as node B4.1 (EvalKit D-59) and released as `evalkit-harness 0.2.0`.

## Outputs
- EvalKit repo: node B4.1 green (fresh-context verifier confirmed SUT-01, SUT-02, TSC-01, SLC-01, BWC-01; v0.1-shaped runs byte-identical), commit 43df77e, tag v0.2.0, release workflow green on macOS, Ubuntu and Windows, `evalkit-harness 0.2.0` on PyPI (published with the owner's go-ahead, 2026-11-16).
- This repo: the `eval` extra and the dev group pin `evalkit-harness>=0.2,<0.3`; `tests/eval/test_evalkit_contract.py` checks the import contract (adapter signature, `Case.meta`, `TaskConfig.scorers` and `slices`, `Record.task_scores`, `ModelSummary.task_scores`, `RunSummary.slices`).
