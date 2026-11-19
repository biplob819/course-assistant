# Quickstart: the sample course

This guide takes you from a fresh checkout to grounded answers on the sample course. Part 1
needs no key and spends nothing. Part 2 uses your own OpenAI key, should take under
10 minutes and spends well under $1.

> **Draft (R3.1).** The 10-minute figure is a target, not yet timed on a clean machine. Until
> the package is on PyPI, install from a checkout as shown below.

You need [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer.

## Part 1: offline, no key

```bash
git clone https://github.com/biplob819/course-assistant
cd course-assistant
uv sync --all-extras
uv run course-assistant --version
uv run course-assistant eval-replay
```

`eval-replay` sends 40 recorded sample-course questions back through the real answer path
and compares each answer with the stored one. It prints
`golden replay: 40/40 requests match` and exits 0. No network call is made.

## Part 2: live answers with your own key

The sample course is three short sessions (slides, pre-reads, agendas) under
[`sample_course/`](../../sample_course). It is licensed CC BY 4.0.

**1. Make a vector store.** In your OpenAI dashboard, open the project you want to use,
create an empty vector store and copy its id. It starts with `vs_`.

**2. Set your key** in your shell. This reads it without showing it or saving it in your
shell history. Never paste it into a file you commit.

```bash
read -rs OPENAI_API_KEY
export OPENAI_API_KEY
```

**3. Write a config** for the sample course:

```bash
uv run course-assistant init my-sample
```

Open `my-sample/config.yaml` and change two lines:

- `cohort: sample` (the sample manifest's cohort);
- `vector_store_id:` the id from step 1.

The other files `init` wrote are for your own course later. For the sample course you use
the committed `sample_course/manifest.yaml`.

**4. Check the setup.** Nothing here is sent anywhere.

```bash
uv run course-assistant doctor --config my-sample/config.yaml --manifest sample_course/manifest.yaml --root .
```

Every line should read `ok` (an `ANTHROPIC_API_KEY` warning is fine). It ends with
`doctor: ready`.

**5. See the plan, then ingest.** The dry run makes no API call.

```bash
uv run course-assistant ingest --config my-sample/config.yaml --manifest sample_course/manifest.yaml --root . --dry-run
uv run course-assistant ingest --config my-sample/config.yaml --manifest sample_course/manifest.yaml --root .
```

The second command uploads the split course text to your vector store. It ends with
`reconciled: vector store vs_... holds N units`.

**6. Ask questions.** This runs 30 questions from the public golden set through the full
answer path and prints latency, cost and each status:

```bash
uv run python scripts/smoke_live.py --config my-sample/config.yaml --latency-only --yes
```

It prints its cost estimate first and stops if the estimate is over `--max-cost` (default
$5). Results go to `runs/smoke-<stamp>.json`, which is git-ignored.

> **Pending (R3.1).** A one-question command (`course-assistant ask`) and a local run of the
> learner page without the demo's secrets are not built yet. The learner page today needs
> the demo setup in [`docs/deploy.md`](../deploy.md).

## When you are done

Delete the vector store in your OpenAI dashboard, and the `my-sample/` and `runs/` folders,
if you no longer need them.

Next: [bring your own course](own-course.md), then [evaluate it](evaluate.md).
