"""D3.2 entry point through Streamlit's AppTest: the startup lock, the two routed pages, the
learner page's replay default, and the off switch through both pages on one store. No
network: the pages get the B2.5 and B2.3 rigs through their installed factories."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from course_assistant.answer.ask import Assistant
from course_assistant.app import deploy, team_wiring, wiring
from course_assistant.demo import cache as demo_cache
from tests.answer.test_ask import QUESTION, happy
from tests.app.team_rig import STAFF_CODE, TeamRig
from tests.demo.test_demo import CODE, HASH
from tests.deploy.test_deploy import (
    KV_TOKEN_VALUE,
    NAMES,
    OWNER_KEY_VALUE,
    PID_VALUE,
    demo_fixture,
    good_secrets,
)

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "deploy" / "streamlit_app.py"
pytestmark = pytest.mark.app


@pytest.fixture(autouse=True)
def isolated() -> Iterator[None]:
    """AppTest installs the script as `__main__`, and the entry fills its non-secret defaults
    into the environment: put both back, and keep the real environment's secrets out."""
    main = sys.modules["__main__"]
    saved = dict(os.environ)
    for name in (*NAMES, "DEMO_TRUSTED_PROXY_HOPS", *deploy.DEFAULTS):
        os.environ.pop(name, None)
    yield
    os.environ.clear()
    os.environ.update(saved)
    sys.modules["__main__"] = main
    wiring.set_factory(None)
    team_wiring.set_factory(None)


@pytest.fixture
def rig(tmp_path: Path) -> TeamRig:
    r = TeamRig(tmp_path, owner={k: v * 3 for k, v in happy().items()})
    replay = tmp_path / "replay.json"  # the course the entry checks for (the demo cache)
    replay.write_text(demo_cache.dumps(demo_fixture()), encoding="utf-8")
    os.environ["CA_REPLAY"] = str(replay)
    wiring.set_factory(lambda: r.learners.chat)
    team_wiring.set_factory(lambda: r.team)
    return r


def entry(secrets: dict[str, str] | None = None) -> AppTest:
    at = AppTest.from_file(str(ENTRY), default_timeout=60)
    for k, v in (good_secrets() if secrets is None else secrets).items():
        at.secrets[k] = v
    at.run()
    return at


def labelled(widgets: Any, label: str) -> Any:
    found = [w for w in widgets if w.label == label]
    assert found, f"no widget labelled {label!r}: {[w.label for w in widgets]}"
    return found[0]


def everything(at: AppTest) -> str:
    parts: list[Any] = []
    for kind in ("markdown", "caption", "error", "info", "warning", "success", "text",
                 "title", "header", "subheader"):  # fmt: skip
        parts += [getattr(e, "value", "") for e in getattr(at, kind)]
    parts += [w.label for w in at.text_input] + [w.label for w in at.button]
    return "\n".join(str(p) for p in parts)


def learner_ready(at: AppTest) -> AppTest:
    labelled(at.text_input, "Passkey").input(CODE)
    labelled(at.button, "Unlock").click()
    at.run()
    labelled(at.button, "I agree and continue").click()
    at.run()
    return at


@pytest.mark.safety
@pytest.mark.parametrize("case", ["missing", "equal", "malformed", "no course"])
def test_entry_serves_only_a_locked_page_when_misconfigured(rig, case, caplog):
    secrets = good_secrets()
    if case == "no course":  # no demo cache committed and no ingested course
        os.environ["CA_REPLAY"] = str(Path(os.environ["CA_REPLAY"]).with_name("gone.json"))
    elif case == "missing":
        secrets.pop("DEMO_KV_REST_TOKEN")
    elif case == "equal":
        secrets["STAFF_PASSKEY_HASH"] = HASH
    else:
        secrets["DEMO_PASSKEY_HASH"] = "pbkdf2_sha256$1$x$y"
    with caplog.at_level(logging.WARNING):
        at = entry(secrets)
    assert not at.exception
    assert [e.value for e in at.error] == [deploy.LOCKED]
    assert len(at.text_input) == 0 and len(at.chat_input) == 0 and len(at.button) == 0
    shown = everything(at)
    for value in (OWNER_KEY_VALUE, KV_TOKEN_VALUE, PID_VALUE, HASH, *NAMES):
        assert value not in shown, value  # not even a secret's name on the page
    for value in (OWNER_KEY_VALUE, KV_TOKEN_VALUE, PID_VALUE, HASH):
        assert value not in caplog.text
    assert "demo startup refused" in caplog.text
    assert rig.learners.owner_client.calls == []


def test_entry_opens_on_the_learner_passkey_and_routes_the_team_page(rig):
    at = entry()
    assert not at.exception
    assert [w.label for w in at.text_input] == ["Passkey"]  # the learner page, locked
    assert len(at.chat_input) == 0
    labelled(at.text_input, "Passkey").input(CODE)
    labelled(at.button, "Unlock").click()
    at.run()
    at.switch_page(str(deploy.PAGES[1].path))
    at.run()
    assert not at.exception
    assert [w.label for w in at.text_input] == ["Teaching-team passkey"]
    labelled(at.text_input, "Teaching-team passkey").input(CODE)  # the learner code
    labelled(at.button, "Unlock").click()
    at.run()
    assert len(at.radio) == 0  # the learner passkey never opens the staff page
    labelled(at.text_input, "Teaching-team passkey").input(STAFF_CODE)
    labelled(at.button, "Unlock").click()
    at.run()
    assert labelled(at.radio, "Section").value == "Queue"


def test_learner_page_opens_on_worked_examples_with_no_model_call(rig):
    fx = demo_fixture()
    rig.learners.demo.cache = demo_cache.DemoCache(fx)
    at = learner_ready(entry())
    mode = labelled(at.radio, "Mode")
    assert mode.value == mode.options[0] == "Worked examples (no live call)"
    assert len(at.chat_input) == 0  # live questions are the second option
    pick = labelled(at.selectbox, "Example question")
    assert pick.options == [f"Example {i}" for i in range(1, len(fx.cases) + 1)]
    pick.select("Example 2")  # question text never reaches a Markdown label (CHT-05)
    at.run()
    case = fx.cases[1]
    assert case.expected is not None
    shown = [t.value for t in at.text]
    assert case.question in shown and case.expected.text in shown
    assert len([b for b in at.button if b.label == "Ask the teaching team"]) == 0
    assert rig.learners.owner_client.calls == []
    labelled(at.radio, "Mode").set_value("Ask your own question")
    at.run()
    assert len(at.chat_input) == 1


def test_the_pause_on_the_team_page_turns_live_answers_off_on_the_learner_page(rig):
    at = learner_ready(entry())
    at.switch_page(str(deploy.PAGES[1].path))
    at.run()
    labelled(at.text_input, "Teaching-team passkey").input(STAFF_CODE)
    labelled(at.button, "Unlock").click()
    at.run()
    labelled(at.radio, "Section").set_value("Pause switches")
    at.run()
    labelled(at.checkbox, "Yes, pause all sessions").check()
    at.run()
    labelled(at.button, "Pause all sessions").click()
    at.run()
    assert not at.exception
    at.switch_page(str(deploy.PAGES[0].path))
    at.run()
    calls = len(rig.learners.owner_client.calls)
    at.chat_input[0].set_value(QUESTION)
    at.run()
    assert Assistant.PAUSED_MESSAGE in [t.value for t in at.text]
    assert len(rig.learners.owner_client.calls) == calls  # off: no model call
