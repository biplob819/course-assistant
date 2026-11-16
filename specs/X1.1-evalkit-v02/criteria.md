# X1.1 acceptance criteria

**EKX-01** A system-under-test adapter receives the full `Case` (id, question, context, meta including `prior_turns`).
**EKX-02** `TaskConfig.scorers` may name plugin scorers; their results flow into records, summary and report.
**EKX-03** Slices: summary and report broken down by a case field (`rule`, `split`) with CIs.
**EKX-04** `evalkit-harness 0.2.0` released; course-assistant pins it and the import contract test passes.
