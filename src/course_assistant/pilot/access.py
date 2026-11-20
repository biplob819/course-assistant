"""Personal access codes for pilot learners (OWNER D-98).

With no single sign-on from the learning platform, each learner gets one random code, sent
privately by the teaching team. The app keeps only `HMAC-SHA256(code, PID_SECRET)` cut to 16
hex characters (`privacy.pseudonym.learner_pid`), which is also the learner's pseudonymous
id: history, consent and deletion follow the code, and no name, email or platform id ever
reaches the app. The registry file maps those ids to a label (L001, L002, ...) so a code can
be revoked by its label; who holds which label is known only to the teaching team.

`AccessCodeGate` is the passkey gate (`demo.passkey.PasskeyGate`, with its per-session,
per-address and global guess limits) checking codes against the registry instead of one
shared hash. The registry is read at every check, so a revoked code closes its open
sessions at their next step with no restart.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from course_assistant.demo._errors import GuardRefused
from course_assistant.demo.passkey import CLOSED, PasskeyGate
from course_assistant.privacy.pseudonym import MIN_SECRET, PseudonymError, learner_pid

ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no 0/O, 1/I/L, U: easy to read out
CODE_RE = re.compile(rf"[{ALPHABET}]{{4}}-[{ALPHABET}]{{4}}-[{ALPHABET}]{{4}}")
MAX_CODES = 2000


class AccessError(ValueError):
    """The registry or the secret cannot be used (fixed text, never a code)."""


def normalize(code: str) -> str:
    """Upper case with spaces and hyphens removed, so "abcd efgh-jkmn" is the same code."""
    return re.sub(r"[\s-]", "", code).upper() if isinstance(code, str) else ""


def _new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


_LABEL = re.compile(r"^L[0-9]{3,}$")
_PID = re.compile(r"^[0-9a-f]{16}$")


def load(registry: Path) -> dict[str, dict[str, Any]]:
    """The learners map {pid: {"label", "revoked"}}; AccessError when it cannot be read or
    any entry is malformed, so a damaged registry closes the page instead of crashing it."""
    try:
        data = json.loads(Path(registry).read_text(encoding="utf-8"))
        learners = data["learners"]
        if data.get("version") != 1 or not isinstance(learners, dict):
            raise ValueError
        for pid, entry in learners.items():
            if not (isinstance(pid, str) and _PID.match(pid) and isinstance(entry, dict)
                    and isinstance(entry.get("label"), str) and _LABEL.match(entry["label"])
                    and isinstance(entry.get("revoked"), bool)):  # fmt: skip
                raise ValueError
        return learners
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise AccessError("the access-code registry cannot be read") from None


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """An advisory lock beside `path`, held for a whole read-modify-write."""
    lock = Path(f"{path}.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_json(path: Path, data: Any) -> None:
    """Write through a unique temp file created mode 600, then replace: never a partial file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=1) + "\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _save(registry: Path, learners: dict[str, dict[str, Any]]) -> None:
    _write_json(registry, {"version": 1, "learners": learners})


def new_codes(registry: Path, count: int, secret: str) -> list[tuple[str, str]]:
    """Add `count` new codes to the registry (created if missing); return (label, code)
    pairs, the only place the codes appear."""
    if not 1 <= count <= MAX_CODES:
        raise AccessError(f"make between 1 and {MAX_CODES} codes at a time")
    if not isinstance(secret, str) or len(secret.strip()) < MIN_SECRET:
        raise AccessError("PID_SECRET is missing or too short")
    with _locked(registry):
        learners = load(registry) if Path(registry).exists() else {}
        start = 1 + max((int(v["label"][1:]) for v in learners.values()), default=0)
        rows: list[tuple[str, str]] = []
        while len(rows) < count:
            code = _new_code()
            pid = learner_pid(normalize(code), secret)
            if pid in learners:
                continue  # astronomically rare; never reuse an id
            label = f"L{start + len(rows):03d}"
            learners[pid] = {"label": label, "revoked": False}
            rows.append((label, code))
        _save(registry, learners)
    return rows


def revoke(registry: Path, label: str) -> bool:
    """Revoke the code with this label; False when no such label."""
    with _locked(registry):
        learners = load(registry)
        hit = [pid for pid, v in learners.items() if v.get("label") == label]
        if not hit:
            return False
        for pid in hit:
            learners[pid]["revoked"] = True
        _save(registry, learners)
    return True


class DailyCount:
    """Questions per learner per UTC day, kept apart from the conversations so deleting
    history cannot reset it. Holds pseudonymous ids and counts only, for today only."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def __repr__(self) -> str:
        return "DailyCount()"

    def take(self, pid: str, day: dt.date, cap: int) -> bool:
        """Count one question for `pid` on `day` if it is under `cap`; False when it is not."""
        with _locked(self.path):
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                counts = data["counts"] if data.get("day") == day.isoformat() else {}
                if not isinstance(counts, dict):
                    counts = {}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                counts = {}
            used = counts.get(pid, 0) if isinstance(counts.get(pid, 0), int) else cap
            if used >= cap:
                return False
            counts[pid] = used + 1
            _write_json(self.path, {"day": day.isoformat(), "counts": counts})
            return True


class AccessCodeGate(PasskeyGate):
    """The passkey gate over personal codes: the session's mark is the learner's id."""

    PROMPT = ("Access code", "The personal code the teaching team sent you, like ABCD-EFGH-JKMN.")
    WRONG_TEXT = "Wrong access code."
    NEEDED_TEXT = "Enter your access code to continue."

    def __init__(self, registry: Path, load_secret: Callable[[], str | None],
                 **limits: Any) -> None:  # fmt: skip
        super().__init__(lambda: None, **limits)
        self.registry = Path(registry)
        self._secret = load_secret

    def __repr__(self) -> str:
        return f"AccessCodeGate(max_failures={self.max_failures}, window_s={self.window_s})"

    def _stored(self) -> Any:
        try:
            secret = self._secret()
            learners = load(self.registry)
        except Exception:
            raise GuardRefused(CLOSED) from None
        if not secret or len(secret.strip()) < MIN_SECRET:
            raise GuardRefused(CLOSED)  # a short secret could never match a code: closed
        return (secret, learners)

    def _accept(self, code: str, stored: Any) -> str | None:
        secret, learners = stored
        try:
            pid = learner_pid(normalize(code), secret)
        except PseudonymError:
            return None
        entry = learners.get(pid)
        return pid if entry is not None and not entry.get("revoked") else None

    def _valid(self, mark: str, stored: Any) -> bool:
        entry = stored[1].get(mark)
        return entry is not None and not entry.get("revoked")

    def identity(self, session: str) -> str | None:
        with self._lock:
            return self._unlocked.get(session) if isinstance(session, str) else None
