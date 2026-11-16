"""The persistent daily spend counter (D3.1, RPL-02; D-16, EvalKit L-39).

Money is integer micro-dollars, so the counter has no float drift at the cap. The counter sits
behind `SpendCounter`: `FileSpendCounter` (local development and the tests' fake) and
`KVSpendCounter` on the hosted KV store (Redis over REST: `DEMO_KV_REST_URL`,
`DEMO_KV_REST_TOKEN`), whose transport is injected so no test makes a call.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import threading
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar, Protocol

MICRO = 1_000_000
KV_URL = "DEMO_KV_REST_URL"
KV_TOKEN = "DEMO_KV_REST_TOKEN"
KEY_TTL_S = 3 * 24 * 3600


def to_micro(usd: float) -> int:
    """Dollars as micro-dollars, rounded up (a hold is never under the amount)."""
    if isinstance(usd, bool) or not isinstance(usd, int | float):
        raise ValueError("amount is not a number")
    if not math.isfinite(usd) or usd < 0:
        raise ValueError("amount must be finite and non-negative")
    return math.ceil(round(usd * MICRO, 6))


def _check(*amounts: int) -> None:
    for a in amounts:
        if isinstance(a, bool) or not isinstance(a, int) or a < 0:
            raise ValueError("micro-dollar amounts are non-negative integers")


class SpendCounter(Protocol):
    def reserve(self, day: str, amount: int, cap: int) -> bool: ...
    def settle(self, day: str, reserved: int, actual: int) -> None: ...
    def spent(self, day: str) -> int: ...


class FileSpendCounter:
    """Daily spend in a JSON file: `{"days": {"2026-11-20": micro}}`. Each read-modify-write
    holds a process lock and, where the OS has one, an exclusive file lock, and the file is
    replaced atomically, so it survives restarts and concurrent requests."""

    _locks: ClassVar[dict[str, threading.Lock]] = {}
    _locks_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lockfile = self.path.with_suffix(self.path.suffix + ".lock")
        key = str(self.path.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.Lock())

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, int]]:
        with self._lock, open(self._lockfile, "a+", encoding="utf-8") as fh:
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except ImportError:  # no flock on this OS: the process lock still holds
                pass
            days = self._read()
            before = dict(days)
            yield days
            if days != before:
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps({"days": days}, sort_keys=True), encoding="utf-8")
                os.replace(tmp, self.path)

    def _read(self) -> dict[str, int]:
        if not self.path.is_file():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        days = data.get("days", {})
        if not isinstance(days, dict) or not all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in days.values()
        ):
            raise ValueError("spend file is corrupt")
        return {str(k): int(v) for k, v in days.items()}

    def reserve(self, day: str, amount: int, cap: int) -> bool:
        _check(amount, cap)
        with self._locked() as days:
            if days.get(day, 0) + amount > cap:
                return False
            days[day] = days.get(day, 0) + amount
            return True

    def settle(self, day: str, reserved: int, actual: int) -> None:
        _check(reserved, actual)
        with self._locked() as days:
            days[day] = max(0, days.get(day, 0) + actual - reserved)

    def spent(self, day: str) -> int:
        with self._locked() as days:
            return days.get(day, 0)


class KVClient(Protocol):
    def incrby(self, key: str, amount: int) -> int: ...
    def get(self, key: str) -> int | None: ...
    def expire(self, key: str, seconds: int) -> None: ...


class KVSpendCounter:
    """Daily spend in the hosted KV store. A reservation is one atomic INCRBY; a total past
    the cap is given back with a second INCRBY and refused, so concurrent requests can never
    hold past the cap together (at worst one is refused early)."""

    def __init__(self, kv: KVClient, prefix: str = "ca-demo-spend") -> None:
        self.kv = kv
        self.prefix = prefix

    def _key(self, day: str) -> str:
        return f"{self.prefix}:{day}"

    def reserve(self, day: str, amount: int, cap: int) -> bool:
        _check(amount, cap)
        key = self._key(day)
        total = self.kv.incrby(key, amount)
        with contextlib.suppress(Exception):  # an expiry is housekeeping, never the guard
            self.kv.expire(key, KEY_TTL_S)
        if total > cap:
            self.kv.incrby(key, -amount)
            return False
        return True

    def settle(self, day: str, reserved: int, actual: int) -> None:
        _check(reserved, actual)
        delta = actual - reserved
        if delta:
            total = self.kv.incrby(self._key(day), delta)
            if total < 0:  # never below zero, whatever another settle did meanwhile
                self.kv.incrby(self._key(day), -total)

    def spent(self, day: str) -> int:
        return max(0, self.kv.get(self._key(day)) or 0)


class KVError(Exception):
    """The KV store could not be reached or answered badly (never its text or the token)."""


Post = Callable[[str, dict[str, str], bytes, float], Any]


def _urllib_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Any:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class RestKV:
    """Redis commands over REST (`POST url` with a JSON command array and a bearer token,
    answer `{"result": ...}`). The transport is injected; the token never leaves the header."""

    def __init__(self, url: str, token: str, *, post: Post | None = None,
                 timeout: float = 5.0) -> None:  # fmt: skip
        if not url.startswith("https://") or not token:
            raise ValueError("the KV store needs an https URL and a token")
        self._url = url.rstrip("/")
        self._token = token
        self._post = post or _urllib_post
        self._timeout = timeout

    def __repr__(self) -> str:
        return "RestKV(url=<set>, token=<hidden>)"

    def _cmd(self, *args: object) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        try:
            data = self._post(self._url, headers, json.dumps(list(args)).encode(), self._timeout)
        except Exception as exc:
            raise KVError(f"KV request failed ({type(exc).__name__})") from None
        if not isinstance(data, dict) or "error" in data or "result" not in data:
            raise KVError("KV store answered with an error")
        return data["result"]

    def incrby(self, key: str, amount: int) -> int:
        result = self._cmd("INCRBY", key, int(amount))
        if isinstance(result, bool) or not isinstance(result, int):
            raise KVError("KV store answered with a non-integer")
        return result

    def get(self, key: str) -> int | None:
        result = self._cmd("GET", key)
        if result is None:
            return None
        try:
            return int(result)
        except (TypeError, ValueError):
            raise KVError("KV store answered with a non-integer") from None

    def expire(self, key: str, seconds: int) -> None:
        self._cmd("EXPIRE", key, int(seconds))
