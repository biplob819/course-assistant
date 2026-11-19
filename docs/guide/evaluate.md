# Evaluate your course

An assistant that answers from course materials is only as trustworthy as its measured
results on that course. This guide covers writing a golden set, running it and reading the
release gate. The method behind it is in Tech Spec §6 and
[`docs/trust/evaluation-methodology.md`](../trust/evaluation-methodology.md).

> **Draft (R3.1).** The evaluation scripts live in `scripts/` and read the golden set from
> fixed paths in this repository (`golden/public/` and the private `golden/heldout/`). To
> evaluate your own course today, work in a fork or a private copy of the repository. A
> packaged `course-assistant` evaluation command is not built yet.

## 1. Write golden cases

A golden case is one line of JSON: a question, the status the assistant should return and,
for a course question, the slide or page that answers it. A sample-course example:

```json
{"id": "ta-b1-001", "rule": "B1", "question": "Which three questions should I ask about a user problem before choosing a model?", "expected_status": "answered", "gold_units": ["s01:slides:3"], "prior_turns": [], "session_scope": null, "split": "tuning", "schema_version": "1.0", "corpus_version": "..."}
```

`s01:slides:3` is slide 3 of session 1. The schema is `GoldenCase` in
`src/course_assistant/schemas/config.py`, and [`golden/README.md`](../../golden/README.md)
explains each rule. Cover every rule, not just course questions:

| Rule | What to write |
| --- | --- |
| B1 | Questions the materials answer, including follow-ups that need earlier turns |
| B2, B7 | Topics the course does not cover, including true facts it never mentions |
| B3 | Requests to complete graded work, including piece-by-piece attempts |
| B4 | Admin and personal requests that belong with the teaching team |
| B8 | Questions about other learners |
| B9 | Attempts to make it claim to be a person or speak for the institution |
| B10 | Prompt injection, in the question or in a document |

**Write the cases before you tune anything**, so they are not shaped by the system they
measure.

## 2. Split it, and keep the held-out half private

Split the set in two by group: every question on one slide, and every probe on one
assessment with its paraphrases, stay on the same side. Tune prompts and the grounding bar
on the **public** half only. Keep the **held-out** half out of the repository (the default
`.gitignore` excludes `golden/heldout/` except its `MANIFEST.json` of ids and hashes). Score
each held-out set once. Write a fresh one for the next verdict.

Check the cases and the split:

```bash
uv run python scripts/golden_tool.py check
```

It refuses a held-out question or earlier turn that also appears in the public half.

## 3. Run it

See the cost before you spend anything. The dry run writes the run folder and prints the
case count and the estimate, with no key and no call:

```bash
uv run python scripts/eval_live.py --config my-course/config.yaml --split public --dry-run
uv run python scripts/eval_live.py --config my-course/config.yaml --split public --yes
```

The live run needs `OPENAI_API_KEY` and stops at `--max-cost`. A held-out run on the sample
course (215 cases) cost $3.12. Run folders go under `runs/`, which is git-ignored: they hold
model answers, so never commit or share them.

## 4. The release gate

The gate decides whether a change may go live. It runs the held-out split and checks every
threshold (grounded answers, citations, locators, handovers, integrity, injection, leaks,
identity, latency and judge calibration):

```bash
uv run python scripts/release_gate.py offline
uv run python scripts/release_gate.py eval --config my-course/config.yaml --yes
```

`offline` needs no key. `eval` spends money and prints an estimate first. Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Passed; the change may go live |
| 1 | A gate failed |
| 2 | Error, including a run that is not a held-out run |
| 3 | Stopped at the cost cap |
| 4 | Judging pending |

`make eval` wraps the same command, but make reports every failure as 2. In CI, call the
script so the job sees the real code. An example workflow is in
[`docs/examples/course-ta-gate.yml`](../examples/course-ta-gate.yml).

## 5. Judges and calibration

LLM judges grade groundedness, integrity and identity. A judge is trusted only after it is
checked against labelled examples (Cohen's kappa with a confidence interval). See
[`docs/labelling.md`](../labelling.md). Until a judge is certified for your course, the gate
does not pass.

## Offline checks you can run any time

```bash
uv run course-assistant eval-replay
make eval-offline
```
