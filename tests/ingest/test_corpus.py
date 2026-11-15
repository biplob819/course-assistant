"""ING-02: stable order and unit_index, corpus version semantics, the corpus store, and
every golden-set locator resolving (D-30)."""

from __future__ import annotations

import hashlib
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from build_sample_course import pdf
from course_assistant.ingest import (
    KIND_ORDER,
    Corpus,
    CorpusStore,
    IngestError,
    Ledger,
    Unit,
    apply,
    corpus_version,
    ordered,
    plan,
    unit_version,
)
from harness_lib import ROOT
from tests.ingest.conftest import write_course
from tests.ingest.fakes import FakeVectorStore


def unit(session: str, kind: str, n: int, text: str = "Some unit text here.") -> Unit:
    key = f"{session}:{kind}:{n}"
    return Unit(manifest_id=f"m-{session}-{kind.replace('_', '-')}", session=session,
                kind=kind,  # type: ignore[arg-type]
                number=n, key=key, locator=f"p. {n}", label=f"label {n}", text=text,
                version=unit_version(key, text))  # fmt: skip


def golden_locators() -> set[str]:
    out: set[str] = set()
    for line in (
        (ROOT / "golden" / "public" / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ):
        out |= set(json.loads(line).get("gold_units", []))
    return out


def test_units_are_ordered_session_kind_manifest_order_locator(sample_plan):
    keys = [t.unit.key for t in sample_plan.targets]
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    sort_key = [(t.unit.session, rank[t.unit.kind], t.unit.number) for t in sample_plan.targets]
    assert sort_key == sorted(sort_key)
    assert keys[0] == "s01:slides:1" and keys[-1] == "s03:agenda:1"
    assert [t.unit_index for t in sample_plan.targets] == list(range(1, len(keys) + 1))
    assert KIND_ORDER == ("slides", "pre_read", "agenda", "post_session", "faq")


def test_unit_index_is_stable_when_the_manifest_is_reordered(sample_manifest, sample_plan):
    reordered = sample_manifest.model_copy(
        update={"entries": tuple(reversed(sample_manifest.entries))}
    )
    again = plan(reordered, Ledger.empty(), root=ROOT)
    assert [(t.unit.key, t.unit_index) for t in again.targets] == [
        (t.unit.key, t.unit_index) for t in sample_plan.targets
    ]
    assert again.corpus_version == sample_plan.corpus_version


@settings(max_examples=60, deadline=None)
@given(st.permutations([unit(s, k, n) for s in ("s01", "s02") for k in ("faq", "slides", "agenda")
                        for n in (1, 2, 10)]))  # fmt: skip
def test_order_is_the_same_for_any_input_order(units):
    got = [u.key for u in ordered(units)]
    assert got == [u.key for u in ordered(sorted(units, key=lambda u: u.key))]
    assert got[:3] == ["s01:slides:1", "s01:slides:2", "s01:slides:10"]


def test_duplicate_locators_are_refused():
    with pytest.raises(IngestError, match="s01:faq:1"):
        ordered([unit("s01", "faq", 1), unit("s01", "faq", 1, "Other text here now.")])


def test_corpus_version_changes_when_and_only_when_text_or_locator_changes():
    base = [unit("s01", "slides", 1), unit("s01", "slides", 2, "Second slide text here.")]
    v = corpus_version(base)
    assert v == hashlib.sha256("\n".join(sorted(u.version for u in base)).encode()).hexdigest()
    assert corpus_version(list(reversed(base))) == v  # order does not matter
    text = [base[0], unit("s01", "slides", 2, "Second slide text changed.")]
    moved = [base[0], unit("s01", "slides", 3, "Second slide text here.")]
    assert corpus_version(text) != v and corpus_version(moved) != v
    relabelled = [base[0], base[1].model_copy(update={"label": "another label",
                                                      "manifest_id": "m-other"})]  # fmt: skip
    assert corpus_version(relabelled) == v


def test_a_new_file_with_the_same_text_keeps_the_corpus_version(tmp_path):
    pages = [("Heading", "Some page text with enough words.")]
    one = write_course(tmp_path, [("p.pdf", "s01", "pre_read", pdf(pages, "first title"))])
    v1 = plan(one, Ledger.empty(), root=tmp_path).corpus_version
    two = write_course(tmp_path, [("p.pdf", "s01", "pre_read", pdf(pages, "second title"))])
    assert one.entries[0].sha256 != two.entries[0].sha256
    assert plan(two, Ledger.empty(), root=tmp_path).corpus_version == v1
    edited = pdf([("Heading", "Some page text, edited.")], "t")
    three = write_course(tmp_path, [("p.pdf", "s01", "pre_read", edited)])
    assert plan(three, Ledger.empty(), root=tmp_path).corpus_version != v1


def test_every_public_golden_locator_resolves(sample_plan, tmp_path):
    locs = golden_locators()
    assert len(locs) >= 30
    keys = {t.unit.key for t in sample_plan.targets}
    assert locs <= keys, sorted(locs - keys)
    fake = FakeVectorStore()
    store = CorpusStore(tmp_path / "corpus_store", "sample")
    corpus = apply(sample_plan, fake, vector_store_id="vs_test", store=store, sleep=lambda s: None)
    for loc in sorted(locs):
        u = corpus.resolve(loc)
        session, kind, n = loc.split(":")
        assert (u.session, u.kind) == (session, kind)
        assert u.locator in (f"slide {n}", f"p. {n}", "agenda")
    with pytest.raises(IngestError, match="s01:slides:20"):
        corpus.resolve("s01:slides:20")  # the dropped canvas slide


def test_the_store_keeps_one_json_file_per_corpus_version(sample_plan, tmp_path):
    store = CorpusStore(tmp_path / "corpus_store", "sample")
    assert store.current() is None
    corpus = apply(sample_plan, FakeVectorStore(), vector_store_id="vs_test", store=store,
                   sleep=lambda s: None)  # fmt: skip
    path = tmp_path / "corpus_store" / "sample" / f"corpus-{corpus.corpus_version}.json"
    assert path.is_file()
    assert store.load(corpus.corpus_version) == corpus == store.current()
    assert all(u.corpus_version == corpus.corpus_version for u in corpus.units)
    assert [u.unit_index for u in corpus.units] == list(range(1, len(corpus.units) + 1))
    assert corpus.corpus_version == sample_plan.corpus_version
    with pytest.raises(IngestError):
        store.load("0" * 64)


def test_a_corpus_with_gaps_or_a_wrong_version_is_refused(sample_plan, tmp_path):
    store = CorpusStore(tmp_path / "cs", "sample")
    corpus = apply(sample_plan, FakeVectorStore(), vector_store_id="vs_test", store=store,
                   sleep=lambda s: None)  # fmt: skip
    units = list(corpus.units)
    with pytest.raises(ValueError, match="unit_index"):
        Corpus(schema_version="1.0", cohort="sample", corpus_version=corpus.corpus_version,
               units=tuple(units[1:]))  # fmt: skip
    with pytest.raises(ValueError, match="corpus_version"):
        Corpus(schema_version="1.0", cohort="sample", corpus_version="f" * 64,
               units=tuple(units))  # fmt: skip
