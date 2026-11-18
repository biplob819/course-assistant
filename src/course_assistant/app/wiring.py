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

Which corpus is served (P4.1 groundwork, D-57): with `CA_CONFIG` (a pilot course), only the
gate-approved version named by the corpus store's `LIVE.json`, minus removed units
(`ingest.serving`), read again at every page run, so a flip or a removal is served with no
restart and a newer ingest never is; no pointer, or one that cannot be read, locks the page.
With the demo cache alone (the hosted demo on the sample course), the cache's own replay
corpus, which never changes while the app runs. Every trace line carries the served corpus
version id (the allowlisted `version` key) and nothing else about the corpus.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from course_assistant.app.chat import ChatState, LearnerChat, PerThreadStore
from course_assistant.demo._errors import GuardRefused
from course_assistant.ingest.corpus import Corpus

if TYPE_CHECKING:
    from course_assistant.demo.cache import DemoCache
    from course_assistant.llm import TraceSink
    from course_assistant.schemas.config import AppConfig
    from course_assistant.store.db import Store

CONFIG = "CA_CONFIG"
REPLAY = "CA_REPLAY"
DAILY_RESET = "CA_DAILY_RESET"
ASSESSMENTS = "CA_ASSESSMENTS"
APP_DIR = "CA_APP_DIR"
OWNER_KEY = "OPENAI_API_KEY"
DEFAULT_ASSESSMENTS = Path("sample_course") / "assessments.yaml"
DEFAULT_APP_DIR = Path("runs") / "app"

# a fixed text for a page whose gate-approved corpus cannot be read (P4.1); never a path
SERVING_LOCKED = "The course assistant is not available right now. Please try again later."
NO_LIVE = ("No gate-approved corpus: the release gate has not passed a corpus version for "
           "this course, or its LIVE.json cannot be read.")  # fmt: skip

_factory: Callable[[], LearnerChat] | None = None
_CHECKS: dict[tuple[str, str], Serving] = {}  # the startup check's readers (serving_problem)


class WiringError(Exception):
    """The page cannot start: a setting is missing or unreadable (fixed text, no secret)."""


class Serving:
    """The corpus the pages serve, read at every call (P4.1): the pilot's gate-approved view
    (`ingest.serving.ServedCorpus`) or the demo's fixed replay corpus. `version()` is the id
    last served, for the trace lines of calls made outside one Assistant."""

    def __init__(self, read: Callable[[], Corpus],
                 read_keyed: Callable[[], tuple[Corpus, str]] | None = None) -> None:  # fmt: skip
        self._read = read
        self._read_keyed = read_keyed
        self._last: str | None = None

    @classmethod
    def fixed(cls, corpus: Corpus) -> Serving:
        return cls(lambda: corpus)

    def __repr__(self) -> str:
        return "Serving()"

    def __call__(self) -> Corpus:
        """The served corpus now; `GuardRefused(SERVING_LOCKED)` when there is none."""
        return self.served()[0]

    def served(self) -> tuple[Corpus, str]:
        """The served corpus now and the vector-store attribute its files are found by
        (`ingest.serving.ServedCorpus.read`; `corpus_version` for a fixed corpus)."""
        from course_assistant.ingest.corpus import VERSION_KEY
        from course_assistant.ingest.serving import ServingError

        try:
            if self._read_keyed is not None:
                corpus, key = self._read_keyed()
            else:
                corpus, key = self._read(), VERSION_KEY
        except ServingError:
            raise GuardRefused(SERVING_LOCKED) from None
        self._last = corpus.corpus_version
        return corpus, key

    def version(self) -> str | None:
        return self._last


class VersionedSink:
    """A trace sink that stamps every record with the served corpus version id (P4.1) before
    the real sink (which keeps only allowlisted keys); deletion passes straight through."""

    def __init__(self, inner: TraceSink, version: Callable[[], str | None]) -> None:
        self.inner = inner
        self._version = version

    def __repr__(self) -> str:
        return "VersionedSink()"

    def emit(self, record: dict[str, Any]) -> None:
        version = self._version()
        out = {k: v for k, v in record.items() if k != "version"}
        if version is not None:
            out["version"] = version
        self.inner.emit(out)

    def delete_pid(self, pid: str, conversation_ids: Collection[str] | None = None,
                   known: Collection[str] = ()) -> int:  # fmt: skip
        remove = getattr(self.inner, "delete_pid", None)
        return 0 if remove is None else int(remove(pid, conversation_ids, known))


class ServedChat(LearnerChat):
    """The learner chat over a `Serving`: every page run (`locked_reason`, the page's first
    call) re-reads the served corpus, and with none every method refuses with the fixed
    `SERVING_LOCKED`, whatever the passkey session. The passkey gate is unchanged: it is still
    checked first by `LearnerChat` on every method."""

    def __init__(self, *args: Any, serving: Serving, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.serving = serving
        self._down = False

    def refresh(self) -> bool:
        try:
            self.corpus = self.serving()
        except GuardRefused:
            self._down = True
            return False
        self._down = False
        return True

    def locked_reason(self, state: ChatState) -> str | None:
        if not self.refresh():
            return SERVING_LOCKED
        return super().locked_reason(state)

    def _require(self, state: ChatState) -> None:
        super()._require(state)
        if self._down:
            raise GuardRefused(SERVING_LOCKED)


def pilot_serving(config_path: Path, config: AppConfig) -> Serving:
    """The pilot's served corpus for the course at `config_path`; `WiringError(NO_LIVE)` when
    none can be served now."""
    from course_assistant.ingest import CorpusStore
    from course_assistant.ingest.serving import ServedCorpus

    served = ServedCorpus(CorpusStore(config_path.parent / "corpus_store", config.cohort))
    serving = Serving(served, served.read)
    try:
        serving()
    except GuardRefused:
        raise WiringError(NO_LIVE) from None
    return serving


def serving_problem(env: Mapping[str, str]) -> str | None:
    """For the startup lock (D3.2 entry): None when the pages have a corpus to serve or the
    course comes from the demo cache; else a description with no path and no secret."""
    from course_assistant.schemas.config import load_app_config

    raw = (env.get(CONFIG) or "").strip()
    if not raw:
        return None  # the demo cache's replay corpus (or `course_problem` locks)
    try:
        config = load_app_config(Path(raw))
    except Exception:
        return "the course config named by CA_CONFIG cannot be read"
    key = (str(Path(raw).resolve().parent), config.cohort)
    serving = _CHECKS.get(key)  # one reader per course: corpus files are parsed once
    try:
        if serving is None:
            serving = _CHECKS[key] = pilot_serving(Path(raw), config)
        else:
            serving()
    except (WiringError, GuardRefused):
        return "no gate-approved corpus (LIVE.json missing or unreadable)"
    return None


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
    from course_assistant.llm import JsonlSink, load_prices
    from course_assistant.schemas.config import load_app_config, load_assessments

    env = os.environ if env is None else env

    def secret(name: str) -> str | None:
        return read_secret(name, env=env, secrets_store=secrets_store)

    cache = demo_cache(env)
    raw = env.get(CONFIG)
    if not raw and cache is not None:  # the hosted demo: the course travels in the cache
        config, assessments = cache.fixture.config, cache.fixture.assessments
        serving = Serving.fixed(cache.fixture.corpus)  # the replay corpus (P4.1 choice)
    else:
        if not raw:
            raise WiringError(f"Set {CONFIG} to the AppConfig YAML of an ingested course.")
        path = Path(raw)
        try:
            config = load_app_config(path)
            assessments = load_assessments(Path(env.get(ASSESSMENTS) or DEFAULT_ASSESSMENTS))
        except Exception:
            raise WiringError(f"Cannot read the course named by {CONFIG}.") from None
        serving = pilot_serving(path, config)  # the gate-approved corpus only (P4.1)
    try:
        corpus = serving()
    except GuardRefused:
        raise WiringError(NO_LIVE) from None
    app_dir = Path(env.get(APP_DIR) or DEFAULT_APP_DIR)
    app_dir.mkdir(parents=True, exist_ok=True)
    stores = app_stores(env, app_dir)
    raw_sink = JsonlSink(app_dir / "trace.jsonl")
    sink = VersionedSink(raw_sink, serving.version)  # calls made outside one Assistant
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
        """One request's Assistant on the corpus served now; its trace lines carry exactly
        that version."""
        served, key = serving.served()
        return Assistant(config, served, assessments, stores(), source,
                         sink=VersionedSink(raw_sink, lambda: served.corpus_version),
                         version_key=key)  # fmt: skip

    demo = LiveDemo(gate, guard, assistant_for=assistant_for, owner_responder=owner,
                    responder_for_key=for_key, prices=prices, cache=cache)  # fmt: skip
    return ServedChat(gate, demo, assistant_for=assistant_for, store=stores,
                      traces=[raw_sink], corpus=corpus, serving=serving)  # fmt: skip
