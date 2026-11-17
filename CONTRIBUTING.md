# Contributing to course-assistant

Thank you for helping. Bug reports, fixes, tests, documentation and new golden cases for the
sample course are all welcome.

## Set up

```bash
git clone https://github.com/biplob819/course-assistant
cd course-assistant
uv sync --all-extras
uv run pytest -q
```

The tests make no network calls and spend nothing: every model response is scripted or
replayed. You do not need an API key to contribute.

## Before you open a pull request

Run the same checks CI runs (Python 3.11 to 3.13, Linux and macOS):

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
uv run python scripts/secret_scan.py
uv run python scripts/corpus_guard.py
```

- **Tests first.** A bug fix comes with a test that fails without it. A feature comes with
  tests for its behaviour and its error cases.
- **No paid calls in tests.** Use the scripted clients and the replay fixtures. Commands
  that spend money print an estimate and need explicit confirmation.
- **No keys anywhere.** Keys never appear in code, tests, logs, traces or output.
  `scripts/secret_scan.py` runs in CI and in pre-commit.
- **No course material or learner data.** Only the sample course (CC BY 4.0) belongs in this
  repository. `scripts/corpus_guard.py` refuses course-type files outside `sample_course/`,
  anything under `golden/heldout/` except its manifest, and any privately listed term.
- **Text files are UTF-8**, and every `open`, `read_text` and `write_text` names its encoding.
- **Commands that delete** (`purge`, `teardown`, ingest deletions) refuse without explicit
  confirmation and never touch files they did not create.
- Add one line to [`changelog.md`](changelog.md).

## Golden cases

New cases go in `golden/public/cases.jsonl` and must pass `python scripts/golden_tool.py
check`. Write them against the sample course only. Never paste a held-out case, a model
answer from a held-out run or a calibration label into an issue or a pull request. See
[`golden/README.md`](golden/README.md) and [`DATA_CARD.md`](DATA_CARD.md).

## Changing what the assistant does

A change to prompts, rules, models or the grounding bar changes the measured numbers. Say so
in the pull request. The owner reruns the evaluation and the release gate
(`scripts/release_gate.py`) before such a change goes live; tuning uses the public half only.

## Larger changes

Open an issue first to agree on the approach. Design decisions are recorded in
[`docs/decisions.md`](docs/decisions.md) and lessons in [`docs/lessons.md`](docs/lessons.md).
Reusable evaluation features belong in [EvalKit](https://github.com/biplob819/evalkit)
rather than here.

By contributing you agree that your work is released under the project's
[MIT licence](LICENSE) (sample-course material under [CC BY 4.0](sample_course/LICENSE)), and
to follow the [code of conduct](CODE_OF_CONDUCT.md).
