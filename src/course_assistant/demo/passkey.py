"""The passkey gate (D3.1, RPL-04; OWNER D-45 and D-48).

The passkey is a simple numeric passcode that only the owner knows. The repo never holds it:
the app's secret store holds a salted PBKDF2-HMAC-SHA256 hash (`DEMO_PASSKEY_HASH`), made
locally with `course-assistant passkey-hash`. Every page and route calls `require(session)`
before doing anything, and that holds for visitors who bring their own OpenAI key too.

- The hash is read at every check, so changing the secret revokes every unlocked session at
  once and removing it closes the gate (fail closed).
- Codes are compared in constant time (`hmac.compare_digest` on the derived digests).
- Failed attempts are counted per session and per IP; after `max_failures` within `window_s`
  the caller is refused before the code is checked, even when the code is right.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from course_assistant.demo._errors import GuardRefused, PasskeyConfigError

SECRET_NAME = "DEMO_PASSKEY_HASH"
SCHEME = "pbkdf2_sha256"
ITERATIONS = 600_000
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 2_000_000  # more would make each unlock attempt a CPU denial of service
SALT_BYTES = 16
_CODE = re.compile(r"^[0-9]{6,12}$")
_HASH = re.compile(
    r"^pbkdf2_sha256\$([0-9]{6,8})\$([A-Za-z0-9+/=]{16,64})\$([A-Za-z0-9+/=]{40,64})$"
)
MAX_KEYS = 10_000  # failure memory bound: the oldest idle keys are forgotten first

CLOSED = "The demo is closed right now."
NEEDED = "Enter the passkey to continue."
WRONG = "Wrong passkey."
LOCKED = "Too many wrong passkeys. Try again in 15 minutes."


def valid_code(code: object) -> bool:
    return isinstance(code, str) and _CODE.fullmatch(code) is not None


def _derive(code: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, iterations)


def hash_passcode(code: str, *, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    """`pbkdf2_sha256$<iterations>$<salt b64>$<digest b64>` for a 6-12 digit passcode."""
    if not valid_code(code):
        raise ValueError("a passcode is 6 to 12 digits")
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise ValueError(f"iterations must be {MIN_ITERATIONS} to {MAX_ITERATIONS}")
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    if len(salt) < SALT_BYTES:
        raise ValueError(f"salt must be at least {SALT_BYTES} bytes")
    b64 = base64.b64encode
    digest = _derive(code, salt, iterations)
    return f"{SCHEME}${iterations}${b64(salt).decode()}${b64(digest).decode()}"


def _parse(stored: str) -> tuple[int, bytes, bytes]:
    m = _HASH.fullmatch(stored.strip()) if isinstance(stored, str) else None
    if m is None:
        raise PasskeyConfigError(f"{SECRET_NAME} is not a passkey hash")
    try:
        salt = base64.b64decode(m[2], validate=True)
        digest = base64.b64decode(m[3], validate=True)
    except (binascii.Error, ValueError):
        raise PasskeyConfigError(f"{SECRET_NAME} is not a passkey hash") from None
    iterations = int(m[1])
    if (
        not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS
        or len(salt) < SALT_BYTES
        or len(digest) != 32
    ):
        raise PasskeyConfigError(f"{SECRET_NAME} is not a passkey hash")
    # one text per hash (D3.2 verifier): no leading zeros, canonical base64 only, so two
    # spellings of one hash can never pass a text comparison as different hashes
    b64 = base64.b64encode
    if m[1] != str(iterations) or b64(salt).decode() != m[2] or b64(digest).decode() != m[3]:
        raise PasskeyConfigError(f"{SECRET_NAME} is not a passkey hash")
    return iterations, salt, digest


def same_hash(a: str, b: str) -> bool:
    """True when two stored hashes are the same hash by value (iterations, salt, digest);
    a malformed one is never the same as anything."""
    try:
        return _parse(a) == _parse(b)
    except PasskeyConfigError:
        return False


def verify_passcode(code: str, stored: str) -> bool:
    """True only when `code` derives to the stored digest. A malformed hash raises
    `PasskeyConfigError` (never a pass); a malformed code is derived anyway and fails."""
    iterations, salt, digest = _parse(stored)
    candidate = code if isinstance(code, str) else ""
    ok = hmac.compare_digest(_derive(candidate, salt, iterations), digest)
    return ok and valid_code(code)


def fingerprint(stored: str) -> str:
    """Which hash a session was unlocked against (never the hash itself)."""
    return hashlib.sha256(stored.strip().encode("utf-8")).hexdigest()[:16]


def read_secret(name: str, *, env: Mapping[str, str] | None = None,
                secrets_store: Mapping[str, object] | None = None) -> str | None:  # fmt: skip
    """A secret from the environment first, then a Streamlit-style secrets mapping; None when
    unset or blank. Never from a file in the repo."""
    env = os.environ if env is None else env
    value = env.get(name)
    if (value is None or not value.strip()) and secrets_store is not None:
        try:
            raw = secrets_store.get(name)
        except Exception:
            raw = None
        value = raw if isinstance(raw, str) else None
    return value.strip() if value and value.strip() else None


def env_hash(
    env: Mapping[str, str] | None = None, secrets_store: Mapping[str, object] | None = None
) -> Callable[[], str | None]:
    """A `load_hash` for the gate that reads `DEMO_PASSKEY_HASH` at every check."""
    return lambda: read_secret(SECRET_NAME, env=env, secrets_store=secrets_store)


class PasskeyGate:
    PROMPT = ("Passkey", "The numeric passkey the course team gave you.")  # the page's field

    def __init__(self, load_hash: Callable[[], str | None], *, max_failures: int = 5,
                 window_s: float = 900.0, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = MAX_KEYS, global_max_failures: int = 50) -> None:  # fmt: skip
        if max_failures < 1 or window_s <= 0 or max_keys < 2 or global_max_failures < 1:
            raise ValueError("max_failures, window_s and max_keys must be positive")
        self._load = load_hash
        self.max_failures = max_failures
        self.global_max_failures = global_max_failures
        self.window_s = window_s
        self._clock = clock
        self._max_keys = max_keys
        self._failures: dict[tuple[str, str], deque[float]] = {}
        self._unlocked: dict[str, str] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"PasskeyGate(max_failures={self.max_failures}, window_s={self.window_s})"

    def _stored(self) -> str:
        try:
            stored = self._load()
        except Exception:
            stored = None
        if not stored:
            raise GuardRefused(CLOSED)
        try:
            _parse(stored)
        except PasskeyConfigError:
            raise GuardRefused(CLOSED) from None
        return stored

    @staticmethod
    def _keys(session: str, ip: str | None) -> list[tuple[str, str]]:
        keys = [("session", session)]
        if ip:
            keys.append(("ip", ip))
        return keys

    def _recent(self, key: tuple[str, str], now: float) -> deque[float]:
        hits = self._failures.get(key)
        if hits is None:
            return deque()
        while hits and now - hits[0] > self.window_s:
            hits.popleft()
        if not hits:
            del self._failures[key]
            return deque()
        return hits

    def locked(self, session: str, ip: str | None = None) -> bool:
        with self._lock:
            now = self._clock()
            keys = self._all_keys(session, ip)
            return any(len(self._recent(k, now)) >= self._limit(k) for k in keys)

    def unlock(self, code: str, *, session: str, ip: str | None = None) -> None:
        """Unlock `session` with the passcode, or raise `GuardRefused`. Each attempt is counted
        as a failure before the slow check runs, under the lock, so simultaneous guesses cannot
        all slip in before the lockout (verifier D3.1 pass 1); a right code then takes its own
        count back. A global count also caps guesses from rotating sessions with no trusted IP."""
        if not isinstance(session, str) or not session:
            raise GuardRefused(NEEDED)
        stored = self._stored()
        keys = self._all_keys(session, ip)
        with self._lock:
            now = self._clock()
            if any(len(self._recent(k, now)) >= self._limit(k) for k in keys):
                raise GuardRefused(LOCKED)
            for key in keys:
                self._recent(key, now)
                self._failures.setdefault(key, deque()).append(now)
            self._bound(self._failures)
        mark = self._accept(code, stored)
        if mark is not None:
            with self._lock:
                for key in keys:
                    hits = self._failures.get(key)
                    if hits:
                        with contextlib.suppress(ValueError):
                            hits.remove(now)
                self._failures.pop(("session", session), None)
                self._unlocked[session] = mark
                self._bound(self._unlocked)
            return
        raise GuardRefused(WRONG)

    def _accept(self, code: str, stored: Any) -> str | None:
        """The session's mark when `code` is right, else None (the slow check)."""
        return fingerprint(stored) if verify_passcode(code, stored) else None

    def _valid(self, mark: str, stored: Any) -> bool:
        """Whether an unlocked session's mark still holds against what is set now."""
        return mark == fingerprint(stored)

    def identity(self, session: str) -> str | None:
        """The learner id an unlock gave this session; a shared passkey gives none."""
        return None

    def _all_keys(self, session: str, ip: str | None) -> list[tuple[str, str]]:
        """Per session and per IP; the global cap counts only attempts with no IP, so a flood of
        wrong guesses can never lock out the owner or visitors with a known IP (pass 2)."""
        keys = self._keys(session, ip)
        return keys if ip else [*keys, ("global", "*")]

    def _limit(self, key: tuple[str, str]) -> int:
        return self.global_max_failures if key[0] == "global" else self.max_failures

    def require(self, session: str) -> None:
        """Every page and route calls this first: refuse unless `session` was unlocked against
        the hash that is set now."""
        stored = self._stored()
        with self._lock:
            mark = self._unlocked.get(session) if isinstance(session, str) else None
            ok = mark is not None and self._valid(mark, stored)
        if not ok:
            raise GuardRefused(NEEDED)

    def lock_session(self, session: str) -> None:
        """Forget an unlocked session (sign out)."""
        with self._lock:
            self._unlocked.pop(session, None)

    def _bound(self, table: dict) -> None:  # type: ignore[type-arg]
        while len(table) > self._max_keys:
            del table[next(iter(table))]  # insertion order: the oldest first
