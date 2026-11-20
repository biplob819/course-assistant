"""Pilot teardown (P4.3, NFR4): delete the cohort's vector store and its files in the
operator's OpenAI project, delete the app's own data files, check the store is gone, and
write a proof.

The proof names ids and counts only: never question text, learner ids or error messages
(an SDK error can echo request content). The client is injected, so tests make no call.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# the app's own files in its folder (CA_APP_DIR); anything else there is left alone
APP_FILES = ("app.sqlite", "app.sqlite-wal", "app.sqlite-shm", "trace.jsonl",
             "trace.jsonl.lock")  # fmt: skip


class TeardownAPI(Protocol):
    def list_files(self, vector_store_id: str) -> list[Any]: ...
    def delete_file(self, file_id: str) -> Any: ...
    def delete_store(self, vector_store_id: str) -> Any: ...
    def store_exists(self, vector_store_id: str) -> bool: ...


@dataclass
class TeardownReport:
    vector_store_id: str
    at: dt.datetime
    file_ids: list[str] = field(default_factory=list)
    files_failed: list[str] = field(default_factory=list)
    store_gone: bool = False
    app_files_removed: list[str] = field(default_factory=list)
    app_files_left: list[str] = field(default_factory=list)

    @property
    def files_deleted(self) -> int:
        return len(self.file_ids) - len(self.files_failed)

    @property
    def complete(self) -> bool:
        return self.store_gone and not self.files_failed and not self.app_files_left


def teardown(api: TeardownAPI, vector_store_id: str, app_dir: Path, *,
             now: dt.datetime) -> TeardownReport:  # fmt: skip
    """Delete every file of the store (the File objects, not only their attachment), then
    the store, then the app's data files. Keeps going past a failure and reports it."""
    report = TeardownReport(vector_store_id=vector_store_id, at=now)
    report.file_ids = sorted(f.id for f in api.list_files(vector_store_id))
    for file_id in report.file_ids:
        try:
            api.delete_file(file_id)
        except Exception:  # never the message: it can echo request content
            report.files_failed.append(file_id)
    with contextlib.suppress(Exception):  # whether it worked is checked next
        api.delete_store(vector_store_id)
    try:
        report.store_gone = not api.store_exists(vector_store_id)
    except Exception:
        report.store_gone = False
    for name in APP_FILES:
        path = Path(app_dir) / name
        if not path.exists():
            continue
        try:
            path.unlink()
            report.app_files_removed.append(name)
        except OSError:
            report.app_files_left.append(name)
    return report


def proof_dict(report: TeardownReport) -> dict[str, Any]:
    return {
        "kind": "course-assistant teardown proof",
        "at": report.at.isoformat(),
        "vector_store_id": report.vector_store_id,
        "store_gone": report.store_gone,
        "files_deleted": report.files_deleted,
        "file_ids": report.file_ids,
        "files_failed": report.files_failed,
        "app_files_removed": report.app_files_removed,
        "app_files_left": report.app_files_left,
        "complete": report.complete,
    }


def write_proof(report: TeardownReport, path: Path) -> None:
    Path(path).write_text(json.dumps(proof_dict(report), indent=2) + "\n", encoding="utf-8")
