"""The corpus store (ING-02): units in a stable order, the corpus version, one JSON file per
corpus version, and the ledger of vector-store files this cohort's ingest has created.

`corpus_store/` holds course text, so it is git-ignored; the sample course's store is rebuilt
by running ingest, never committed.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import ValidationError, model_validator

from course_assistant.ingest.split import IngestError, Unit
from course_assistant.schemas._base import FrozenMap, Ident, Model, Name, Sha256, Versioned
from course_assistant.schemas.config import Locator
from course_assistant.schemas.corpus import CorpusUnit

KIND_ORDER = ("slides", "pre_read", "agenda", "post_session", "faq")
_RANK = {k: i for i, k in enumerate(KIND_ORDER)}


def ordered(units: Iterable[Unit]) -> tuple[Unit, ...]:
    """Session, then kind, then manifest order, then locator number. Numbers run on across
    entries of one session and kind in manifest order, so the number carries manifest order.
    unit_index is a unit's 1-based place in this order."""
    out = tuple(sorted(units, key=lambda u: (u.session, _RANK[u.kind], u.number)))
    keys = [u.key for u in out]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise IngestError("config", f"duplicate locators {', '.join(dupes)}", dupes)
    return out


def corpus_version(units: Iterable[Unit | CorpusUnit]) -> str:
    """sha256 of the sorted unit versions: changes when and only when a unit's text or
    locator changes, or a unit comes or goes."""
    return hashlib.sha256("\n".join(sorted(u.version for u in units)).encode()).hexdigest()


V = TypeVar("V", bound=Versioned)
_NUMBER = re.compile(r"(\d+)$")


def unit_key(u: CorpusUnit) -> str:
    """The D-30 locator of a stored unit: `slide 9` in s02 slides is `s02:slides:9`."""
    m = _NUMBER.search(u.locator)
    return f"{u.session}:{u.kind}:{m.group(1) if m else 1}"


class Corpus(Versioned):
    """One corpus version: every unit with its OpenAI ids."""

    cohort: Ident
    corpus_version: Sha256
    units: tuple[CorpusUnit, ...]

    @model_validator(mode="after")
    def _consistent(self) -> Corpus:
        if [u.unit_index for u in self.units] != list(range(1, len(self.units) + 1)):
            raise ValueError("unit_index must run 1, 2, 3, ... in order")
        if corpus_version(self.units) != self.corpus_version or any(
            u.corpus_version != self.corpus_version for u in self.units
        ):
            raise ValueError("corpus_version does not match the units")
        files = [u.file_id for u in self.units]
        if len(set(files)) != len(files):
            raise ValueError("file_id values must be unique")  # verifier A2.2 pass 1
        keys = [unit_key(u) for u in self.units]
        if len(set(keys)) != len(keys):
            raise ValueError("locators must be unique")
        return self

    def resolve(self, locator: str) -> CorpusUnit:
        """`s02:slides:9` to its unit (D-30); an unknown locator is an error, never a guess."""
        for u in self.units:
            if unit_key(u) == locator:
                return u
        where = f"corpus {self.corpus_version[:12]}"
        raise IngestError("config", f"locator {locator} is not in {where}", [locator])


class Record(Model):
    """A file this ingest created. `status` moves uploaded -> attached -> completed."""

    file_id: Name
    key: Locator
    version: Sha256
    status: Literal["uploaded", "attached", "completed"]
    attributes: FrozenMap[str | int]


class Ledger(Versioned):
    """Every file ingest created and has not yet deleted, saved after each write, so a run
    that stops part-way leaves an exact record and the next run finishes the job."""

    cohort: Ident
    records: tuple[Record, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> Ledger:
        ids = [r.file_id for r in self.records]
        if len(set(ids)) != len(ids):
            raise ValueError("file ids must be unique")
        return self

    @classmethod
    def empty(cls, cohort: str = "sample") -> Ledger:
        return cls(schema_version="1.0", cohort=cohort)

    def without(self, file_id: str) -> Ledger:
        return self.model_copy(update={"records": tuple(r for r in self.records
                                                        if r.file_id != file_id)})  # fmt: skip

    def put(self, record: Record) -> Ledger:
        """Add a record, or replace the one with the same file id in place."""
        rest = [r if r.file_id != record.file_id else record for r in self.records]
        if all(r.file_id != record.file_id for r in self.records):
            rest.append(record)
        return self.model_copy(update={"records": tuple(rest)})


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class CorpusStore:
    """`<directory>/<cohort>/`: `corpus-<version>.json` per corpus version, `CURRENT` naming
    the last applied version, and `ledger.json`."""

    def __init__(self, directory: Path, cohort: str) -> None:
        self.dir = Path(directory) / cohort
        self.cohort = cohort

    def _read(self, path: Path, what: str) -> str:
        failed: str | None = None
        text = ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            failed = exc.strerror or type(exc).__name__
        if failed is not None:
            raise IngestError("config", f"corpus store: cannot read {what} ({failed})")
        return text

    def ledger(self) -> Ledger:
        path = self.dir / "ledger.json"
        if not path.is_file():
            return Ledger.empty(self.cohort)
        return self._parse(Ledger, self._read(path, "the ledger"), "the ledger")

    def save_ledger(self, ledger: Ledger) -> None:
        _atomic_write(self.dir / "ledger.json", ledger.model_dump_json(indent=1) + "\n")

    def save(self, corpus: Corpus) -> Path:
        path = self.dir / f"corpus-{corpus.corpus_version}.json"
        _atomic_write(path, corpus.model_dump_json(indent=1) + "\n")
        _atomic_write(self.dir / "CURRENT", corpus.corpus_version + "\n")
        return path

    def load(self, version: str) -> Corpus:
        if not re.fullmatch(r"[0-9a-f]{64}", version):
            raise IngestError("config", "corpus version must be a sha256")
        path = self.dir / f"corpus-{version}.json"
        if not path.is_file():
            raise IngestError("config", f"corpus store has no version {version[:12]}")
        corpus = self._parse(Corpus, self._read(path, "a corpus version"), "a corpus version")
        if corpus.corpus_version != version:
            raise IngestError("config", f"corpus file {version[:12]} holds another version")
        return corpus

    def current(self) -> Corpus | None:
        path = self.dir / "CURRENT"
        if not path.is_file():
            return None
        return self.load(self._read(path, "CURRENT").strip())

    @staticmethod
    def _parse(model: type[V], text: str, what: str) -> V:
        failed = False
        result: V | None = None
        try:
            result = model.model_validate_json(text)
        except ValidationError:
            failed = True
        if failed or result is None:
            raise IngestError("config", f"corpus store: {what} is not valid")
        return result
