"""What the hosted demo needs around the two pages (D3.2). No Streamlit import: the entry
point (`deploy/streamlit_app.py`) and the pages call these, and tests call them directly.

- `startup_problem`: the app serves nothing (a locked page with `LOCKED`) when a required
  secret is missing, a passkey hash is malformed, or the learner and staff hashes are equal.
- `LiveSecrets`: the host secret store read at every lookup, so a changed hash takes effect
  at the next check with no restart.
- `client_ip` and `trusted_hops`: the client address for the passkey rate limit, trusted only
  as far as the configured proxy hops (none by default).
- `DailyStores`: one demo store per UTC day; earlier days are removed (nightly reset).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import re
import shutil
import threading
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from course_assistant.demo._errors import PasskeyConfigError
from course_assistant.demo.passkey import _parse, read_secret
from course_assistant.store.db import Store

LOG = logging.getLogger("course_assistant.demo")
ROOT = Path(__file__).resolve().parents[3]
APP_DIR = Path(__file__).resolve().parent

REQUIRED = ("OPENAI_API_KEY", "DEMO_PASSKEY_HASH", "STAFF_PASSKEY_HASH", "DEMO_KV_REST_URL",
            "DEMO_KV_REST_TOKEN", "PID_SECRET")  # fmt: skip
HASHES = ("DEMO_PASSKEY_HASH", "STAFF_PASSKEY_HASH")
HOPS = "DEMO_TRUSTED_PROXY_HOPS"
MAX_HOPS = 5
LOCKED = "This demo is not available right now. Please try again later."

# the non-secret settings the hosted demo runs with (paths relative to the repo root); the
# entry point fills them in only when unset
DEFAULTS = {
    "CA_REPLAY": "demo/replay.json",
    "CA_MANIFEST": "sample_course/manifest.yaml",
    "CA_APP_DIR": "runs/app",
    "CA_DAILY_RESET": "1",
}


@dataclass(frozen=True)
class Page:
    title: str
    path: Path
    url_path: str
    default: bool = False


PAGES = (
    Page("Course assistant", APP_DIR / "learner_page.py", "assistant", default=True),
    Page("Teaching team", APP_DIR / "team_page.py", "team"),
)


def apply_defaults(env: MutableMapping[str, str], root: Path = ROOT) -> None:
    """Fill the non-secret demo settings that are unset (paths under `root`)."""
    for name, value in DEFAULTS.items():
        if not (env.get(name) or "").strip():
            env[name] = value if name == "CA_DAILY_RESET" else str(root / value)


# ---------- startup check (DEM-02) ----------


def startup_problem(env: Mapping[str, str] | None = None,
                    secrets_store: Mapping[str, object] | None = None) -> str | None:  # fmt: skip
    """None when the demo may serve; else what is wrong, by secret name only (never a value)."""
    values = {n: read_secret(n, env=env, secrets_store=secrets_store) for n in REQUIRED}
    missing = [n for n, v in values.items() if v is None]
    if missing:
        return "missing " + ", ".join(missing)
    bad = []
    for name in HASHES:
        try:
            _parse(values[name] or "")
        except PasskeyConfigError:
            bad.append(name)
    if bad:
        return "not a passkey hash: " + ", ".join(bad)
    if not (values["DEMO_KV_REST_URL"] or "").startswith("https://"):
        return "DEMO_KV_REST_URL is not an https URL"
    if values["DEMO_PASSKEY_HASH"] == values["STAFF_PASSKEY_HASH"]:
        return "DEMO_PASSKEY_HASH and STAFF_PASSKEY_HASH are equal"
    return None


def course_problem(env: Mapping[str, str]) -> str | None:
    """None when the pages have a course to serve: an ingested course (`CA_CONFIG`) or the
    demo cache (`CA_REPLAY`, which carries the sample course); else what is missing."""
    if (env.get("CA_CONFIG") or "").strip():
        return None
    replay = (env.get("CA_REPLAY") or "").strip()
    if replay and Path(replay).is_file():
        return None
    return "no course: commit the demo cache (CA_REPLAY) or set CA_CONFIG"


def log_startup_problem(problem: str | None) -> None:
    if problem:
        LOG.warning("demo startup refused: %s", problem)


# ---------- the host secret store, read live (B2.3 deferred item) ----------


class LiveSecrets(Mapping[str, object]):
    """A read-only view of the host secret store that calls `load()` at every lookup (the
    pages pass `lambda: st.secrets`), so nothing is snapshotted at startup."""

    def __init__(self, load: Callable[[], Mapping[str, object] | None]) -> None:
        self._load = load

    def _now(self) -> Mapping[str, object]:
        try:
            current = self._load()
        except Exception:
            return {}
        return current if current is not None else {}

    def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
        try:
            return self._now().get(key, default)
        except Exception:
            return default

    def __getitem__(self, key: str) -> object:
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[str]:
        try:
            return iter(list(self._now()))
        except Exception:
            return iter(())

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __repr__(self) -> str:
        return "LiveSecrets()"


_MISSING = object()


# ---------- trusted client IP (D3.1 deferred item) ----------


def trusted_hops(env: Mapping[str, str] | None = None,
                 secrets_store: Mapping[str, object] | None = None) -> int:  # fmt: skip
    """`DEMO_TRUSTED_PROXY_HOPS`, read at every call: 0 to 5; anything else is 0."""
    raw = read_secret(HOPS, env=env, secrets_store=secrets_store)
    if raw is None or not re.fullmatch(r"[0-9]", raw):
        return 0
    n = int(raw)
    return n if n <= MAX_HOPS else 0


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is None:
            value = next((v for k, v in headers.items() if k.lower() == name.lower()), None)
    except Exception:
        return None
    return value if isinstance(value, str) else None


def client_ip(headers: Mapping[str, str] | None, hops: int) -> str | None:
    """The address the trusted proxy saw: the `hops`-th entry from the right of
    `X-Forwarded-For`, only when it parses as an IP. With no trusted hop (0), None: the left
    side of the header is whatever the client wrote, and the socket address is the proxy's,
    shared by every visitor, so neither may key the rate limit."""
    if not isinstance(hops, int) or hops < 1 or hops > MAX_HOPS:
        return None
    raw = _header(headers, "X-Forwarded-For")
    if not raw:
        return None
    chain = [p.strip() for p in raw.split(",")]
    if len(chain) < hops:
        return None
    candidate = chain[-hops]
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        return None


# ---------- nightly demo data reset (DEM-03) ----------

_DAY_DIR = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


class DailyStores:
    """The demo store of today (UTC): `root/<YYYY-MM-DD>/app.sqlite`, one `Store` per thread
    (Streamlit runs each rerun on a new thread). When the day changes, earlier day folders
    are removed, so demo conversations, escalations, consents and audit rows live one day."""

    def __init__(self, root: Path, today: Callable[[], dt.date] = _utc_today) -> None:
        self.root = Path(root)
        self._today = today
        self._lock = threading.Lock()
        self._day: dt.date | None = None
        self._local = threading.local()

    def __repr__(self) -> str:
        return "DailyStores()"

    def __call__(self) -> Store:
        day = self._today()
        with self._lock:
            if day != self._day:
                self._roll(day)
        cached = getattr(self._local, "store", None)
        if cached is not None and cached[0] == day:
            return cached[1]
        store = Store(self.root / day.isoformat() / "app.sqlite")
        self._local.store = (day, store)
        return store

    def _roll(self, day: dt.date) -> None:
        folder = self.root / day.isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        removed = 0
        for old in self.root.iterdir():
            if old.is_dir() and _DAY_DIR.match(old.name) and old.name != day.isoformat():
                shutil.rmtree(old, ignore_errors=True)
                removed += 1
        self._day = day
        if removed:
            LOG.info("demo store reset: removed=%d", removed)
