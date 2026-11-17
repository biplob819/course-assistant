# R3.1 — Open-source packaging and v0.1.0

**Wave 3. Depends on:** T3.1, D3.2. **Unblocks:** P4.x (none blocked on it in the graph).

## Goal
Publish the project as an open-source package (OWNER D-21): a course team elsewhere goes from
`pip install course-assistant` to a grounded answer on their own materials without reading the
build harness. This node adds `course-assistant init` and `course-assistant doctor`, the
community files and GitHub templates, a release workflow that publishes to PyPI by Trusted
Publishing on a version tag, the user docs, the privacy statement, an example eval Action and
a README that renders on PyPI (OWNER D-46). Drafting started under OWNER D-72; nothing is
published and no tag is created without the owner (D-21, D-72).

The build is split into parallel lanes; this spec covers the whole node. Lane B (release
mechanics) owns `cli.py` (`init`, `doctor`), `.github/` templates and `release.yml`,
`scripts/release*.py` (not `release_gate.py`), the README's image and link URLs, and
`tests/release/`. Docs, the privacy statement, the example Action and the README text are
other lanes'.

## Owner-attest rows (D-60)
The PyPI publish, the `v0.1.0` tag and the clean-venv install of the *published* package need
the owner (their PyPI account, the trusted-publisher registration, the protected `pypi`
environment and the decision to publish, D-21). These live in OSS-08, whose catalog note must
end `(owner attest)` before its trace bracket; it is green only with `attest=OWNER D-n` opening
"Attests R3.1". OSS-01 keeps the release *workflow* (provable offline from the file); the act
of publishing is OSS-08. **Catalog change needed (not lane B's file):** append
`(owner attest)` to the OSS-08 note in `docs/graph/catalog.tsv` and the Tech Spec §11.4 row.

## Outputs

### `course-assistant init [DIR]` (OSS-02)
- Writes into `DIR` (default `.`; created if missing) the starter files for a new course:
  `config.yaml`, `manifest.yaml`, `assessments.yaml` and `.env.example`. The templates are
  package data under `course_assistant/templates/` (shipped in the wheel), so `init` works from
  a plain `pip install` with no repo checkout.
- The generated `config.yaml` and `manifest.yaml` parse with the project's own loaders
  (`load_app_config`, `load_manifest`) once the placeholders are left as written; the vector
  store id is a clearly marked placeholder that `doctor` reports as not yet set.
- Never overwrites (L-3): an existing file is left byte-for-byte unchanged and reported as
  `kept <name> (exists)`; the others are written. Writes are exclusive-create (`open(..., "x")`),
  so a file appearing between check and write is not clobbered. No `--force` flag.
- `.env.example` holds names only, every value empty (`OPENAI_API_KEY=`). No network call.
- Exit 0 when every file is written or kept; 2 when `DIR` is not a folder or cannot be written.

### `course-assistant doctor [--config PATH]` (OSS-03)
Offline only: no network, no API call, no key value read beyond "is it set and non-empty".
Each check prints one fixed line `ok|warn|FAIL  <check>: <fixed message>`; messages are
constant strings plus file names and secret *names*, never a value.
- `python`: running interpreter is ≥ 3.11 (FAIL otherwise).
- `extras`: for each optional extra (`openai`, `ingest`, `eval`, `app`), whether its modules are
  importable (`importlib.util.find_spec`); missing `openai` is FAIL (no answer without it), the
  others warn with the `pip install 'course-assistant[<extra>]'` line.
- `config`: `config.yaml` (default `./config.yaml`) loads with `load_app_config`; FAIL with the
  error class name on a parse or schema error, FAIL when missing.
- `secrets`: `OPENAI_API_KEY` is set and non-empty in the environment or a `.env` next to the
  config (FAIL, names only). The value is never printed, logged or echoed back.
- `vector_store`: the configured id matches `^vs_[A-Za-z0-9]+$` (FAIL on the init placeholder
  or a malformed id). Format only; no call checks the store exists.
- `corpus`: the manifest next to the config loads with `load_manifest`, its cohort equals the
  config's, and every file it lists exists under the corpus root (FAIL naming the first
  missing path, plus a count).
- Exit codes: 0 no FAIL (warnings allowed), 1 at least one FAIL, 2 the command itself could
  not run (bad arguments).

### Community files and templates (OSS-05)
- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md` exist (W0.1/T3.x drafts).
- `.github/ISSUE_TEMPLATE/bug_report.yml`, `feature_request.yml` and `config.yml`
  (`blank_issues_enabled: false`, a contact link "Report a security vulnerability" pointing to
  `SECURITY.md` / private advisories). The bug form asks for version, Python, OS, the
  `doctor` output, and warns not to paste keys, passcodes or learner text.
- `.github/pull_request_template.md`: checklist (tests first, `ruff`, `pyright`, `pytest`,
  no secrets, no network in tests, changelog), plus the security pointer to `SECURITY.md`.

### `.github/workflows/release.yml` (OSS-01)
- `on: push: tags: ["v*.*.*"]` only; top-level `permissions: contents: read`.
- `build` job: checkout (SHA-pinned, `persist-credentials: false`, full history), setup-uv
  (SHA-pinned), `uv sync --locked --all-extras`, run `tests/harness/test_packaging.py`
  (the exclusion test) and `tests/release/`, check the tag equals `v` + the pyproject version
  (`scripts/release_check.py`), `uv build`, `twine check`-equivalent metadata check
  (`scripts/release_check.py --dist dist`: one sdist and one wheel, names match the version,
  README rendered as Markdown, no private path in either), upload `dist/` as an artefact
  (SHA-pinned).
- `publish` job: `needs: build`, `environment: pypi` (protected; the owner adds reviewers),
  `permissions: id-token: write` only, `if:` the ref is a tag *and* the tagged commit is on
  `main` (the build job outputs `on_main` from `git merge-base --is-ancestor $GITHUB_SHA
  origin/main`), downloads the artefact and runs `pypa/gh-action-pypi-publish` (SHA-pinned)
  with no `password`/token input (Trusted Publishing, OIDC).
- Every `uses:` is pinned to a 40-hex SHA with a version comment, as in `ci.yml`.
- `ci.yml` and `eval-gate.yml` unchanged.

### README links (OSS-09, lane B part)
- Every relative image and link in `README.md` becomes an absolute URL so it renders on PyPI:
  images and SVGs `https://raw.githubusercontent.com/biplob819/course-assistant/main/<path>`;
  other files `https://github.com/biplob819/course-assistant/blob/main/<path>` (folders
  `/tree/main/`). In-page anchors (`#...`) stay. Text is other lanes'.

### Docs, privacy, example Action (OSS-04, OSS-06, OSS-07; other lanes)
- OSS-04: quickstart on the sample course, bring your own course (uses `init`, `doctor`,
  `ingest --dry-run`), writing and evaluating a golden set, cost and privacy.
- OSS-06: `PRIVACY.md` states no telemetry and that learner and course data go only to the
  operator's OpenAI project.
- OSS-07: an example workflow under `docs/` that runs the course-ta gate and fails the job on a
  failed threshold.

### Release (OSS-08, owner attest)
- Semver policy in `CONTRIBUTING.md` or the changelog (0.x: minor may break; patch never).
- Owner: register the trusted publisher on PyPI (`biplob819/course-assistant`, workflow
  `release.yml`, environment `pypi`), protect the `pypi` environment, bump the version to
  0.1.0, tag `v0.1.0` on main, approve the publish, then in a clean venv
  `pip install course-assistant` → `--version`, `doctor`, `eval-replay` pass; record
  `OWNER D-n` "Attests R3.1".

## Rules
- No network in any test or in `init`/`doctor`. No secret value printed, logged or written.
- No stored PyPI token anywhere (repo, workflow, secrets). No tag, no publish by the build.
- `init` never overwrites a file.

## Out of scope
- The actual PyPI publish, the tag and the published-package check (owner, OSS-08).
- EvalKit releases (D-68: EvalKit's own build).
