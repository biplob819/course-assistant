"""P4.1 groundwork: the served (gate-approved) corpus and a waiting candidate share one vector
store. Ingest tags every file with the candidate's `corpus_version` and, while a candidate
that adds or changes units waits for the release gate, the files of the served view also with
`served_version`; the answer and hint searches select the served version's files only. An
ungated new or changed unit is never retrievable, the served version keeps answering, a
removal-only candidate behaves as before, and a flip of the live pointer needs no re-sync.

No network: ingest writes to the in-memory fake vector store (SDK models), and the search
filter is evaluated here over the attributes the fake holds, as the file search applies it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from build_sample_course import pdf
from course_assistant.answer.answer import AnswerStep
from course_assistant.answer.hint import HintStep
from course_assistant.ingest import ATTRIBUTE_KEYS, CorpusStore, apply, plan
from course_assistant.ingest.corpus import Corpus
from course_assistant.ingest.serving import LIVE_FILE, ServedCorpus, ServingError, live_corpus
from course_assistant.schemas.config import AppConfig
from tests.ingest.conftest import CONFIG, write_course
from tests.ingest.fakes import FakeVectorStore

TEXT = "Enough words on this page to keep it in the corpus."
NOT_A_RESPONDER: Any = object()  # nothing is called: only the filters are built


def matches(flt: dict[str, Any], attrs: dict[str, Any]) -> bool:
    """The file search's compound filter over one file's attributes (eq / and / or)."""
    kind = flt["type"]
    if kind == "and":
        return all(matches(f, attrs) for f in flt["filters"])
    if kind == "or":
        return any(matches(f, attrs) for f in flt["filters"])
    assert kind == "eq", kind  # the only comparison the steps use
    return flt["key"] in attrs and attrs[flt["key"]] == flt["value"]


def hits(fake: FakeVectorStore, flt: dict[str, Any]) -> set[str]:
    return {fid for fid, f in fake.attached.items() if matches(flt, dict(f.attributes or {}))}


class Pilot:
    """One course in tmp_path: pages of one pre-read, synced to the fake store."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.store = CorpusStore(tmp / "corpus_store", "sample")
        self.fake = FakeVectorStore()
        self.config = AppConfig.model_validate(CONFIG)

    def sync(self, pages: list[tuple[str, str]]) -> Corpus:
        data = pdf(pages, "pre-read")
        m = write_course(self.tmp, [("p.pdf", "s01", "pre_read", data)])
        p = plan(m, self.store.ledger(), root=self.tmp, live=live_corpus(self.store))
        return apply(p, self.fake, vector_store_id=self.fake.vector_store_id, store=self.store,
                     sleep=lambda _: None)  # fmt: skip

    def flip(self, version: str) -> None:
        """What the release gate writes on a pass (E2.4 `flip_live`), minus the run details."""
        state = {"corpus_version": version, "how": "gate", "settings": {}}
        (self.store.dir / LIVE_FILE).write_text(json.dumps(state), encoding="utf-8")

    def served(self) -> tuple[Corpus, str]:
        return ServedCorpus(self.store).read()

    def answer_hits(self, session: str | None = None) -> set[str]:
        corpus, key = self.served()
        step = AnswerStep(self.config, corpus, NOT_A_RESPONDER, version_key=key)
        return hits(self.fake, step.filters(session))

    def hint_hits(self) -> set[str]:
        corpus, key = self.served()
        step = HintStep(self.config, corpus, NOT_A_RESPONDER, version_key=key)
        return hits(self.fake, step.filters(["s01"]))

    def files(self, corpus: Corpus) -> set[str]:
        return {u.file_id for u in corpus.units}


ONE, TWO, THREE = ("One", TEXT), ("Two", TEXT), ("Three", TEXT)
TWO_CHANGED = ("Two, changed", TEXT + " A changed sentence.")


@pytest.fixture
def pilot(tmp_path: Path) -> Pilot:
    p = Pilot(tmp_path)
    first = p.sync([ONE, TWO])
    p.flip(first.corpus_version)
    return p


def test_steady_state_answers_from_corpus_version_as_before(pilot):
    corpus, key = pilot.served()
    assert key == "corpus_version"
    step = AnswerStep(pilot.config, corpus, NOT_A_RESPONDER, version_key=key)
    default = AnswerStep(pilot.config, corpus, NOT_A_RESPONDER)
    assert step.filters(None) == default.filters(None)  # the request (and its replay hash)
    assert step.filters(None) == {"type": "eq", "key": "corpus_version",
                                  "value": corpus.corpus_version}  # fmt: skip
    assert pilot.answer_hits() == pilot.hint_hits() == pilot.files(corpus)


@pytest.mark.safety
def test_a_waiting_candidate_is_never_retrievable_and_the_served_version_keeps_answering(pilot):
    live, _ = pilot.served()
    candidate = pilot.sync([ONE, TWO_CHANGED, THREE])  # page 2 changed, page 3 new
    served, key = pilot.served()
    assert key == "served_version"
    assert [u.version for u in served.units] == [live.units[0].version]  # page 1 only
    ungated = pilot.files(candidate) - pilot.files(served)
    assert len(ungated) == 2
    for found in (pilot.answer_hits(), pilot.answer_hits("s01"), pilot.hint_hits()):
        assert found == pilot.files(served)  # never empty, never an ungated unit
        assert not found & ungated
    # the plain corpus_version filter of the served id would match nothing at all
    plain = AnswerStep(pilot.config, served, NOT_A_RESPONDER).filters(None)
    assert hits(pilot.fake, plain) == set()


def test_flipping_the_pointer_serves_the_candidate_with_no_re_sync(pilot):
    candidate = pilot.sync([ONE, TWO_CHANGED, THREE])
    writes = len(pilot.fake.writes())
    pilot.flip(candidate.corpus_version)
    served, key = pilot.served()
    assert served == candidate and key == "corpus_version"
    assert pilot.answer_hits() == pilot.hint_hits() == pilot.files(candidate)
    assert len(pilot.fake.writes()) == writes  # nothing was written to the store
    pilot.sync([ONE, TWO_CHANGED, THREE])  # the next run drops the stale served tag
    before = len(pilot.fake.writes())
    pilot.sync([ONE, TWO_CHANGED, THREE])
    assert len(pilot.fake.writes()) == before  # and then a run with no change writes nothing


def test_a_removal_only_candidate_is_served_at_once_as_before(pilot):
    removal = pilot.sync([ONE])
    served, key = pilot.served()
    assert served == removal and key == "corpus_version"
    assert all(f.attributes and f.attributes["served_version"] == ""
               for f in pilot.fake.attached.values())  # fmt: skip
    assert pilot.answer_hits() == pilot.files(removal)


def test_a_new_candidate_replaces_the_served_tags_of_the_one_before(pilot):
    pilot.sync([ONE, TWO, THREE])  # adds page 3
    pilot.sync([ONE, TWO_CHANGED])  # instead: page 3 gone again, page 2 changed
    served, key = pilot.served()
    assert key == "served_version" and len(served.units) == 1
    assert pilot.answer_hits() == pilot.files(served)


@pytest.mark.safety
@pytest.mark.parametrize("pointer", ["missing", "not json", "unknown version"])
def test_an_unreadable_pointer_at_sync_tags_no_served_file(tmp_path, pointer):
    p = Pilot(tmp_path)
    p.sync([ONE, TWO])
    live = p.store.dir / LIVE_FILE
    if pointer == "not json":
        live.write_text("{", encoding="utf-8")
    if pointer == "unknown version":
        p.flip("f" * 64)
    assert live_corpus(p.store) is None
    p.sync([ONE, TWO, THREE])
    assert {f.attributes["served_version"] for f in p.fake.attached.values()
            if f.attributes} == {""}  # fmt: skip
    with pytest.raises(ServingError):
        p.served()  # and the pages lock: nothing is served


@pytest.mark.safety
def test_a_pointer_moved_elsewhere_without_a_sync_fails_closed(pilot):
    second = pilot.sync([ONE, TWO, THREE])
    pilot.sync([ONE, TWO_CHANGED, THREE])
    # the pointer names a version the last sync did not tag a served view for: nothing of
    # the candidate, and nothing outside the view, can be found (here: nothing at all)
    pilot.flip(second.corpus_version)
    served, key = pilot.served()
    assert key == "served_version" and len(served.units) == 2
    assert pilot.answer_hits() == pilot.hint_hits() == set()


@pytest.mark.safety
def test_an_unknown_version_key_is_refused(pilot):
    corpus, _ = pilot.served()
    with pytest.raises(ValueError, match="version key"):
        AnswerStep(pilot.config, corpus, NOT_A_RESPONDER, version_key="session")
    with pytest.raises(ValueError, match="version key"):
        HintStep(pilot.config, corpus, NOT_A_RESPONDER, version_key="")


def test_the_attributes_stay_within_the_vector_store_limits(pilot):
    pilot.sync([ONE, TWO_CHANGED, THREE])
    assert "served_version" in ATTRIBUTE_KEYS and len(ATTRIBUTE_KEYS) <= 16
    for f in pilot.fake.attached.values():
        attrs = dict(f.attributes or {})
        assert set(attrs) == set(ATTRIBUTE_KEYS)
        assert all(len(k) <= 64 for k in attrs)
        assert all(len(v) <= 512 for v in attrs.values() if isinstance(v, str))
