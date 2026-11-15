# N1.1 acceptance criteria

**SCH-01** `ManifestEntry`, `Manifest`, `CorpusUnit` and `Assessment` validate the Tech Spec §3 fields; manifest ids and paths are unique; `unit_index` is 1-based; `kind` is one of the five kinds; an `Assessment` with any solution-like field is refused.
**SCH-02** `Conversation`, `Question`, `Route`, `Citation`, `SearchHit`, `GroundingResult` and `Answer` validate the §3 fields and their invariants (answered needs a citation; non-answers carry none; route assessment id only for course or graded work; `turn_index` 1-based).
**SCH-03** `Escalation`, `Consent`, `DeletionRequest`, `DigestRow`, `PauseState` and `AuditEvent` validate the §3 fields and the timestamp-order and status invariants; `AuditEvent` cannot hold free text.
**SCH-04** `AppConfig` and `GoldenCase` validate, including the per-rule golden case invariants; the YAML loaders name the file and field on error, and refuse duplicate keys, non-UTF-8 and non-mapping files.
**SCH-05** Every model is deeply immutable and hashable, rejects unknown fields, non-finite floats, bool-as-int and naive datetimes; every persisted top-level model requires `schema_version`; learner references are 16-hex pids; the package exports exactly the models in an independent list taken from the Tech Spec §3 (L-4, L-10).
