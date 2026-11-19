# Bring your own course

This guide sets the assistant up on your own course materials. Do the
[quickstart](quickstart.md) first, so you know what a working setup looks like.

> **Draft (R3.1).** Until the package is on PyPI, run these commands from a checkout with
> `uv run`. Once it is published, `pip install 'course-assistant[openai,ingest]'` gives the
> same `course-assistant` command.

## What a course is

A course is a folder with three files and your materials:

| File | What it holds |
| --- | --- |
| `config.yaml` | The cohort id, your vector store id, the models and the grounding bar |
| `manifest.yaml` | Every approved file, per session, with its sha256. Nothing else is indexed |
| `assessments.yaml` | The briefs of graded work. The assistant gives hints on these, never the work |

Only what the manifest lists is ever sent to the vector store. Never list a model solution,
a marking guide or learner work.

## 1. Write the starter files

```bash
uv run course-assistant init my-course
```

`init` writes `config.yaml`, `manifest.yaml`, `assessments.yaml` and `.env.example` into
`my-course/`. It never overwrites a file: one that exists is kept and reported as
`kept <name> (exists)`. It makes no network call.

## 2. Fill them in

- **`config.yaml`.** Pick a `cohort` id (lower case, for example `stats101-2027`). Create an
  empty vector store in your OpenAI project and paste its id (`vs_...`) as
  `vector_store_id`. Set `monthly_budget_usd`. Leave `grounding_bar` at 0.4 until you have
  your own golden set (see [evaluate your course](evaluate.md)).
- **`manifest.yaml`.** Put your files under `my-course/materials/` (or anywhere under the
  corpus root). Add one entry per file: `session` (`s01`, `s02`, ...), `kind` (`slides`,
  `pre_read`, `agenda`, `post_session` or `faq`), the licence, who approved it (a role id
  such as `instructor-1`, never a name) and its sha256. Get the hash with:

```bash
shasum -a 256 my-course/materials/s01-slides.pptx
```

- **`assessments.yaml`.** One entry per graded item, with the brief learners see and the
  sessions it draws on. Briefs only.
- **Your key.** `.env.example` lists the names. Set the values in your shell or your host's
  secret store. The package does not read `.env` by itself.

Slides (`.pptx`) and PDFs are supported. Speaker notes are left out unless an entry sets
`include_speaker_notes: true`.

## 3. Check the setup

```bash
uv run course-assistant doctor --config my-course/config.yaml
```

`doctor` checks, offline:

- Python 3.11 or newer;
- the optional extras;
- that `config.yaml` loads;
- that `OPENAI_API_KEY` is set (it names the variable, never its value);
- the vector store id's format (it does not contact the store);
- that every manifest file exists and matches its sha256.

It exits 0 when nothing fails (warnings are fine) and 1 when a check fails. Fix each `FAIL`
line and run it again.

## 4. Ingest

Always look at the plan first. The dry run makes no API call:

```bash
uv run course-assistant ingest --config my-course/config.yaml --dry-run
```

It lists each unit (a slide or a page) to upload or delete. When the plan looks right, run
it without `--dry-run`. Ingest only ever deletes what it uploaded, and it checks the vector
store against the manifest when it finishes.

If your manifest paths are relative to another folder, pass `--root`; if the manifest is not
next to the config, pass `--manifest`.

## 5. Evaluate before learners use it

A new course is not ready because ingest worked. Write a small golden set for it and run the
release gate first: see [evaluate your course](evaluate.md). Costs and what leaves your
machine are in [cost and privacy](cost-and-privacy.md).

## Changing the course later

Edit the manifest (add, change or remove entries), run `doctor`, then `ingest --dry-run` and
`ingest`. A change that adds or changes units waits for the release gate before learners are
served it. A removal is served at once.
