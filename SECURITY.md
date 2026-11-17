# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/biplob819/course-assistant/security/advisories/new)
for this repository, not in a public issue. Include what you found, how to reproduce it and
the version or commit you tested. You will get an acknowledgement within a week. Once a fix
is released, the advisory is published with credit to you unless you prefer otherwise.

Examples of what to report: a way to get answers from outside the approved materials, to get
graded work completed, to read another learner's data, to bypass a passcode or the spend
caps, or to make a key, a passcode or learner text appear in a log, trace or page.

Supported versions: the latest release (none is published yet) and the `main` branch.

## How keys are handled

- Keys are read from environment variables, a local `.env` file (git-ignored) or the host's
  secret store: `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY` for the second judge in evaluation.
- Each key is sent only to its own provider, through the provider's official SDK.
- The project never prints, logs or writes a key value. Startup checks name a missing secret,
  never its value. Run folders, traces and caches never contain keys.
- `scripts/secret_scan.py` runs in CI and pre-commit and fails on anything that looks like a
  key or private key.

## The demo

- Two numeric passcodes (learner and teaching team) are stored only as salted PBKDF2 hashes.
  Failed attempts are rate-limited. Replacing a hash locks every session unlocked with the
  old code at its next check.
- The demo uses a separate OpenAI project with a hard monthly spend limit, plus a cap per
  request and per day held in a hosted counter that survives restarts.
- The teaching team can pause all sessions. Removing the learner passcode hash closes every
  page.
- No traceback reaches a visitor, and Streamlit usage statistics are off.

Steps: [`docs/deploy.md`](docs/deploy.md).

## What to keep out of a public repository

Your `.env`, `.streamlit/secrets.toml`, the `runs/`, `.evalkit/` and `corpus_store/`
folders, your course material, your held-out golden cases and your calibration labels. The
default `.gitignore` already excludes them, and the corpus guard refuses them.
