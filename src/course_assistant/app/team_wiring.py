"""Build the teaching-team view from named settings (B2.3). Secrets come only from the
environment or the host's secret store (D3.1 `read_secret`), never from a file in the repo.

    CA_CONFIG            AppConfig YAML of an ingested course (corpus store next to it); when
                         unset, the course comes from the demo cache named by CA_REPLAY (D3.2)
    CA_REPLAY, CA_DAILY_RESET  as for the learner page (wiring.py)
    CA_MANIFEST          the approved manifest (default manifest.yaml next to the config)
    CA_APP_DIR           the app store shared with the learner page (default runs/app)
    STAFF_PASSKEY_HASH   this page's own passkey hash; unset keeps the page closed
    DEMO_PASSKEY_HASH    the learner hash, read only to refuse a staff passkey equal to it
    CA_STAFF_ROLE        the role id written to the audit log (default instructor-1)
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from course_assistant.app.team import DEFAULT_ROLE, TeachingTeam

CONFIG = "CA_CONFIG"
MANIFEST = "CA_MANIFEST"
APP_DIR = "CA_APP_DIR"
ROLE = "CA_STAFF_ROLE"
DEFAULT_APP_DIR = Path("runs") / "app"

_factory: Callable[[], TeachingTeam] | None = None


class WiringError(Exception):
    """The page cannot start: a setting is missing or unreadable (fixed text, no secret)."""


def set_factory(fn: Callable[[], TeachingTeam] | None) -> None:
    """Install the function `controller()` returns from (tests), or None for `from_env`."""
    global _factory
    _factory = fn


def controller(default: Callable[[], TeachingTeam] | None = None) -> TeachingTeam:
    if _factory is not None:
        return _factory()
    return (default or from_env)()


def from_env(env: Mapping[str, str] | None = None,
             secrets_store: Mapping[str, object] | None = None) -> TeachingTeam:  # fmt: skip
    from course_assistant.app.team import staff_hash
    from course_assistant.app.wiring import WiringError as LearnerWiringError
    from course_assistant.app.wiring import app_stores, demo_cache
    from course_assistant.demo.passkey import PasskeyGate, env_hash
    from course_assistant.ingest import CorpusStore
    from course_assistant.schemas.config import load_app_config, load_manifest

    env = os.environ if env is None else env
    try:
        cache = demo_cache(env)
    except LearnerWiringError as exc:
        raise WiringError(str(exc)) from None
    raw = env.get(CONFIG)
    if not raw and cache is not None:  # the hosted demo: the course travels in the cache
        config, corpus = cache.fixture.config, cache.fixture.corpus
        try:
            manifest = load_manifest(Path(env.get(MANIFEST) or ""))
        except Exception:
            raise WiringError(f"Set {MANIFEST} to the course's approved manifest.") from None
    else:
        if not raw:
            raise WiringError(f"Set {CONFIG} to the AppConfig YAML of an ingested course.")
        path = Path(raw)
        try:
            config = load_app_config(path)
            corpus = CorpusStore(path.parent / "corpus_store", config.cohort).current()
            manifest = load_manifest(Path(env.get(MANIFEST) or path.parent / "manifest.yaml"))
        except Exception:
            raise WiringError(f"Cannot read the course named by {CONFIG}.") from None
        if corpus is None:
            raise WiringError(f"No corpus yet: run course-assistant ingest --config {path}.")
    app_dir = Path(env.get(APP_DIR) or DEFAULT_APP_DIR)
    app_dir.mkdir(parents=True, exist_ok=True)
    gate = PasskeyGate(staff_hash(env, secrets_store))
    try:
        return TeachingTeam(gate, store=app_stores(env, app_dir), corpus=corpus,
                            manifest=manifest, config=config,
                            role=env.get(ROLE) or DEFAULT_ROLE,
                            learner_hash=env_hash(env, secrets_store))  # fmt: skip
    except ValueError:
        raise WiringError(f"Check {ROLE} and that the manifest matches the course.") from None
