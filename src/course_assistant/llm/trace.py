"""Trace records with no learner or answer text (OAI-05, D-14, D-15).

`strip_text` is an allowlist: a key reaches a sink only if it is listed here, so a new field
holding text can never slip into a trace by being forgotten in a denylist.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import stat
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Any, Protocol

TRACE_KEYS = frozenset({
    "request_id", "step", "version", "prompt_hash", "rules_hash", "purpose", "model",
    "model_snapshot", "learner_pid", "conversation_id", "question_id", "turn_index",
    "route", "status", "top_score", "n_citations", "n_hits", "grounding_passed",
    "integrity_passed", "tokens_in", "tokens_cached", "tokens_cache_write", "tokens_out",
    "tokens_reasoning",
    "file_search_calls", "cost_usd", "latency_ms", "attempts", "error", "refusal", "at",
})  # fmt: skip


# String-valued keys and the exact shapes they may take: an enum, a generated id or a model
# name. Anything else (a one-word answer in "status", "Paris" as a pid) is dropped.
_ENUMS = {
    "purpose": {"answer", "route", "support", "integrity", "hint", "digest", "eval", "ingest"},
    "route": {"course", "graded_work", "admin", "personal", "off_topic", "injection"},
    "status": {"answered", "not_covered", "hint_only", "handed_over", "error"},
    "error": {"transport", "parse", "config"},
    "step": {"route", "answer", "ground", "integrity", "hint", "finalize", "pause", "window"},
}
# model names: known tiers only, so a learner word cannot pose as a suffix ("gpt-5-zebrafish")
_MODEL = re.compile(r"gpt-\d[\d.]*(-(sol|luna|terra|mini|nano|pro))?(-\d{4}-\d{2}-\d{2})?")
MODEL_PATTERN = _MODEL  # shared with the ledger, so neither can store a free-text model

_PATTERNS = {
    "learner_pid": re.compile(r"[0-9a-f]{16}"),
    "question_id": re.compile(r"q-[0-9a-f]{12,32}"),
    "conversation_id": re.compile(r"c-[0-9a-f]{12,32}"),
    "request_id": re.compile(r"r-[0-9a-f]{16}"),
    "model": _MODEL,
    "model_snapshot": _MODEL,
    "version": re.compile(r"v?\d{1,4}(\.\d{1,4}){0,2}|(?=[0-9a-f]*\d)[0-9a-f]{8,64}"),
    "prompt_hash": re.compile(r"[0-9a-f]{12,64}"),
    "rules_hash": re.compile(r"[0-9a-f]{12,64}"),
    "at": re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(\+00:00|Z)?"),
}


def _safe(key: str, value: Any) -> bool:
    if value is None or (isinstance(value, bool | int | float) and key not in _ENUMS):
        if isinstance(value, float) and not math.isfinite(value):
            return False
        return key not in _PATTERNS or value is None
    if not isinstance(value, str):
        return False
    if key in _ENUMS:
        return value in _ENUMS[key]
    pattern = _PATTERNS.get(key)
    return pattern is not None and pattern.fullmatch(value) is not None


def strip_text(record: dict[str, Any]) -> dict[str, Any]:
    """Allowlisted keys whose values have the exact shape allowed for that key."""
    return {k: v for k, v in record.items() if k in TRACE_KEYS and _safe(k, v)}


class TraceSink(Protocol):
    def emit(self, record: dict[str, Any]) -> None: ...


class DeletableSink(TraceSink, Protocol):
    """A sink that can remove a learner's lines (B2.2, US9)."""

    def delete_pid(self, pid: str, conversation_ids: Collection[str] | None = None,
                   known: Collection[str] = ()) -> int: ...  # fmt: skip


def _record(line: str) -> dict[str, Any] | None:
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


class JsonlSink:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """One exclusive lock, on a sidecar file (the data file's inode changes on rewrite),
        shared by emit and delete_pid, so no line is appended during a rewrite and lost
        (verifier B2.2 pass 1)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a", encoding="utf-8") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)

    def emit(self, record: dict[str, Any]) -> None:
        line = json.dumps(strip_text(record), sort_keys=True, default=str) + "\n"
        with self._locked(), self.path.open("a+", encoding="utf-8") as fh:
            # a crash can leave a torn last line with no newline: start on a fresh line so
            # this record never fuses with another learner's fragment (verifier B2.2 pass 5)
            if fh.tell() > 0:
                fh.seek(fh.tell() - 1)
                if fh.read(1) != "\n":
                    line = "\n" + line
            fh.write(line)

    def delete_pid(self, pid: str, conversation_ids: Collection[str] | None = None,
                   known: Collection[str] = ()) -> int:  # fmt: skip
        """Trusts its caller: `conversation_ids` and `known` must be the learner's own (privacy
        .delete guarantees it through DeletionRequest.covers). Remove the learner's lines (all of
        them, or those of the given conversations) and return how many went. A line is the
        learner's if it carries their pid, or carries no pid but one of their conversation ids:
        `known` (from the deletion request) plus every conversation id on an intact line of
        theirs. A damaged line goes if it names the pid or one of those conversations as a whole
        id. The file is rewritten to a temporary file and swapped in, so a crash leaves either the
        old or the new file.
        """
        with self._locked():
            return self._delete(pid, conversation_ids, set(known))

    def _delete(self, pid: str, conversation_ids: Collection[str] | None, known: set[str]) -> int:
        tmp = self.path.with_name(self.path.name + ".tmp")
        if tmp.is_dir() and not tmp.is_symlink():
            tmp.rmdir()  # an empty leftover directory; a non-empty one fails loudly
        elif tmp.is_symlink() or tmp.exists():
            tmp.unlink()  # a leftover from a crash, or something planted (verifier B2.2 pass 2)
        if not self.path.is_file():
            return 0
        lines = [x for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]
        mine = set(known)
        for line in lines:
            rec = _record(line)
            if rec is not None and rec.get("learner_pid") == pid:
                cid = rec.get("conversation_id")
                if isinstance(cid, str):
                    mine.add(cid)
        cids = mine if conversation_ids is None else set(conversation_ids)
        names = (cids | {pid}) if conversation_ids is None else cids
        kept: list[str] = []
        dropped = 0
        for line in lines:
            if self._doomed(line, pid, cids, names, conversation_ids is None):
                dropped += 1
            else:
                kept.append(line)
        if dropped:
            mode = stat.S_IMODE(self.path.stat().st_mode)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(tmp, flags, mode)
            os.chmod(tmp, mode)  # keep the original permissions, never wider (pass 1)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("".join(f"{x}\n" for x in kept))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)  # the rename itself survives a crash
            finally:
                os.close(dfd)
        return dropped

    @staticmethod
    def _doomed(line: str, pid: str, cids: set[str], names: set[str], whole: bool) -> bool:
        rec = _record(line)
        if rec is None:
            # a damaged line: drop it only if it names one of the ids as a whole id, never as
            # part of a longer one (verifier B2.2 passes 1 and 2)
            return any(re.search(rf"(?<![0-9A-Za-z-]){re.escape(n)}(?![0-9A-Za-z-])", line)
                       for n in names)  # fmt: skip
        owner = rec.get("learner_pid")
        if owner is not None and owner != pid:
            return False  # another learner's line, whatever conversation id it names
        if whole and owner == pid:
            return True
        return rec.get("conversation_id") in cids
