"""ING-03 to ING-05: upload with all seven attributes and wait; delete removed units from the
vector store and from file storage; reconcile the store's file list against the plan."""

from __future__ import annotations

import json

import pytest

from build_sample_course import pdf
from course_assistant.ingest import (
    ATTRIBUTE_KEYS,
    CorpusStore,
    IngestError,
    OpenAIVectorStore,
    apply,
    plan,
    unit_key,
)
from tests.ingest.conftest import write_course
from tests.ingest.fakes import FakeVectorStore, status_error


def nosleep(seconds: float) -> None:
    return None


def page(heading: str, text: str = "Enough words on this page to keep it.") -> tuple[str, str]:
    return (heading, text)


def course(tmp_path, headings: list[str], extra: list[tuple[str, str, str, bytes]] | None = None):
    data = pdf([page(h) for h in headings], "pre-read")
    return write_course(tmp_path, [("p.pdf", "s01", "pre_read", data), *(extra or [])])


def run(manifest, root, fake, store, **kw):
    p = plan(manifest, store.ledger(), root=root)
    kw.setdefault("sleep", nosleep)
    return p, apply(p, fake, vector_store_id=fake.vector_store_id, store=store, **kw)


@pytest.fixture
def store(tmp_path):
    return CorpusStore(tmp_path / "corpus_store", "sample")


# ---------- ING-03 ----------


def test_new_units_are_uploaded_and_attached_with_all_seven_attributes(sample_plan, store):
    fake = FakeVectorStore()
    corpus = apply(sample_plan, fake, vector_store_id="vs_test", store=store, sleep=nosleep)
    uploads = fake.names("upload")
    attaches = fake.names("attach")
    assert len(uploads) == len(attaches) == len(sample_plan.targets) == len(corpus.units)
    assert ATTRIBUTE_KEYS == ("session", "kind", "locator", "unit_index", "manifest_id",
                              "version", "corpus_version")  # fmt: skip
    by_file = {u.file_id: u for u in corpus.units}
    for (filename, data), (vs, file_id, attrs) in zip(uploads, attaches, strict=True):
        u = by_file[file_id]
        assert vs == "vs_test" and filename.endswith(".txt")
        assert data == u.text.encode("utf-8")
        assert set(attrs) == set(ATTRIBUTE_KEYS)
        assert attrs["session"] == u.session and attrs["kind"] == u.kind
        assert attrs["unit_index"] == u.unit_index and isinstance(attrs["unit_index"], int)
        assert attrs["manifest_id"] == u.manifest_id and attrs["version"] == u.version
        assert attrs["corpus_version"] == corpus.corpus_version
        assert attrs["locator"] == f"{u.session}:{u.kind}:{attrs['locator'].rsplit(':', 1)[1]}"
        assert u.vs_file_id == u.file_id
    assert fake.attached["file-0001"].attributes == dict(attaches[0][2])


def test_ingest_waits_for_each_file_to_complete(tmp_path, store):
    fake = FakeVectorStore()
    slept: list[float] = []
    m = course(tmp_path, ["One", "Two"])
    p = plan(m, store.ledger(), root=tmp_path)
    apply(p, fake, vector_store_id="vs_test", store=store, sleep=slept.append, poll_s=0.5)
    order = [name for name, _ in fake.calls if name != "list_files"]
    assert order == ["upload", "attach", "retrieve", "retrieve"] * 2
    assert slept == [0.5, 0.5]
    assert all(v.status == "completed" for v in fake.attached.values())
    assert [r.status for r in store.ledger().records] == ["completed", "completed"]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_a_failed_or_cancelled_file_stops_the_run(tmp_path, store, status):
    fake = FakeVectorStore(final={"s01-pre_read-2.txt": status})
    m = course(tmp_path, ["One", "Two", "Three"])
    with pytest.raises(IngestError) as exc:
        run(m, tmp_path, fake, store)
    assert exc.value.kind == "config"
    assert status in str(exc.value) and "s01:pre_read:2" in str(exc.value)
    assert len(fake.names("upload")) == 2  # the run stopped there
    failed = fake.names("attach")[1][1]
    assert failed not in fake.files and failed not in fake.attached  # cleaned up, it was ours
    assert [r.key for r in store.ledger().records] == ["s01:pre_read:1"]
    assert store.current() is None
    # the next run finishes the job and nothing is orphaned
    fake.final.clear()
    run(m, tmp_path, fake, store)
    assert sorted(fake.files) == sorted(fake.attached)
    assert len(fake.attached) == 3


def test_processing_that_never_finishes_is_a_config_error(tmp_path, store):
    fake = FakeVectorStore(final={"s01-pre_read-1.txt": "in_progress"})
    m = course(tmp_path, ["One"])
    clock = iter(range(0, 10_000, 100))
    with pytest.raises(IngestError, match="did not finish"):
        run(m, tmp_path, fake, store, clock=lambda: float(next(clock)), timeout_s=300.0)


# ---------- ING-04 ----------


def test_a_removed_unit_leaves_the_store_and_its_file_object_is_deleted(tmp_path, store):
    fake = FakeVectorStore()
    agenda = pdf([page("Agenda", "Session one agenda with timings and topics.")], "agenda")
    m = course(tmp_path, ["One", "Two"], [("a.pdf", "s01", "agenda", agenda)])
    _, first = run(m, tmp_path, fake, store)
    gone = first.resolve("s01:agenda:1").file_id
    smaller = course(tmp_path, ["One", "Two"])
    p, second = run(smaller, tmp_path, fake, store)
    assert [r.file_id for r in p.deletions] == [gone]
    assert fake.names("detach") == [("vs_test", gone)]
    assert fake.names("delete_file") == [(gone,)]
    assert gone not in fake.attached and gone not in fake.files
    assert {unit_key(u) for u in second.units} == {"s01:pre_read:1", "s01:pre_read:2"}
    assert gone not in {r.file_id for r in store.ledger().records}


def test_a_changed_unit_replaces_the_old_file(tmp_path, store):
    fake = FakeVectorStore()
    _, first = run(course(tmp_path, ["One", "Two"]), tmp_path, fake, store)
    old = first.resolve("s01:pre_read:2").file_id
    data = pdf([page("One"), page("Two", "Rewritten page two text.")], "x")
    m = write_course(tmp_path, [("p.pdf", "s01", "pre_read", data)])
    p, second = run(m, tmp_path, fake, store)
    assert [t.unit.key for t in p.uploads] == ["s01:pre_read:2"]
    assert [r.file_id for r in p.deletions] == [old]
    assert old not in fake.files and old not in fake.attached
    assert "Rewritten" in second.resolve("s01:pre_read:2").text
    assert second.corpus_version != first.corpus_version


def test_an_insertion_relabels_later_units_without_reuploading(tmp_path, store):
    fake = FakeVectorStore()
    agenda = pdf([page("Agenda", "Session one agenda with timings and topics.")], "agenda")
    _, first = run(course(tmp_path, ["One"], [("a.pdf", "s01", "agenda", agenda)]), tmp_path,
                   fake, store)  # fmt: skip
    agenda_id = first.resolve("s01:agenda:1").file_id
    fake.calls.clear()
    p, second = run(course(tmp_path, ["One", "Two"], [("a.pdf", "s01", "agenda", agenda)]),
                    tmp_path, fake, store)  # fmt: skip
    assert [t.unit.key for t in p.uploads] == ["s01:pre_read:2"]
    assert not p.deletions
    updates = fake.names("update")
    assert {u[1] for u in updates} == {agenda_id, first.resolve("s01:pre_read:1").file_id}
    agenda_attrs = fake.attached[agenda_id].attributes or {}
    assert agenda_attrs["unit_index"] == 3
    assert agenda_attrs["corpus_version"] == second.corpus_version
    assert second.resolve("s01:agenda:1").file_id == agenda_id


def test_a_failure_midway_is_recovered_by_the_next_run(tmp_path, store):
    fake = FakeVectorStore()
    m = course(tmp_path, ["One", "Two", "Three"])
    fake.fail["attach"] = status_error(500)
    with pytest.raises(IngestError):
        run(m, tmp_path, fake, store)
    run(m, tmp_path, fake, store)
    assert sorted(fake.files) == sorted(fake.attached) and len(fake.attached) == 3


def test_files_already_gone_are_not_an_error(tmp_path, store):
    fake = FakeVectorStore()
    _, first = run(course(tmp_path, ["One", "Two"]), tmp_path, fake, store)
    gone = first.resolve("s01:pre_read:2").file_id
    del fake.attached[gone]
    del fake.files[gone]
    p, _ = run(course(tmp_path, ["One"]), tmp_path, fake, store)
    assert [r.file_id for r in p.deletions] == [gone]
    assert gone not in {r.file_id for r in store.ledger().records}


def test_ingest_only_deletes_files_it_created(tmp_path, store):
    fake = FakeVectorStore()
    fake.foreign = ["file-someone-else"]
    m = course(tmp_path, ["One"])
    with pytest.raises(IngestError):
        run(m, tmp_path, fake, store)
    assert not fake.names("detach") and not fake.names("delete_file")


# ---------- ING-05 ----------


def test_after_apply_the_store_file_list_equals_the_planned_units(sample_plan, store):
    fake = FakeVectorStore()
    corpus = apply(sample_plan, fake, vector_store_id="vs_test", store=store, sleep=nosleep)
    assert fake.names("list_files") == [("vs_test",)]
    assert {u.vs_file_id for u in corpus.units} == set(fake.attached)


@pytest.mark.parametrize("drift", ["extra", "missing"])
def test_a_reconcile_difference_raises_with_the_differing_ids(tmp_path, store, drift):
    fake = FakeVectorStore()
    m = course(tmp_path, ["One", "Two"])
    if drift == "extra":
        fake.foreign = ["file-stray-1"]
        want = ["file-stray-1"]
    else:
        fake.hidden = {"file-0002"}
        want = ["file-0002"]
    with pytest.raises(IngestError) as exc:
        run(m, tmp_path, fake, store)
    assert exc.value.kind == "reconcile"
    assert list(exc.value.ids) == want and want[0] in str(exc.value)
    assert store.current() is None  # no corpus version is recorded for a failed reconcile


def test_no_error_ever_chains_provider_text(tmp_path, store):
    for method in ("upload", "attach", "retrieve", "list_files"):
        fake = FakeVectorStore(fail={method: status_error(500)})
        with pytest.raises(IngestError) as exc:
            run(course(tmp_path, ["One"]), tmp_path, fake, store)
        assert "HTTP 500" in str(exc.value) and "SECRET" not in str(exc.value)
        assert exc.value.__context__ is None and exc.value.__cause__ is None
        store = CorpusStore(tmp_path / f"cs-{method}", "sample")


def test_the_openai_adapter_calls_the_right_endpoints():
    import httpx2
    import openai

    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = request.content
        is_json = request.headers.get("content-type", "").startswith("application/json")
        parsed = json.loads(body) if is_json and body else {}
        seen.append((request.method, request.url.path, parsed))
        path = request.url.path
        vsf = {"id": "file-1", "object": "vector_store.file", "created_at": 1,
               "status": "completed", "usage_bytes": 3, "vector_store_id": "vs_1",
               "attributes": {"session": "s01"}}  # fmt: skip
        if path == "/v1/files" and request.method == "POST":
            assert b'name="purpose"' in body and b"assistants" in body and b"a.txt" in body
            out = {"id": "file-1", "object": "file", "bytes": 3, "created_at": 1,
                   "filename": "a.txt", "purpose": "assistants", "status": "processed"}  # fmt: skip
        elif path == "/v1/files/file-1":
            out = {"id": "file-1", "object": "file", "deleted": True}
        elif path == "/v1/vector_stores/vs_1/files" and request.method == "GET":
            out = {"object": "list", "data": [vsf], "has_more": False, "first_id": "file-1",
                   "last_id": "file-1"}  # fmt: skip
        elif request.method == "DELETE":
            out = {"id": "file-1", "object": "vector_store.file.deleted", "deleted": True}
        else:
            out = vsf
        return httpx2.Response(200, json=out)

    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    client = openai.OpenAI(api_key="test", base_url="https://example.invalid/v1", http_client=http)
    api = OpenAIVectorStore(client)
    assert api.upload("a.txt", b"abc").id == "file-1"
    assert api.attach("vs_1", "file-1", {"session": "s01", "unit_index": 1}).status == "completed"
    assert api.retrieve("vs_1", "file-1").id == "file-1"
    api.update("vs_1", "file-1", {"session": "s01", "unit_index": 2})
    assert [f.id for f in api.list_files("vs_1")] == ["file-1"]
    api.detach("vs_1", "file-1")
    api.delete_file("file-1")
    routes = [(m, p) for m, p, _ in seen]
    assert routes == [
        ("POST", "/v1/files"),
        ("POST", "/v1/vector_stores/vs_1/files"),
        ("GET", "/v1/vector_stores/vs_1/files/file-1"),
        ("POST", "/v1/vector_stores/vs_1/files/file-1"),
        ("GET", "/v1/vector_stores/vs_1/files"),
        ("DELETE", "/v1/vector_stores/vs_1/files/file-1"),
        ("DELETE", "/v1/files/file-1"),
    ]
    assert seen[1][2] == {"file_id": "file-1", "attributes": {"session": "s01", "unit_index": 1}}
    assert seen[3][2] == {"attributes": {"session": "s01", "unit_index": 2}}
