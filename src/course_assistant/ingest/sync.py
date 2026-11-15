"""Vector store sync (ING-03 to ING-05): plan against the ledger, apply, reconcile.

`plan` reads only local files, so a dry run needs no key and makes no call. `apply` deletes
removed units first (removal is a compliance action), then uploads and waits for each new
unit, then refreshes the attributes of units whose place or corpus version moved, then
reconciles the store's file list against the plan. The ledger is saved after every write,
so a run that stops part-way is finished by the next one, and ingest only ever deletes files
it created. A second run with no change makes no write call: only the reconcile read.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from course_assistant.ingest.corpus import Corpus, CorpusStore, Ledger, Record, corpus_version
from course_assistant.ingest.corpus import ordered as order_units
from course_assistant.ingest.split import (
    Dropped,
    IngestError,
    Unit,
    single_exit,
    units_from_manifest,
)
from course_assistant.schemas._base import FrozenMap, Model, Name, PosInt, Sha256
from course_assistant.schemas.corpus import CorpusUnit, Manifest

if TYPE_CHECKING:
    from openai.types import FileDeleted, FileObject
    from openai.types.vector_stores import VectorStoreFile, VectorStoreFileDeleted

ATTRIBUTE_KEYS = ("session", "kind", "locator", "unit_index", "manifest_id", "version",
                  "corpus_version")  # fmt: skip
Attributes = dict[str, str | int]


class VectorStoreAPI(Protocol):
    """The only OpenAI calls ingest makes. Tests implement it with SDK models (L-9)."""

    def upload(self, filename: str, data: bytes) -> FileObject: ...
    def attach(
        self, vector_store_id: str, file_id: str, attributes: Attributes
    ) -> VectorStoreFile: ...
    def retrieve(self, vector_store_id: str, file_id: str) -> VectorStoreFile: ...
    def update(
        self, vector_store_id: str, file_id: str, attributes: Attributes
    ) -> VectorStoreFile: ...
    def detach(self, vector_store_id: str, file_id: str) -> VectorStoreFileDeleted: ...
    def delete_file(self, file_id: str) -> FileDeleted: ...
    def list_files(self, vector_store_id: str) -> list[VectorStoreFile]: ...


class OpenAIVectorStore:
    """VectorStoreAPI over the OpenAI SDK: `purpose="assistants"` text files, attached with
    attributes; deleting detaches from the store and, separately, deletes the File object."""

    def __init__(self, client: Any) -> None:
        self._c = client

    def upload(self, filename: str, data: bytes) -> FileObject:
        return self._c.files.create(file=(filename, data, "text/plain"), purpose="assistants")

    def attach(self, vector_store_id: str, file_id: str, attributes: Attributes) -> VectorStoreFile:
        return self._c.vector_stores.files.create(
            vector_store_id=vector_store_id, file_id=file_id, attributes=attributes
        )

    def retrieve(self, vector_store_id: str, file_id: str) -> VectorStoreFile:
        return self._c.vector_stores.files.retrieve(file_id, vector_store_id=vector_store_id)

    def update(self, vector_store_id: str, file_id: str, attributes: Attributes) -> VectorStoreFile:
        return self._c.vector_stores.files.update(
            file_id, vector_store_id=vector_store_id, attributes=attributes
        )

    def detach(self, vector_store_id: str, file_id: str) -> VectorStoreFileDeleted:
        return self._c.vector_stores.files.delete(file_id, vector_store_id=vector_store_id)

    def delete_file(self, file_id: str) -> FileDeleted:
        return self._c.files.delete(file_id)

    def list_files(self, vector_store_id: str) -> list[VectorStoreFile]:
        return list(self._c.vector_stores.files.list(vector_store_id=vector_store_id, limit=100))


class Target(Model):
    """A planned unit: its place in the corpus and the attributes its file must carry."""

    unit: Unit
    unit_index: PosInt
    attributes: FrozenMap[str | int]


class Relabel(Model):
    file_id: Name
    target: Target


class Plan(Model):
    cohort: str
    corpus_version: Sha256
    targets: tuple[Target, ...]
    uploads: tuple[Target, ...]
    relabels: tuple[Relabel, ...]
    deletions: tuple[Record, ...]
    unchanged: tuple[Relabel, ...]
    dropped: tuple[Dropped, ...]

    @property
    def writes(self) -> int:
        return len(self.uploads) + len(self.relabels) + len(self.deletions)

    def render(self, dry_run: bool = False) -> str:
        head = "ingest plan" + (" (dry run, no API calls)" if dry_run else "")
        lines = [
            f"{head} for cohort {self.cohort}: {len(self.targets)} units, "
            f"corpus {self.corpus_version[:12]}",
            f"  {len(self.uploads)} upload, {len(self.relabels)} relabel, "
            f"{len(self.deletions)} delete, {len(self.unchanged)} unchanged",
        ]
        for d in self.dropped:
            lines.append(f"  dropped {d.key} ({d.manifest_id}): {d.words} word"
                         f"{'s' * (d.words != 1)} after the title")  # fmt: skip
        lines += [f"  delete {r.key} ({r.file_id})" for r in self.deletions]
        lines += [f"  upload {t.unit.key}" for t in self.uploads]
        lines += [f"  relabel {r.target.unit.key} ({r.file_id})" for r in self.relabels]
        if not self.writes:
            lines.append("  nothing to do")
        return "\n".join(lines)


def attributes(unit: Unit, unit_index: int, version: str) -> Attributes:
    return {"session": unit.session, "kind": unit.kind, "locator": unit.key,
            "unit_index": unit_index, "manifest_id": unit.manifest_id,
            "version": unit.version, "corpus_version": version}  # fmt: skip


@single_exit
def plan(manifest: Manifest, store_state: Ledger, *, root: Path) -> Plan:
    """What apply must do to make the store match the manifest. Reads local files only."""
    split = units_from_manifest(manifest, root)
    units = order_units(split.units)
    version = corpus_version(units)
    targets = tuple(Target(unit=u, unit_index=i, attributes=attributes(u, i, version))
                    for i, u in enumerate(units, start=1))  # fmt: skip
    kept: dict[str, Record] = {}  # key -> the one completed record that still serves it
    deletions: list[Record] = []
    wanted = {t.unit.key: t for t in targets}
    for r in store_state.records:
        t = wanted.get(r.key)
        if t is not None and r.version == t.unit.version and r.status == "completed" \
                and r.key not in kept:  # fmt: skip
            kept[r.key] = r
        else:
            deletions.append(r)
    uploads: list[Target] = []
    relabels: list[Relabel] = []
    unchanged: list[Relabel] = []
    for t in targets:
        r = kept.get(t.unit.key)
        if r is None:
            uploads.append(t)
        elif dict(r.attributes) != dict(t.attributes):
            relabels.append(Relabel(file_id=r.file_id, target=t))
        else:
            unchanged.append(Relabel(file_id=r.file_id, target=t))
    return Plan(cohort=manifest.cohort, corpus_version=version, targets=targets,
                uploads=tuple(uploads), relabels=tuple(relabels), deletions=tuple(deletions),
                unchanged=tuple(unchanged), dropped=split.dropped)  # fmt: skip


def _safe(exc: BaseException) -> str:
    """A provider error as its status code or type name, never its message."""
    code = getattr(exc, "status_code", None)
    return f"HTTP {code}" if isinstance(code, int) else type(exc).__name__


def _call(what: str, fn: Callable[[], Any], *, missing_ok: bool = False) -> Any:
    failed: str | None = None
    try:
        return fn()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        if missing_ok and getattr(exc, "status_code", None) == 404:
            return None
        failed = _safe(exc)
    raise IngestError("config", f"{what} failed ({failed})")


class _Run:
    def __init__(self, client: VectorStoreAPI, vs: str, store: CorpusStore,
                 sleep: Callable[[float], None], clock: Callable[[], float], poll_s: float,
                 timeout_s: float) -> None:  # fmt: skip
        self.c, self.vs, self.store = client, vs, store
        self.sleep, self.clock, self.poll_s, self.timeout_s = sleep, clock, poll_s, timeout_s
        self.ledger = store.ledger()

    def _save(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.store.save_ledger(ledger)

    def delete(self, r: Record) -> None:
        _call(f"removing {r.key} ({r.file_id}) from the vector store",
              lambda: self.c.detach(self.vs, r.file_id), missing_ok=True)  # fmt: skip
        _call(f"deleting file {r.file_id} ({r.key})",
              lambda: self.c.delete_file(r.file_id), missing_ok=True)  # fmt: skip
        self._save(self.ledger.without(r.file_id))

    def upload(self, t: Target) -> None:
        key = t.unit.key
        filename = key.replace(":", "-") + ".txt"
        f = _call(f"uploading {key}", lambda: self.c.upload(filename, t.unit.text.encode("utf-8")))
        attrs: Attributes = dict(t.attributes)
        rec = Record(file_id=f.id, key=key, version=t.unit.version, status="uploaded",
                     attributes=attrs)  # fmt: skip
        self._save(self.ledger.put(rec))
        _call(f"attaching {key} ({f.id})", lambda: self.c.attach(self.vs, f.id, attrs))
        self._save(self.ledger.put(rec.model_copy(update={"status": "attached"})))
        status = self._wait(key, f.id)
        if status != "completed":
            self.delete(rec)  # ours, and useless: never leave it behind
            raise IngestError("config", f"{key} ({f.id}) processing {status}", [f.id])
        self._save(self.ledger.put(rec.model_copy(update={"status": "completed"})))

    def _wait(self, key: str, file_id: str) -> str:
        start = self.clock()
        while True:
            vsf = _call(f"checking {key} ({file_id})", lambda: self.c.retrieve(self.vs, file_id))
            if vsf.status != "in_progress":
                return str(vsf.status)
            if self.clock() - start >= self.timeout_s:
                raise IngestError(
                    "config", f"{key} ({file_id}) did not finish processing in "
                    f"{self.timeout_s:.0f} s", [file_id]
                )  # fmt: skip
            self.sleep(self.poll_s)

    def relabel(self, r: Relabel) -> None:
        attrs: Attributes = dict(r.target.attributes)
        _call(f"updating attributes of {r.target.unit.key} ({r.file_id})",
              lambda: self.c.update(self.vs, r.file_id, attrs))  # fmt: skip
        old = next(x for x in self.ledger.records if x.file_id == r.file_id)
        self._save(self.ledger.put(old.model_copy(update={"attributes": attrs})))

    def reconcile(self, p: Plan) -> dict[str, str]:
        """The store's files must be exactly the planned units' files, else exit 2."""
        listed = _call("listing the vector store", lambda: self.c.list_files(self.vs))
        remote = {f.id for f in listed}
        by_key = {r.key: r.file_id for r in self.ledger.records if r.status == "completed"}
        planned = {by_key[t.unit.key] for t in p.targets if t.unit.key in by_key}
        missing_units = [t.unit.key for t in p.targets if t.unit.key not in by_key]
        differ = sorted(remote ^ planned) + missing_units
        extra_records = {r.file_id for r in self.ledger.records} - planned
        differ += sorted(extra_records - set(differ))
        if differ:
            raise IngestError(
                "reconcile",
                f"vector store {self.vs} does not match the plan; differing ids: "
                + ", ".join(differ),
                differ,
            )
        return by_key


@single_exit
def apply(
    p: Plan,
    client: VectorStoreAPI,
    *,
    vector_store_id: str,
    store: CorpusStore,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    poll_s: float = 1.0,
    timeout_s: float = 600.0,
) -> Corpus:
    """Make the vector store match the plan, reconcile, and record the corpus version.
    `sleep` and `clock` default to `time.sleep` and `time.monotonic`, looked up per call."""
    run = _Run(client, vector_store_id, store, sleep or time.sleep, clock or time.monotonic,
               poll_s, timeout_s)  # fmt: skip
    planned_ids = {r.file_id for r in p.deletions}
    stale = [r for r in run.ledger.records if r.file_id in planned_ids]
    if len(stale) != len(p.deletions):
        raise IngestError("config", "the ledger changed since the plan was made; plan again")
    for r in stale:
        run.delete(r)
    for t in p.uploads:
        run.upload(t)
    for r in p.relabels:
        run.relabel(r)
    by_key = run.reconcile(p)
    units = tuple(
        CorpusUnit(
            unit_index=t.unit_index,
            file_id=by_key[t.unit.key],
            vs_file_id=by_key[t.unit.key],
            manifest_id=t.unit.manifest_id,
            session=t.unit.session,
            kind=t.unit.kind,
            locator=t.unit.locator,
            label=t.unit.label,
            text=t.unit.text,
            version=t.unit.version,
            corpus_version=p.corpus_version,
        )
        for t in p.targets
    )
    corpus = Corpus(schema_version="1.0", cohort=p.cohort, corpus_version=p.corpus_version,
                    units=units)  # fmt: skip
    store.save(corpus)
    return corpus
