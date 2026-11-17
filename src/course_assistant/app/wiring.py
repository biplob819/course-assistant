"""Build the learner chat from named settings (B2.5). Secrets come only from the environment
or the host's secret store (D3.1 `read_secret`), never from a file in the repo.

    CA_CONFIG          AppConfig YAML of an ingested course (corpus store next to it); when
                       unset, the course comes from the demo cache named by CA_REPLAY (D3.2)
    CA_REPLAY          a D3.1 demo cache: the page's worked examples (D3.2)
    CA_DAILY_RESET     "1": one app store per UTC day, earlier days removed (D3.2)
    CA_ASSESSMENTS     assessment briefs (default sample_course/assessments.yaml)
    CA_APP_DIR         app store, trace file and local spend counter (default runs/app)
    DEMO_PASSKEY_HASH  the passkey hash (D3.1); unset keeps the page closed
    OPENAI_API_KEY     the owner's key; unset means only visitors' own keys answer
    DEMO_KV_REST_URL, DEMO_KV_REST_TOKEN  the hosted spend counter, when set
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from course_assistant.app.chat import LearnerChat, PerThreadStore

if TYPE_CHECKING:
    from course_assistant.demo.cache import DemoCache
    from course_assistant.store.db import Store

CONFIG = "CA_CONFIG"
REPLAY = "CA_REPLAY"
DAILY_RESET = "CA_DAILY_RESET"
ASSESSMENTS = "CA_ASSESSMENTS"
APP_DIR = "CA_APP_DIR"
OWNER_KEY = "OPENAI_API_KEY"
DEFAULT_ASSESSMENTS = Path("sample_course") / "assessments.yaml"
DEFAULT_APP_DIR = Path("runs") / "app"

_factory: Callable[[], LearnerChat] | None = None


class WiringError(Exception):
    """The page cannot start: a setting is missing or unreadable (fixed text, no secret)."""


def set_factory(fn: Callable[[], LearnerChat] | None) -> None:
    """Install the function `controller()` returns from (tests), or None for `from_env`."""
    global _factory
    _factory = fn


def controller(default: Callable[[], LearnerChat] | None = None) -> LearnerChat:
    """The page's LearnerChat: an installed factory first, else `default` (the page passes
    a cached `from_env`)."""
    if _factory is not None:
        return _factory()
    return (default or from_env)()


def demo_cache(env: Mapping[str, str]) -> DemoCache | None:
    """The demo cache named by `CA_REPLAY`, or None when unset or the file is not there yet
    (the owner's `make demo-cache` run is pending); an unreadable cache refuses to start."""
    from course_assistant.demo.cache import DemoCache, load_cache

    raw = env.get(REPLAY)
    if not raw or not Path(raw).is_file():
        return None
    try:
        return DemoCache(load_cache(Path(raw)))
    except Exception:
        raise WiringError(f"Cannot read the demo cache named by {REPLAY}.") from None


def app_stores(env: Mapping[str, str], app_dir: Path) -> Callable[[], Store]:
    """One store on one file, or one per UTC day when `CA_DAILY_RESET` is "1" (the demo)."""
    if (env.get(DAILY_RESET) or "").strip() == "1":
        from course_assistant.app.deploy import DailyStores

        return DailyStores(app_dir)
    return PerThreadStore(app_dir / "app.sqlite")


def from_env(env: Mapping[str, str] | None = None,
             secrets_store: Mapping[str, object] | None = None) -> LearnerChat:  # fmt: skip
    import openai

    from course_assistant.answer import responder
    from course_assistant.answer.ask import Assistant
    from course_assistant.answer.replay import Source
    from course_assistant.demo.guard import Guard, GuardConfig
    from course_assistant.demo.live import LiveDemo
    from course_assistant.demo.passkey import PasskeyGate, env_hash, read_secret
    from course_assistant.demo.spend import (
        KV_TOKEN,
        KV_URL,
        FileSpendCounter,
        KVSpendCounter,
        RestKV,
        SpendCounter,
    )
    from course_assistant.ingest import CorpusStore
    from course_assistant.llm import JsonlSink, load_prices
    from course_assistant.schemas.config import load_app_config, load_assessments

    env = os.environ if env is None else env

    def secret(name: str) -> str | None:
        return read_secret(name, env=env, secrets_store=secrets_store)

    cache = demo_cache(env)
    raw = env.get(CONFIG)
    if not raw and cache is not None:  # the hosted demo: the course travels in the cache
        config, corpus = cache.fixture.config, cache.fixture.corpus
        assessments = cache.fixture.assessments
    else:
        if not raw:
            raise WiringError(f"Set {CONFIG} to the AppConfig YAML of an ingested course.")
        path = Path(raw)
        try:
            config = load_app_config(path)
            corpus = CorpusStore(path.parent / "corpus_store", config.cohort).current()
            assessments = load_assessments(Path(env.get(ASSESSMENTS) or DEFAULT_ASSESSMENTS))
        except Exception:
            raise WiringError(f"Cannot read the course named by {CONFIG}.") from None
        if corpus is None:
            raise WiringError(f"No corpus yet: run course-assistant ingest --config {path}.")
    app_dir = Path(env.get(APP_DIR) or DEFAULT_APP_DIR)
    app_dir.mkdir(parents=True, exist_ok=True)
    stores = app_stores(env, app_dir)
    sink = JsonlSink(app_dir / "trace.jsonl")
    prices = load_prices()
    gate = PasskeyGate(env_hash(env, secrets_store))
    url, token = secret(KV_URL), secret(KV_TOKEN)
    counter: SpendCounter = (KVSpendCounter(RestKV(url, token)) if url and token
                             else FileSpendCounter(app_dir / "spend.json"))  # fmt: skip
    guard = Guard(gate, counter, GuardConfig())

    def client_for(key: str) -> Any:
        return openai.OpenAI(api_key=key, max_retries=0)

    owner_key = secret(OWNER_KEY)
    owner = (responder(client=client_for(owner_key), prices=prices, sink=sink)
             if owner_key else None)  # fmt: skip
    del owner_key

    def for_key(key: str) -> Any:
        """A responder on the visitor's key for one request only; nothing keeps it."""
        return responder(client=client_for(key), prices=prices, sink=sink)

    def assistant_for(source: Source) -> Assistant:
        return Assistant(config, corpus, assessments, stores(), source, sink=sink)

    demo = LiveDemo(gate, guard, assistant_for=assistant_for, owner_responder=owner,
                    responder_for_key=for_key, prices=prices, cache=cache)  # fmt: skip
    return LearnerChat(gate, demo, assistant_for=assistant_for, store=stores, traces=[sink],
                       corpus=corpus)  # fmt: skip
