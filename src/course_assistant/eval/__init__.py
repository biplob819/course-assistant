"""The golden set as the EvalKit task `course-ta` (E2.2, D-10, D-11, Tech Spec §6).

`dataset` turns golden cases into EvalKit cases (corpus units as context), `system` is the
case-aware system adapter that answers through `ask()`, `judges` holds the dual-judge panel for
the probes, and `scorers` the task scorers registered with EvalKit. Importing `system` or
`scorers` needs the `eval` extra (`evalkit-harness`).
"""
