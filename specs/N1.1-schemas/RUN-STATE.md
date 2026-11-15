# N1.1 run state

**Now:** green

## History
- 2026-11-15 — spec and criteria written while W0.1 was in verification.
- 2026-11-15 — built from the drafted schemas; tests first (44 failing at start); D-28 records the spec refinements (Draft, AssessmentList, status rules).
- 2026-11-15 — verifier pass 1 confirmed SCH-01, SCH-02, SCH-04; disproved SCH-03 (slug-shaped free text in audit targets; withdrawal at the moment of consent) and SCH-05 (non-strict models coerced bools and numeric strings; staff named in approved_by; mutable ALLOWED_STATUS; contract test checked presence only). Fixed with regressions, plus should-fix items (YAML errors without content, normalised manifest paths, validated model_copy) (D-29, L-27).
- 2026-11-15 — verifier pass 2 confirmed SCH-01..05. Should-fix items fixed with regressions: FrozenDict copy and pickle, YAML 1.2 core scalars only, unknown keys withheld from errors, generated q-/c- ids (names and emails cannot be ids), all-or-none reply fields, canonical JSON (sorted map keys, UTC timestamps). Converge passed twice; rows flipped green.
