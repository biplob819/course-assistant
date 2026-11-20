#!/usr/bin/env python3
"""Post-deploy smoke check for the hosted demo (D3.2, DEM-04).

Opens the app's Streamlit stream with no passcode, runs the default page once, and checks that
the page is locked: a "Passkey" password input is shown, and nothing behind it is (no chat
input, no worked examples, no teaching-team section, no answer card), and the app is not on
its startup lock (which means a secret is missing or wrong).

    uv run --extra app python scripts/smoke_demo.py https://<app-name>.streamlit.app

Exit 0 pass, 1 fail, 2 cannot connect or a bad URL. It sends no passcode and no key, and
prints only element kinds and fixed labels, never page text.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASSKEY_LABEL = "Passkey"
# what a locked learner page must never show (fixed labels of what lies behind the passkey)
BEHIND = ("Teaching team", "Before you start", "Your history", "Search in", "Mode",
          "Example question", "Section", "Your own OpenAI key")  # fmt: skip
EXIT_OK, EXIT_FAIL, EXIT_CONNECT = 0, 1, 2


class Stream(Protocol):
    def send(self, data: bytes) -> None: ...
    def recv(self, timeout: float) -> bytes | None: ...
    def close(self) -> None: ...


def stream_url(url: str) -> str:
    """The app's websocket stream. Community Cloud serves the app itself under `/~/+/`."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("the URL must start with http:// or https://")
    scheme = "wss" if parts.scheme == "https" else "ws"
    base = parts.path.rstrip("/")
    host = parts.hostname or ""
    if host.endswith(".streamlit.app") and "/~/+" not in base:
        base += "/~/+"
    return urlunsplit((scheme, parts.netloc, base + "/_stcore/stream", "", ""))


def page_cookies(page_url: str, timeout: float, opener: Any = None) -> str:
    """The cookies the app's host sets on a plain page load, as one Cookie header. Community
    Cloud answers a cookieless request with a redirect through share.streamlit.io that sets a
    cookie and comes back; a websocket opened without it gets an HTTP status, not a stream."""
    import http.cookiejar
    import urllib.request

    jar = http.cookiejar.CookieJar()
    fetch = opener or urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    get = fetch.open  # OpenerDirector.open fetches a URL; it reads bytes, not a text file
    with get(page_url, timeout=timeout) as resp:
        resp.read(1)
    return "; ".join(f"{c.name}={c.value}" for c in jar)


class _WebSocket:
    """The real transport (websockets, which Streamlit itself depends on)."""

    def __init__(self, url: str, timeout: float, cookie: str = "", origin: str = "") -> None:
        from websockets.sync.client import connect

        # Community Cloud refuses the stream (401) without the page's cookies and its Origin
        headers = {k: v for k, v in (("Cookie", cookie), ("Origin", origin)) if v} or None
        self._cm = connect(url, subprotocols=["streamlit"],  # type: ignore[list-item]
                           open_timeout=timeout, max_size=None,
                           additional_headers=headers)  # fmt: skip
        self._ws = self._cm.__enter__()

    def send(self, data: bytes) -> None:
        self._ws.send(data)

    def recv(self, timeout: float) -> bytes | None:
        try:
            msg = self._ws.recv(timeout=timeout)
        except TimeoutError:
            return None
        return msg if isinstance(msg, bytes) else None

    def close(self) -> None:
        self._cm.__exit__(None, None, None)


def _rerun_message() -> bytes:
    from streamlit.proto.BackMsg_pb2 import BackMsg

    msg = BackMsg()
    msg.rerun_script.query_string = ""
    msg.rerun_script.page_script_hash = ""
    msg.rerun_script.widget_states.SetInParent()  # no widget values: no passcode, no key
    return msg.SerializeToString()


def _password_type() -> int:
    from streamlit.proto.TextInput_pb2 import TextInput

    return TextInput.PASSWORD


def collect(stream: Stream, timeout: float, max_messages: int = 500) -> list[tuple[str, str]]:
    """(element kind, label or body) for each element of one run of the default page."""
    from streamlit.proto.ForwardMsg_pb2 import ForwardMsg

    stream.send(_rerun_message())
    found: list[tuple[str, str]] = []
    for _ in range(max_messages):
        raw = stream.recv(timeout)
        if raw is None:
            break
        msg = ForwardMsg.FromString(raw)
        kind = msg.WhichOneof("type")
        if kind == "script_finished":
            break
        if kind != "delta" or msg.delta.WhichOneof("type") != "new_element":
            continue
        el = msg.delta.new_element
        etype = el.WhichOneof("type") or ""
        inner: Any = getattr(el, etype, None)
        text = ""
        for field in ("label", "body", "placeholder"):
            if inner is not None and hasattr(inner, field):
                text = str(getattr(inner, field))
                break
        if etype == "text_input" and inner is not None and inner.type == _password_type():
            etype = "password_input"
        found.append((etype, text))
    return found


def verdict(elements: list[tuple[str, str]], locked_text: str) -> list[str]:
    """Reasons the page is not a correctly locked demo; empty means pass."""
    reasons: list[str] = []
    if any(text == locked_text for _, text in elements):
        reasons.append("the startup lock is shown: a secret or the course is missing or wrong")
    if not any(k == "password_input" and t == PASSKEY_LABEL for k, t in elements):
        reasons.append("no Passkey input on the default page")
    for kind, text in elements:
        if kind in ("chat_input", "exception"):
            reasons.append(f"a {kind} is shown without the passkey")
        if any(text == b or text.startswith(b + " ") for b in BEHIND):
            reasons.append(f"'{text}' is shown without the passkey")
    return reasons


def main(argv: list[str] | None = None,
         connect: Callable[[str, float], Stream] | None = None) -> int:  # fmt: skip
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("url", metavar="URL", help="the deployed app, e.g. https://x.streamlit.app")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per wait")
    args = parser.parse_args(argv)
    from course_assistant.app.deploy import LOCKED

    try:
        ws_url = stream_url(args.url)
    except ValueError as exc:
        print(f"smoke: {exc}")
        return EXIT_CONNECT
    page = urlsplit(args.url.strip())
    origin = f"{page.scheme}://{page.netloc}"
    opener = connect or (lambda u, t: _WebSocket(u, t, page_cookies(args.url, t), origin))
    try:
        stream = opener(ws_url, args.timeout)
    except Exception as exc:
        print(f"smoke: cannot connect ({type(exc).__name__}); the app may be asleep: open it in "
              "a browser once, then run this again")  # fmt: skip
        return EXIT_CONNECT
    try:
        elements = collect(stream, args.timeout)
    except Exception as exc:
        print(f"smoke: the stream failed ({type(exc).__name__})")
        return EXIT_CONNECT
    finally:
        with contextlib.suppress(Exception):
            stream.close()
    reasons = verdict(elements, LOCKED)
    print(f"smoke: {len(elements)} elements on the default page")
    for r in reasons:
        print(f"smoke: FAIL {r}")
    if reasons:
        return EXIT_FAIL
    print("smoke: PASS the page is locked behind the passkey")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
