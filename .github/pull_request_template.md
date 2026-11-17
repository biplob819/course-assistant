## What and why

<!-- What changes, and the issue or decision it follows. -->

## Checklist

- [ ] Tests first: a test fails without this change and passes with it
- [ ] `uv run ruff check .` and `uv run ruff format --check .` are clean
- [ ] `uv run pyright` is clean
- [ ] `uv run pytest -q` passes, with no network call and no API key
- [ ] No key, passcode, hash, learner text or private course material in code, tests, fixtures or logs
- [ ] A change to the answer rules adds or updates golden cases
- [ ] `changelog.md` updated for a user-visible change

## Security

Found a vulnerability? Do not describe it in a pull request. Report it privately as
[SECURITY.md](https://github.com/biplob819/course-assistant/blob/main/SECURITY.md) describes.
