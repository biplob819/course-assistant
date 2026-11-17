"""A pilot course on disk for the serving tests (P4.1 groundwork): an AppConfig, a corpus
store with one ingested version, an approved manifest, and the live pointer the release gate
writes. Candidates are built from the B2.5 test corpus by adding or removing units."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import yaml

from course_assistant.ingest import Corpus, CorpusStore, corpus_version
from course_assistant.schemas.corpus import CorpusUnit
from tests.answer.test_ask import CONFIG, CORPUS
from tests.app.team_rig import manifest

LIVE = "LIVE.json"
NEW_FILE = "file-s04-slides-2"
NEW_TEXT = "Session four: a new topic that has not passed the release gate yet."


def corpus_of(units: Sequence[CorpusUnit]) -> Corpus:
    cv = corpus_version(units)
    return Corpus(schema_version="1.0", cohort=CORPUS.cohort, corpus_version=cv,
                  units=tuple(u.model_copy(update={"unit_index": i, "corpus_version": cv})
                              for i, u in enumerate(units, start=1)))  # fmt: skip


def new_unit() -> CorpusUnit:
    import hashlib

    return CORPUS.units[0].model_copy(update={
        "file_id": NEW_FILE, "vs_file_id": "vsf-new", "manifest_id": "m-s04-slides",
        "session": "s04", "locator": "slide 2", "label": "Session 4 slides, slide 2",
        "text": NEW_TEXT, "version": hashlib.sha256(NEW_TEXT.encode()).hexdigest()})  # fmt: skip


def with_addition(base: Corpus = CORPUS) -> Corpus:
    """A candidate that adds a unit (it must wait for the gate)."""
    return corpus_of([*base.units, new_unit()])


def without_session(session: str, base: Corpus = CORPUS) -> Corpus:
    """A candidate that only removes units (it is served at once)."""
    return corpus_of([u for u in base.units if u.session != session])


class Course:
    """`<tmp>/course/config.yaml`, its corpus store and manifest; `CORPUS` ingested."""

    def __init__(self, tmp: Path, *, live: bool = True) -> None:
        self.config = tmp / "course" / "config.yaml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(yaml.safe_dump(CONFIG.model_dump(mode="json")), encoding="utf-8")
        self.manifest = self.config.parent / "manifest.yaml"
        self.manifest.write_text(yaml.safe_dump(manifest().model_dump(mode="json")),
                                 encoding="utf-8")  # fmt: skip
        self.store = CorpusStore(self.config.parent / "corpus_store", CONFIG.cohort)
        self.store.save(CORPUS)
        self.app_dir = tmp / "app"
        if live:
            self.mark_live(CORPUS.corpus_version)

    @property
    def live_file(self) -> Path:
        return self.store.dir / LIVE

    def ingest(self, corpus: Corpus) -> None:
        """A new ingest: the store's `CURRENT` moves, the pointer does not."""
        self.store.save(corpus)

    def mark_live(self, version: str, **extra: object) -> None:
        """What the release gate writes on a pass (E2.4 `flip_live`), minus the run details."""
        state = {"corpus_version": version, "how": "gate", "settings": {}, **extra}
        self.live_file.write_text(json.dumps(state), encoding="utf-8")

    def env(self, **over: str) -> dict[str, str]:
        return {"CA_CONFIG": str(self.config), "CA_APP_DIR": str(self.app_dir),
                "CA_MANIFEST": str(self.manifest), **over}  # fmt: skip
