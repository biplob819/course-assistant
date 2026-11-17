"""P4.1 groundwork through Streamlit's AppTest on the real D3.2 entry point and the real
pages, wired from the environment (no installed factory): a pilot course is served only from
the gate's live pointer, the startup lock covers a missing or unreadable pointer at every run,
and the staff view shows the served version. No network; no model call is made."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from course_assistant.app import deploy, team_wiring, wiring
from course_assistant.app.chat import session_labels
from tests.answer.test_ask import CORPUS
from tests.app.team_rig import STAFF_CODE
from tests.deploy.test_deploy import NAMES, good_secrets
from tests.deploy.test_entry import everything, labelled, learner_ready
from tests.serving.helpers import Course, with_addition, without_session

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "deploy" / "streamlit_app.py"
pytestmark = pytest.mark.app


@pytest.fixture(autouse=True)
def isolated() -> Iterator[None]:
    main = sys.modules["__main__"]
    saved = dict(os.environ)
    for name in (*NAMES, "DEMO_TRUSTED_PROXY_HOPS", "CA_CONFIG", *deploy.DEFAULTS):
        os.environ.pop(name, None)
    wiring.set_factory(None)
    team_wiring.set_factory(None)
    st.cache_resource.clear()  # each test's pages are built from its own environment
    yield
    st.cache_resource.clear()
    os.environ.clear()
    os.environ.update(saved)
    sys.modules["__main__"] = main


@pytest.fixture
def course(tmp_path: Path) -> Course:
    c = Course(tmp_path, live=False)
    os.environ.update(c.env(CA_REPLAY=str(tmp_path / "no-demo-cache.json")))
    return c


def entry(secrets: dict[str, str] | None = None) -> AppTest:
    at = AppTest.from_file(str(ENTRY), default_timeout=60)
    for k, v in (good_secrets() if secrets is None else secrets).items():
        at.secrets[k] = v
    at.run()
    return at


def locked(at: AppTest) -> bool:
    return ([e.value for e in at.error] == [deploy.LOCKED] and len(at.text_input) == 0
            and len(at.chat_input) == 0 and len(at.button) == 0)  # fmt: skip


@pytest.mark.safety
def test_entry_locks_a_pilot_course_with_no_or_an_unreadable_pointer(course, caplog):
    with caplog.at_level(logging.WARNING):
        at = entry()
    assert not at.exception and locked(at)
    assert "LIVE.json" in caplog.text and str(course.config.parent) not in caplog.text
    assert str(course.config.parent) not in everything(at)
    course.mark_live(CORPUS.corpus_version)
    at.run()  # the pointer appears: the next run serves the passkey page
    assert not at.exception and [w.label for w in at.text_input] == ["Passkey"]
    course.live_file.write_text("{", encoding="utf-8")
    at.run()  # damaged after startup: locked again at the next run
    assert locked(at)


@pytest.mark.safety
def test_a_live_pointer_never_weakens_the_startup_lock_or_the_passkeys(course):
    course.mark_live(CORPUS.corpus_version)
    secrets = good_secrets()
    secrets.pop("PID_SECRET")
    assert locked(entry(secrets))  # D3.2's lock still refuses a missing secret
    at = entry()
    assert [w.label for w in at.text_input] == ["Passkey"] and len(at.chat_input) == 0
    at.switch_page(str(deploy.PAGES[1].path))
    at.run()
    assert [w.label for w in at.text_input] == ["Teaching-team passkey"]
    assert len(at.radio) == 0


def test_the_learner_page_offers_only_the_served_sessions(course):
    course.mark_live(CORPUS.corpus_version)
    course.ingest(with_addition())  # a newer ingest, not gated
    at = learner_ready(entry())
    assert not at.exception
    assert labelled(at.selectbox, "Search in").options == session_labels(CORPUS)
    removal = without_session("s02")
    course.ingest(removal)  # a removal: served at the next run, before any flip
    at.run()
    assert labelled(at.selectbox, "Search in").options == session_labels(removal)
    course.mark_live(removal.corpus_version)  # the removal-only flip
    at.run()
    assert labelled(at.selectbox, "Search in").options == session_labels(removal)


def test_the_staff_view_shows_the_served_corpus_version(course):
    course.mark_live(CORPUS.corpus_version)
    candidate = with_addition()
    course.ingest(candidate)
    at = entry()
    at.switch_page(str(deploy.PAGES[1].path))
    at.run()
    labelled(at.text_input, "Teaching-team passkey").input(STAFF_CODE)
    labelled(at.button, "Unlock").click()
    at.run()
    labelled(at.radio, "Section").set_value("Materials")
    at.run()
    shown = everything(at)
    assert f"Live corpus version: {CORPUS.corpus_version}" in shown
    assert candidate.corpus_version not in shown
    removal = without_session("s03")
    course.ingest(removal)
    course.mark_live(removal.corpus_version)
    at.run()
    assert f"Live corpus version: {removal.corpus_version}" in everything(at)
