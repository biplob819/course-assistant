# Releasing v0.1.0: the owner's checklist

R3.1's last step (OSS-08) is the owner's: publishing is done by trusted publishing from
`.github/workflows/release.yml`, with no stored token, and each publish is approved by hand.
Do the steps in order. Steps 3 to 5 can also be done by the builder on your OK.

1. **PyPI pending publisher.** On pypi.org, Account settings → Publishing → Add a new pending
   publisher (GitHub): PyPI project name `course-assistant`, owner `biplob819`, repository
   `course-assistant`, workflow `release.yml`, environment `pypi`.
2. **GitHub environment.** In the repository: Settings → Environments → New environment
   `pypi`. Add yourself as a required reviewer, and limit deployment branches and tags to
   `v*` tags.
3. **Version.** Set `version = "0.1.0"` in `pyproject.toml`, then run `uv lock` (the release
   build runs `uv sync --locked` and fails on a stale lockfile; `uv lock --check` must pass).
4. **Status text.** Mark 0.1.0 as released: the heading and first paragraph of
   `changelog.md`'s 0.1.0 section; the README status note, the "Once published" sentence
   under the quickstart and the PyPI row in Links; the draft notes at the top of
   `docs/guide/quickstart.md` and `docs/guide/own-course.md`; the install note in
   `docs/guide/evaluate.md`.
5. **Main and CI.** Commit, push to main and wait for the `ci` workflow to pass.
6. **Tag.** On main: `git tag v0.1.0 && git push origin v0.1.0`. The `release` workflow builds,
   runs the exclusion test and the release tests, checks the tag equals the version, then waits
   at the `pypi` environment.
7. **Approve.** In the run's page, Review deployments → approve `pypi`. The package is
   published to pypi.org/project/course-assistant.
8. **Clean-venv check.** On a machine with no checkout:

   ```bash
   python3 -m venv /tmp/ca && . /tmp/ca/bin/activate
   pip install 'course-assistant[openai]'
   course-assistant --version          # course-assistant 0.1.0
   course-assistant init /tmp/ca-course && cd /tmp/ca-course
   course-assistant doctor             # FAILs until you fill in the key and the vector store
   course-assistant eval-replay        # golden replay: 40/40 requests match
   ```

9. **Attest.** Add a row to `docs/decisions.md` (next free number) opening with
   "Attests R3.1", for example:

   ```
   | OWNER D-n | <date> | Attests R3.1: course-assistant 0.1.0 published to PyPI by trusted publishing from tag v0.1.0 (release run <url>); a clean venv install ran --version, doctor and eval-replay 40/40 | — | Owner release |
   ```

   Then the OSS-08 proof becomes `attest=OWNER D-n` and the builder flips R3.1 green after the
   verifier.
