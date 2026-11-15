# N1.1 — Schemas and config

**Wave 1. Depends on:** W0.1. **Unblocks:** N1.2, N1.3, N1.5, E2.1 (and through them everything else).

## Goal
Every record the assistant reads, writes or sends is a typed, deeply immutable pydantic model, exactly as the Tech Spec §3 lists, so later nodes cannot pass loose dicts, cache or persist objects that change after creation, or store a learner's name or email by accident.

## Outputs (`src/course_assistant/schemas/`)
- `_base.py`: `Model` (frozen, `extra="forbid"`, finite floats), `Versioned` (required `schema_version`, major "1"), strict scalar types, `FrozenDict`, `deep_freeze`, `Pid` (16 lowercase hex, D-13), `Timestamp` (timezone-aware). Patterns follow EvalKit's `schemas/_base.py` (D-26, L-4).
- `corpus.py` (SCH-01): `ManifestEntry`, `Manifest` (unique `manifest_id`s, unique paths), `CorpusUnit` (1-based `unit_index`), `Assessment` (briefs only: the model has no solution field and refuses one, D-7).
- `request.py` (SCH-02): `Conversation`, `Question`, `Route`, `Citation`, `SearchHit`, `GroundingResult`, `Answer`.
- `people.py` (SCH-03): `Escalation`, `Consent`, `DeletionRequest`, `DigestRow`, `PauseState`, `AuditEvent`.
- `config.py` (SCH-04): `AppConfig`, `GoldenCase`, and `load_app_config(path)` / `load_manifest(path)` YAML loaders whose errors name the file and the field, refuse duplicate keys and non-UTF-8 files.
- `__init__.py` exports every model above (SCH-05).

## Invariants (each one a test)
- `Answer.status == "answered"` needs at least one citation; `not_covered`, `handed_over` and `error` carry none shown to the learner (citations empty).
- `Route.assessment_id` is set only when `category` is `graded_work` or `course`.
- `Escalation`: `status == "replied"` needs `reply_text`, `replied_by` and `replied_at >= created_at`; `open` has none of them.
- `Consent.withdrawn_at`, when set, is after `given_at`; `purposes` non-empty and unique.
- `DeletionRequest`: completion timestamps are not before `requested_at`.
- `DigestRow.count >= 1`; the k-suppression itself is B2.4's job.
- `PauseState.scope` is `"all"` or a session id `s\d\d`.
- `AuditEvent` has no free-text field beyond `action` (a fixed vocabulary) and `target` (an id pattern), so question text cannot be logged (NFR7, H9).
- `GoldenCase`: `B1` needs `gold_units`; `B3` needs `assessment_id`; `B8`/`B10` need a `canary`; `expected_status` must match the rule's allowed statuses (Tech Spec §6.1); `prior_turns` alternate and are non-blank.
- `Question.learner_pid` and every other learner reference are `Pid`, never free text: an email-looking value is refused (NFR3).
- `AppConfig.reasoning_effort` must name both `answer_model` and `check_model` (EvalKit D-24: set explicitly, never default).

## Out of scope
Storage (N1.5), OpenAI calls (N1.2), manifest file hashing against disk (N1.4).
