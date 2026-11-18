"""The corpus the pages serve (P4.1 groundwork, D-57 (4)).

The served corpus is the gate-approved version named by the corpus store's live pointer
(`LIVE.json`, written only by the release gate, E2.4), minus every unit that is no longer in
the ingested corpus (`CURRENT`): a newer ingest is never served until the gate flips the
pointer, and a removal is served at once (§2.3: removal is a compliance action).

This is the same view as `eval.release.serving_corpus`, built here without the eval extra,
which the demo host does not install (D3.2); `tests/serving` keeps the two equal. Unlike that
function, a pointer that cannot be read is an error here, never "nothing live": the pages lock.

Which files a search may find (both versions in one vector store): ingest tags every file with
`CURRENT`'s `corpus_version` and, while `CURRENT` adds or changes units the served view lacks,
the files of the served view also with that view's id as `served_version` (`served_tag`).
`ServedCorpus.read` names the attribute a search must filter on: `corpus_version` when the
served view is `CURRENT` (nothing waits, or a removal-only candidate: as before), else
`served_version`. An ungated new or changed unit carries neither the served id, so it is never
found; a flip of the pointer to `CURRENT` makes the view `CURRENT`, found by `corpus_version`
with no re-sync. A pointer moved anywhere else finds only files tagged for exactly that view
(none, until the next ingest): fail closed.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable
from pathlib import Path

from course_assistant.ingest.corpus import (
    SERVED_KEY,
    VERSION_KEY,
    Corpus,
    CorpusStore,
    corpus_version,
    unit_key,
)
from course_assistant.ingest.split import IngestError

LIVE_FILE = "LIVE.json"  # equal to eval.release.LIVE_FILE (tested)
_SHA = re.compile(r"[0-9a-f]{64}")


class ServingError(Exception):
    """No gate-approved corpus can be served. The message is fixed text: names, no content."""


def _text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ServingError(f"cannot read {path.name}") from None


def _live_version(raw: str | None) -> str:
    if raw is None:
        raise ServingError(f"no {LIVE_FILE}: nothing has passed the release gate")
    try:
        state = json.loads(raw)
    except (ValueError, RecursionError):
        raise ServingError(f"{LIVE_FILE} is not valid JSON") from None
    version = state.get("corpus_version") if isinstance(state, dict) else None
    if not isinstance(version, str) or not _SHA.fullmatch(version):
        raise ServingError(f"{LIVE_FILE} names no corpus version")
    return version


def _ids(corpus: Corpus) -> set[tuple[str, str]]:
    return {(unit_key(u), u.version) for u in corpus.units}


def _kept(live: Corpus, keep_ids: set[tuple[str, str]]) -> Corpus | None:
    keep = [u for u in live.units if (unit_key(u), u.version) in keep_ids]
    if not keep:
        return None
    if len(keep) == len(live.units):
        return live
    cv = corpus_version(keep)
    units = tuple(u.model_copy(update={"unit_index": i, "corpus_version": cv})
                  for i, u in enumerate(keep, start=1))  # fmt: skip
    return Corpus(schema_version=live.schema_version, cohort=live.cohort, corpus_version=cv,
                  units=units)  # fmt: skip


def view(live: Corpus, current: Corpus | None) -> Corpus:
    """The live corpus minus every unit (locator and version) not in `current`."""
    served = _kept(live, _ids(current) if current is not None else set())
    if served is None:
        raise ServingError("nothing of the live corpus is left to serve")
    return served


def served_tag(live: Corpus | None, candidate: Iterable[tuple[str, str]],
               candidate_version: str) -> tuple[str, frozenset[str]] | None:  # fmt: skip
    """For ingest: the id of the view the pages will serve once `candidate` ((locator, unit
    version) pairs, `candidate_version` its corpus version) is `CURRENT`, and the unit versions
    whose files must carry it as `served_version`. None when no file needs the tag: nothing
    is live, nothing of it is left, or the view is the candidate itself."""
    if live is None:
        return None
    served = _kept(live, set(candidate))
    if served is None or served.corpus_version == candidate_version:
        return None
    return served.corpus_version, frozenset(u.version for u in served.units)


def live_corpus(store: CorpusStore) -> Corpus | None:
    """The corpus the live pointer names, for ingest's `served_version` tags; None on anything
    missing or malformed (then no file is tagged, and the pages lock on the same pointer)."""
    try:
        live = store.load(_live_version(_text(store.dir / LIVE_FILE)))
    except (ServingError, IngestError):
        return None
    return live if live.cohort == store.cohort else None


class ServedCorpus:
    """Reads the pointer and `CURRENT` at every call, so a flip (or a removal) is served on the
    next page run with no restart. Corpus files are named by their content hash, so a view is
    kept per (pointer, `CURRENT`) pair and nothing is parsed twice."""

    def __init__(self, store: CorpusStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._views: dict[tuple[str, str | None], tuple[Corpus, str]] = {}

    def __repr__(self) -> str:
        return "ServedCorpus()"

    def __call__(self) -> Corpus:
        return self.read()[0]

    def read(self) -> tuple[Corpus, str]:
        """The served corpus and the attribute its files are found by (`corpus_version` or
        `served_version`), read together from one pointer and one `CURRENT`."""
        live_v = _live_version(_text(self.store.dir / LIVE_FILE))
        raw_current = _text(self.store.dir / "CURRENT")
        current_v = None if raw_current is None else raw_current.strip()
        key = (live_v, current_v)
        with self._lock:
            hit = self._views.get(key)
        if hit is not None:
            return hit
        try:
            live = self.store.load(live_v)
            current = None if current_v is None else self.store.load(current_v)
        except IngestError:
            raise ServingError("the live or current corpus version cannot be read") from None
        if live.cohort != self.store.cohort:
            raise ServingError("the live corpus belongs to another cohort")
        served = view(live, current)
        found_by = VERSION_KEY if served.corpus_version == current_v else SERVED_KEY
        with self._lock:
            if len(self._views) > 8:
                self._views.clear()
            self._views[key] = (served, found_by)
        return served, found_by


def serving_corpus(store: CorpusStore) -> Corpus:
    """The served corpus of `store`, read now; raises `ServingError` when there is none."""
    return ServedCorpus(store)()
