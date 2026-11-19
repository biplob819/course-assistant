# Cost and privacy

## What it costs

You pay your model provider directly. The project adds no fees and no hosted service. The
figures below are measured on the sample course, whose units are short; a course with longer
slides or pages costs more per question. Your own numbers are in the spend ledger
(`runs/ledger.jsonl`) after any live command.

| What | Measured cost | Source |
| --- | --- | --- |
| One learner question (route, answer, checks, hint) | about $0.006 | Held-out run, 19 Nov 2026 (D-83) |
| A held-out evaluation run, 215 cases | $3.12 | Same run |
| A full run on both halves, 429 cases, two judges | $10.68 | Tech Spec §9, 17 Nov 2026 |
| `eval-replay`, `doctor`, `init`, `ingest --dry-run`, the test suite | $0 | No network call |

A rough month for a pilot: 1,000 questions at $0.006 plus six evaluation runs at about $11
is about $70. The vector store also has a small daily storage charge in your OpenAI project.

How spending is kept in check:

- Every command that spends money prints an estimate first and needs `--yes`.
- Evaluation runs stop at `--max-cost`.
- `monthly_budget_usd` in `config.yaml` is checked by the cost guard.
- Set a hard monthly limit on the OpenAI project itself. That limit holds whatever the app
  does.

## Where your data goes

The short version: **no telemetry**, and learner and course data go only to the OpenAI
project you configure. The full statement, with every destination and how long each kind of
data is kept, is [`PRIVACY.md`](../../PRIVACY.md).

- Model calls use `store=false`. Learners are pseudonymous ids; no names or email addresses
  are sent.
- Traces hold ids, statuses, timings and costs, never question or answer text.
- Only files listed in your manifest are uploaded. Graded-work solutions must never be
  listed.
- Evaluation run folders (`runs/`) hold model answers. They are git-ignored; keep them that
  way.
- Run `make purge PILOT_END=YYYY-MM-DD YES=--yes` daily during a pilot. It applies the
  retention periods to the app store and fails if a learner's deletion request is still
  incomplete after 7 days.

Before a real cohort uses it, the institution that runs it decides the legal basis, the
notice to learners and the agreements. `PRIVACY.md` and
[`docs/trust/dpdp-mapping.md`](../trust/dpdp-mapping.md) describe what the software does to
support that. They are not legal advice.

## Keys

Keys come only from the environment or your host's secret store. The project never prints,
logs or writes a key value; `doctor` names a missing key, never its value. See
[`SECURITY.md`](../../SECURITY.md).
