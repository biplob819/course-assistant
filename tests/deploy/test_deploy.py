"""D3.2 demo deploy: DEM-01 to DEM-04, offline.

The startup check, the live secrets view, the trusted client IP, the daily store reset, the
learner replay mode, the provider's `max_tool_calls`, the files Community Cloud reads, the
owner guide and the smoke check (over fake Streamlit stream messages). No network: the
learner and team rigs run D3.1's real gate and guard over scripted outputs through N1.2's
real `respond`; the demo cache is built from the golden fixture's public recordings.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import re
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from course_assistant.answer.ask import Assistant
from course_assistant.app import deploy, team_wiring, wiring
from course_assistant.app.chat import ChatState, card_for
from course_assistant.app.team import TeamState
from course_assistant.demo import cache as demo_cache
from course_assistant.demo.guard import FILE_SEARCH_CALLS_BOUND
from course_assistant.demo.passkey import CLOSED, NEEDED
from course_assistant.llm import InputItem, ResponsesRequest
from course_assistant.llm.client import MAX_TOOL_CALLS, build_kwargs
from course_assistant.store.db import Store
from tests.answer import scripted as s
from tests.answer.test_ask import QUESTION
from tests.app.rig import OWN_KEY, ChatRig
from tests.app.team_rig import STAFF_CODE, STAFF_HASH, TeamRig
from tests.demo.test_demo import CODE, HASH, IP, build_from_golden

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "specs" / "D3.2-demo-deploy" / "spec.md"
GUIDE = ROOT / "docs" / "deploy.md"
EXAMPLE = ROOT / ".streamlit" / "secrets.toml.example"
CONFIG_TOML = ROOT / ".streamlit" / "config.toml"
REQUIREMENTS = ROOT / "deploy" / "requirements.txt"
SMOKE = ROOT / "scripts" / "smoke_demo.py"
NAMES = ("OPENAI_API_KEY", "DEMO_PASSKEY_HASH", "STAFF_PASSKEY_HASH", "DEMO_KV_REST_URL",
         "DEMO_KV_REST_TOKEN", "PID_SECRET")  # fmt: skip
OWNER_KEY_VALUE = "sk-proj-OWNERKEY-d32-5b1c9e"
KV_TOKEN_VALUE = "kvtoken-MARKER-d32-41aa"
PID_VALUE = "pidsecret-MARKER-d32-77c0"


def good_secrets(**over: str) -> dict[str, str]:
    values = {"OPENAI_API_KEY": OWNER_KEY_VALUE, "DEMO_PASSKEY_HASH": HASH,
              "STAFF_PASSKEY_HASH": STAFF_HASH, "DEMO_KV_REST_URL": "https://kv.example.test",
              "DEMO_KV_REST_TOKEN": KV_TOKEN_VALUE, "PID_SECRET": PID_VALUE}  # fmt: skip
    values.update(over)
    return values


@functools.cache
def demo_fixture() -> Any:
    fx, report = build_from_golden()
    assert report.ok()
    return fx


# ======================= DEM-01 pages, replay default, live behind a passcode ==========


def test_entry_routes_exactly_the_learner_page_first_and_the_team_page():
    pages = deploy.PAGES
    assert [p.title for p in pages] == ["Course assistant", "Teaching team"]
    assert [p.default for p in pages] == [True, False]
    assert pages[0].path == ROOT / "src" / "course_assistant" / "app" / "learner_page.py"
    assert pages[1].path == ROOT / "src" / "course_assistant" / "app" / "team_page.py"
    assert all(p.path.is_file() for p in pages)
    entry = (ROOT / "deploy" / "streamlit_app.py").read_text(encoding="utf-8")
    assert "st.navigation" in entry and "startup_problem" in entry


def test_replay_shows_stored_answers_behind_the_passkey_with_no_model_call(tmp_path):
    fx = demo_fixture()
    rig = ChatRig(tmp_path)
    st = rig.state
    assert rig.chat.replay_options(rig.ready(st)) == ()  # no cache: no examples offered
    rig.demo.cache = demo_cache.DemoCache(fx)
    options = rig.chat.replay_options(st)
    assert [o[0] for o in options] == [c.id for c in fx.cases]
    assert [o[1] for o in options] == [c.question for c in fx.cases]
    for case in fx.cases:
        card = rig.chat.replay(st, case.id)
        assert card == replace(card_for(case.expected, fx.corpus), question_id=None,
                               can_escalate=False)  # fmt: skip
        assert card.status == case.expected_status
    assert rig.owner_client.calls == []  # replay makes no model call
    locked = ChatState.new()
    locked.own_key.set(OWN_KEY)  # a visitor with a key but no passkey
    for call in (lambda: rig.chat.replay_options(locked),
                 lambda: rig.chat.replay(locked, fx.cases[0].id)):  # fmt: skip
        with pytest.raises(Exception, match="passkey"):
            call()


def test_live_questions_need_the_passkey_even_with_an_own_key(tmp_path):
    rig = ChatRig(tmp_path)
    st = rig.state
    st.own_key.set(OWN_KEY)
    with pytest.raises(Exception, match="passkey"):
        rig.chat.ask(st, QUESTION)
    assert rig.owner_client.calls == [] and rig.keys_seen == []
    rig.ready(st)
    assert rig.chat.ask(st, QUESTION).status == "answered"


def _request(search: bool) -> ResponsesRequest:
    return ResponsesRequest(purpose="answer", model=s.LUNA, effort="low", instructions="i",
                            input=(InputItem(role="user", content="q"),),
                            vector_store_ids=("vs_1",) if search else ())  # fmt: skip


def test_hosted_tool_calls_are_capped_at_the_executors_bound(tmp_path):
    assert MAX_TOOL_CALLS == FILE_SEARCH_CALLS_BOUND == 3
    assert build_kwargs(_request(True))["max_tool_calls"] == FILE_SEARCH_CALLS_BOUND
    assert "max_tool_calls" not in build_kwargs(_request(False))
    assert "max_tool_calls" not in ResponsesRequest.model_fields  # replay keys unchanged
    rig = ChatRig(tmp_path)
    rig.chat.ask(rig.ready(), QUESTION)
    searched = [kw for _, kw in rig.owner_client.calls if "tools" in kw]
    assert searched and all(kw["max_tool_calls"] == MAX_TOOL_CALLS for kw in searched)


def _course_files(tmp_path: Path) -> tuple[Path, Path]:
    fx = demo_fixture()
    path = tmp_path / "replay.json"
    path.write_text(demo_cache.dumps(fx), encoding="utf-8")
    return path, ROOT / "sample_course" / "manifest.yaml"


def test_wiring_serves_the_course_and_replay_from_the_demo_cache_alone(tmp_path):
    fx = demo_fixture()
    replay, _ = _course_files(tmp_path)
    env = {"CA_REPLAY": str(replay), "CA_APP_DIR": str(tmp_path / "app")}
    chat = wiring.from_env(env, secrets_store={"DEMO_PASSKEY_HASH": HASH})
    st = ChatState.new()
    chat.unlock(st, CODE)
    assert chat.corpus == fx.corpus and chat.demo.cache is not None
    assert [o[0] for o in chat.replay_options(st)] == [c.id for c in fx.cases]
    with pytest.raises(wiring.WiringError):
        wiring.from_env({"CA_REPLAY": str(tmp_path / "missing.json")})


# ======================= DEM-02 secrets, startup check, logs ====================


@pytest.mark.safety
def test_secrets_template_names_every_secret_with_no_value_and_the_real_file_is_ignored():
    data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert tuple(data) == (*NAMES, "DEMO_TRUSTED_PROXY_HOPS")
    assert deploy.REQUIRED == NAMES
    assert all(v == "" for v in data.values())
    ignored = subprocess.run(["git", "check-ignore", "-q", ".streamlit/secrets.toml"],
                             cwd=ROOT, check=False)  # fmt: skip
    assert ignored.returncode == 0
    shown = subprocess.run(["git", "check-ignore", "-q", ".streamlit/secrets.toml.example"],
                           cwd=ROOT, check=False)  # fmt: skip
    assert shown.returncode == 1  # the template itself is committed
    assert not (ROOT / ".streamlit" / "secrets.toml").exists() or ignored.returncode == 0


@pytest.mark.safety
def test_startup_check_refuses_a_missing_secret_a_bad_hash_or_equal_hashes():
    assert deploy.startup_problem({}, good_secrets()) is None
    assert deploy.startup_problem(good_secrets(), None) is None  # environment works too
    for name in NAMES:
        for gone in ({k: v for k, v in good_secrets().items() if k != name},
                     good_secrets(**{name: "  "})):  # fmt: skip
            problem = deploy.startup_problem({}, gone)
            assert problem is not None and name in problem
    for name in ("DEMO_PASSKEY_HASH", "STAFF_PASSKEY_HASH"):
        problem = deploy.startup_problem({}, good_secrets(**{name: "123456"}))
        assert problem is not None and name in problem
    for url in ("http://kv.example.test", "kv.example.test"):
        problem = deploy.startup_problem({}, good_secrets(DEMO_KV_REST_URL=url))
        assert problem is not None and "DEMO_KV_REST_URL" in problem
    equal = deploy.startup_problem({}, good_secrets(STAFF_PASSKEY_HASH=HASH))
    assert equal is not None and "equal" in equal
    for problem in (equal, deploy.startup_problem({}, {})):
        for value in (OWNER_KEY_VALUE, KV_TOKEN_VALUE, PID_VALUE, HASH, STAFF_HASH):
            assert value not in (problem or "")


def test_the_pages_need_a_course_the_demo_cache_or_an_ingested_one(tmp_path):
    replay, _ = _course_files(tmp_path)
    assert deploy.course_problem({"CA_REPLAY": str(replay)}) is None
    assert deploy.course_problem({"CA_CONFIG": "course/config.yaml"}) is None
    for env in ({}, {"CA_REPLAY": str(tmp_path / "gone.json")}, {"CA_REPLAY": " "}):
        problem = deploy.course_problem(env)
        assert problem is not None and "CA_REPLAY" in problem


@pytest.mark.safety
def test_hashes_are_read_live_so_a_changed_hash_revokes_with_no_restart(tmp_path):
    from tests.app.test_chat import _course

    store = {"DEMO_PASSKEY_HASH": HASH, "STAFF_PASSKEY_HASH": STAFF_HASH}
    live = deploy.LiveSecrets(lambda: store)
    env = {"CA_CONFIG": str(_course(tmp_path)), "CA_APP_DIR": str(tmp_path / "app"),
           "CA_MANIFEST": str(ROOT / "sample_course" / "manifest.yaml")}  # fmt: skip
    chat = wiring.from_env(env, secrets_store=live)
    learner = ChatState.new()
    chat.unlock(learner, CODE)
    assert chat.locked_reason(learner) is None
    store["DEMO_PASSKEY_HASH"] = STAFF_HASH  # the owner rotates the code in the host store
    assert chat.locked_reason(learner) == NEEDED  # revoked at the next check, same object
    store.pop("DEMO_PASSKEY_HASH")
    assert chat.locked_reason(learner) == CLOSED
    assert live.get("missing") is None and "STAFF" not in repr(live)
    for page in ("learner_page.py", "team_page.py"):
        src = (ROOT / "src" / "course_assistant" / "app" / page).read_text(encoding="utf-8")
        assert "dict(st.secrets)" not in src and "deploy.LiveSecrets" in src, page


def test_team_wiring_reads_the_staff_hash_live_and_uses_the_demo_cache(tmp_path):
    replay, manifest = _course_files(tmp_path)
    store = {"DEMO_PASSKEY_HASH": HASH, "STAFF_PASSKEY_HASH": STAFF_HASH}
    env = {"CA_REPLAY": str(replay), "CA_MANIFEST": str(manifest),
           "CA_APP_DIR": str(tmp_path / "app")}  # fmt: skip
    team = team_wiring.from_env(env, secrets_store=deploy.LiveSecrets(lambda: store))
    st = TeamState.new()
    team.unlock(st, STAFF_CODE)
    assert team.locked_reason(st) is None
    store["STAFF_PASSKEY_HASH"] = HASH  # now equal to the learner hash: closed at once
    assert team.locked_reason(st) is not None


@pytest.mark.safety
def test_client_ip_trusts_only_the_configured_proxy_hops_from_the_right():
    headers = {"X-Forwarded-For": "6.6.6.6, 198.51.100.4, 203.0.113.9"}
    assert deploy.client_ip(headers, 0) is None  # no trusted proxy: never the header
    assert deploy.client_ip(headers, 1) == "203.0.113.9"
    assert deploy.client_ip(headers, 2) == "198.51.100.4"
    assert deploy.client_ip(headers, 4) is None  # fewer entries than hops
    assert deploy.client_ip({"x-forwarded-for": "2001:db8::1"}, 1) == "2001:db8::1"
    for bad in ("not-an-ip", "203.0.113.9:443", "", " , "):
        assert deploy.client_ip({"X-Forwarded-For": bad}, 1) is None
    assert deploy.client_ip(None, 1) is None and deploy.client_ip({}, 1) is None
    assert deploy.trusted_hops({}, None) == 0
    assert deploy.trusted_hops({"DEMO_TRUSTED_PROXY_HOPS": "1"}, None) == 1
    for bad in ("-1", "6", "one", "1.5", ""):
        assert deploy.trusted_hops({"DEMO_TRUSTED_PROXY_HOPS": bad}, None) == 0
    store = {"DEMO_TRUSTED_PROXY_HOPS": "2"}
    live = deploy.LiveSecrets(lambda: store)
    assert deploy.trusted_hops({}, live) == 2
    store["DEMO_TRUSTED_PROXY_HOPS"] = "0"  # read at every call
    assert deploy.trusted_hops({}, live) == 0
    for page in ("learner_page.py", "team_page.py"):
        src = (ROOT / "src" / "course_assistant" / "app" / page).read_text(encoding="utf-8")
        assert "ip_address" not in src and "deploy.client_ip" in src, page


@pytest.mark.safety
def test_streamlit_config_hides_error_details_and_usage_stats():
    cfg = tomllib.loads(CONFIG_TOML.read_text(encoding="utf-8"))
    assert cfg["client"]["showErrorDetails"] == "none"
    assert cfg["browser"]["gatherUsageStats"] is False
    assert cfg["server"]["headless"] is True
    assert cfg["server"]["enableXsrfProtection"] is True
    assert cfg["server"]["maxUploadSize"] <= 1


@pytest.mark.privacy
def test_logs_are_aggregate_only_for_a_live_turn_a_refused_unlock_and_a_startup_refusal(
    tmp_path, caplog
):
    rig = ChatRig(tmp_path)
    st = rig.state
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception, match="Wrong"):
            rig.chat.unlock(st, "99999999", ip=IP)
        rig.ready(st)
        rig.chat.ask(st, QUESTION)
        st.own_key.set(OWN_KEY)
        rig.chat.ask(st, QUESTION)
        problem = deploy.startup_problem({}, good_secrets(STAFF_PASSKEY_HASH=HASH))
        deploy.log_startup_problem(problem)
        ds = deploy.DailyStores(tmp_path / "days", today=lambda: dt.date(2026, 11, 20))
        ds()
    text = caplog.text
    assert "demo live: mode=owner status=answered" in text
    assert "demo startup refused" in text
    for value in (QUESTION, CODE, "99999999", HASH, STAFF_HASH, OWN_KEY, st.pid, IP,
                  OWNER_KEY_VALUE, KV_TOKEN_VALUE, PID_VALUE, HASH.split("$")[2]):  # fmt: skip
        assert value not in text, value


# ======================= DEM-03 off switch, nightly reset ========================


def test_the_staff_pause_is_the_off_switch_for_live_questions(tmp_path):
    rig = TeamRig(tmp_path, owner={k: v * 2 for k, v in s_happy().items()})
    learner = rig.learners.ready(rig.learners.state)
    staff = rig.unlock()
    rig.team.set_pause(staff, "all", True, confirmed=True)
    calls = len(rig.learners.owner_client.calls)
    card = rig.learners.chat.ask(learner, QUESTION)
    assert card.status == "paused" and card.text == Assistant.PAUSED_MESSAGE
    assert len(rig.learners.owner_client.calls) == calls  # no model call while off
    rig.team.set_pause(staff, "all", False, confirmed=True)
    assert rig.learners.chat.ask(learner, QUESTION).status == "answered"


def s_happy() -> dict[str, list[Any]]:
    from tests.answer.test_ask import happy

    return happy()


def test_removing_the_learner_hash_closes_every_page_at_the_next_check(tmp_path):
    rig = TeamRig(tmp_path)
    learner = rig.learners.ready(rig.learners.state)
    rig.learners.secret.value = None
    assert rig.learners.chat.locked_reason(learner) == CLOSED
    with pytest.raises(Exception, match="closed"):
        rig.learners.chat.ask(learner, QUESTION)


def test_daily_stores_give_one_store_per_utc_day_and_remove_earlier_days(tmp_path):
    day = [dt.date(2026, 11, 20)]
    root = tmp_path / "app"
    root.mkdir()
    (root / "spend.json").write_text("{}", encoding="utf-8")  # never touched by the reset
    (root / "notes").mkdir()  # nor any folder that is not a day
    ds = deploy.DailyStores(root, today=lambda: day[0])
    first = ds()
    assert isinstance(first, Store) and ds() is first
    assert (root / "2026-11-20" / "app.sqlite").is_file()
    at = dt.datetime(2026, 11, 20, 12, tzinfo=dt.UTC)
    first.set_pause("all", True, "instructor-1", at=at)
    assert ds().is_paused(None)
    day[0] = dt.date(2026, 11, 21)
    second = ds()
    assert second is not first and not second.is_paused(None)  # yesterday's data is gone
    assert not (root / "2026-11-20").exists() and (root / "2026-11-21").is_dir()
    assert (root / "spend.json").is_file() and (root / "notes").is_dir()
    assert repr(ds) == "DailyStores()"


def test_wiring_resets_daily_only_when_asked(tmp_path):
    replay, _ = _course_files(tmp_path)
    env = {"CA_REPLAY": str(replay), "CA_APP_DIR": str(tmp_path / "app"),
           "CA_DAILY_RESET": "1"}  # fmt: skip
    chat = wiring.from_env(env, secrets_store={"DEMO_PASSKEY_HASH": HASH})
    assert isinstance(chat.store, deploy.DailyStores)
    plain = wiring.from_env({**env, "CA_DAILY_RESET": ""}, {"DEMO_PASSKEY_HASH": HASH})
    assert not isinstance(plain.store, deploy.DailyStores)


# ======================= DEM-04 owner guide and smoke check ======================


def test_owner_guide_covers_every_deploy_step_and_holds_no_secret():
    guide = GUIDE.read_text(encoding="utf-8")
    for needle in ("hard limit", "course-assistant passkey-hash", "deploy/streamlit_app.py",
                   "make demo-cache", "Revoke", "scripts/smoke_demo.py", "Attests D3.2",
                   "DEMO_TRUSTED_PROXY_HOPS", "Pause", "portfolio-demos", *NAMES):  # fmt: skip
        assert needle in guide, needle
    assert not re.search(r"sk-[A-Za-z0-9_-]{8,}", guide)
    assert not re.search(r"pbkdf2_sha256\$\d", guide)


def _smoke() -> Any:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import smoke_demo
    finally:
        sys.path.pop(0)
    return smoke_demo


def _fm(kind: str, **fields: Any) -> bytes:
    from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
    from streamlit.proto.TextInput_pb2 import TextInput

    m = ForwardMsg()
    if kind == "finished":
        m.script_finished = ForwardMsg.FINISHED_SUCCESSFULLY
        return m.SerializeToString()
    el = m.delta.new_element
    if kind == "password":
        el.text_input.label = fields["label"]
        el.text_input.type = TextInput.PASSWORD
    elif kind == "chat_input":
        el.chat_input.placeholder = "Ask about the course materials"
    elif kind == "alert":
        el.alert.body = fields["body"]
    elif kind == "heading":
        el.heading.body = fields["body"]
    return m.SerializeToString()


class FakeStream:
    def __init__(self, messages: list[bytes]) -> None:
        self.messages = list(messages)
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, timeout: float) -> bytes | None:
        return self.messages.pop(0) if self.messages else None

    def close(self) -> None:
        pass


LOCKED_PAGE = [_fm("heading", body="Course assistant"), _fm("password", label="Passkey"),
               _fm("finished")]  # fmt: skip


def test_smoke_check_passes_only_on_a_page_locked_behind_the_passkey():
    smoke = _smoke()
    opened: list[str] = []

    def run(messages: list[bytes]) -> tuple[int, FakeStream]:
        stream = FakeStream(messages)

        def connect(url: str, timeout: float) -> FakeStream:
            opened.append(url)
            return stream

        return smoke.main(["https://demo-name.streamlit.app"], connect=connect), stream

    code, stream = run(LOCKED_PAGE)
    assert code == 0
    assert opened[-1] == "wss://demo-name.streamlit.app/~/+/_stcore/stream"
    from streamlit.proto.BackMsg_pb2 import BackMsg

    sent = BackMsg.FromString(stream.sent[0])
    assert sent.WhichOneof("type") == "rerun_script"
    assert len(sent.rerun_script.widget_states.widgets) == 0  # no passcode, no key sent
    open_page = [_fm("heading", body="Course assistant"), _fm("chat_input"), _fm("finished")]
    assert run(open_page)[0] == 1
    startup = [_fm("alert", body=deploy.LOCKED), _fm("finished")]
    assert run(startup)[0] == 1  # misconfigured: the startup lock is a failed deploy
    assert run([_fm("finished")])[0] == 1  # no passkey input at all
    staff = [_fm("password", label="Passkey"), _fm("heading", body="Teaching team"),
             _fm("finished")]  # fmt: skip
    assert run(staff)[0] == 1


def test_smoke_check_url_rules_and_connection_failures():
    smoke = _smoke()
    assert smoke.stream_url("http://localhost:8501") == "ws://localhost:8501/_stcore/stream"
    assert (
        smoke.stream_url("https://x.streamlit.app/") == "wss://x.streamlit.app/~/+/_stcore/stream"
    )
    assert smoke.main(["ftp://x"], connect=lambda u, t: FakeStream([])) == 2

    def down(url: str, timeout: float) -> Any:
        raise OSError("connection refused")

    assert smoke.main(["https://x.streamlit.app"], connect=down) == 2
    out = subprocess.run([sys.executable, str(SMOKE), "--help"], capture_output=True,
                         text=True, check=False)  # fmt: skip
    assert out.returncode == 0 and "URL" in out.stdout


def test_spec_names_the_owner_attest_row_and_the_catalog_marks_it():
    from harness_lib import load_catalog

    rows = {c.cap_id: c for c in load_catalog() if c.node == "D3.2"}
    assert set(rows) == {"DEM-01", "DEM-02", "DEM-03", "DEM-04"}
    assert rows["DEM-04"].owner_attest and not rows["DEM-01"].owner_attest
    assert rows["DEM-04"].status != "green" or rows["DEM-04"].proof.startswith("attest=OWNER")
    assert "owner attest" in SPEC.read_text(encoding="utf-8")
    assert json.dumps(deploy.DEFAULTS)  # plain names and paths only


# ======================= verifier pass 1 should-fix ================================


def _other_encoding(stored: str) -> str:
    """The same hash with its salt written in a non-canonical base64 (unused bits set)."""
    import base64

    scheme, n, salt, digest = stored.split("$")
    raw = base64.b64decode(salt)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    body, pad = salt.rstrip("="), "=" * (len(salt) - len(salt.rstrip("=")))
    for c in alphabet:
        other = body[:-1] + c + pad
        if other != salt and base64.b64decode(other) == raw:
            return "$".join((scheme, n, other, digest))
    raise AssertionError("no other encoding")  # pragma: no cover


@pytest.mark.safety
def test_equal_hashes_are_compared_by_value_and_hashes_must_be_canonical():
    from course_assistant.demo import passkey
    from course_assistant.demo._errors import PasskeyConfigError

    padded = HASH.replace("$100000$", "$0100000$")
    assert padded != HASH
    for staff in (padded, " " + HASH + "\n"):
        problem = deploy.startup_problem({}, good_secrets(STAFF_PASSKEY_HASH=staff))
        assert problem is not None and "STAFF_PASSKEY_HASH" in problem
    # a non-canonical hash is refused everywhere, so the gate stays closed on it
    for bad in (padded, _other_encoding(HASH)):
        assert bad != HASH
        with pytest.raises(PasskeyConfigError):
            passkey.verify_passcode(CODE, bad)
    assert passkey.same_hash(HASH, HASH) and not passkey.same_hash(HASH, STAFF_HASH)
    assert passkey.verify_passcode(CODE, HASH)  # canonical hashes still work
    from course_assistant.app.team import staff_hash

    gate = passkey.PasskeyGate(staff_hash({}, good_secrets(STAFF_PASSKEY_HASH=padded)))
    with pytest.raises(Exception, match="closed"):  # the staff page stays closed on it
        gate.unlock(CODE, session="s")


@pytest.mark.safety
def test_the_same_code_under_another_salt_never_opens_the_staff_page(tmp_path):
    from course_assistant.demo import passkey
    from tests.demo.test_demo import FAST

    rig = TeamRig(tmp_path)
    rig.staff_secret.value = passkey.hash_passcode(CODE, iterations=FAST)  # same code, new salt
    assert (
        deploy.startup_problem({}, good_secrets(STAFF_PASSKEY_HASH=rig.staff_secret.value)) is None
    )
    st = TeamState.new()
    with pytest.raises(Exception, match="passkey"):
        rig.team.unlock(st, CODE)
    assert rig.team.locked_reason(st) is not None
    assert "must differ" in GUIDE.read_text(encoding="utf-8")


def _lock_versions() -> dict[str, set[str]]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for pkg in lock["package"]:
        if "version" in pkg:
            out.setdefault(pkg["name"], set()).add(pkg["version"])
    return out


def _pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        req = line.split(";")[0].strip()
        m = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", req)
        assert m, f"not an exact pin: {line}"
        pins[m[1].lower()] = m[2]
    return pins


# third-party top-level modules of the deployed code -> distribution in the lock
DIST = {"yaml": "pyyaml", "pydantic": "pydantic", "openai": "openai", "streamlit": "streamlit",
        "httpx2": "httpx2", "websockets": "websockets"}  # fmt: skip
NOT_DEPLOYED = {"evalkit", "pypdf", "pptx"}  # eval and ingest extras: never on the demo path


def _deployed_imports() -> set[str]:
    import ast

    std = set(sys.stdlib_module_names) | {"course_assistant", "__future__"}
    files = [p for p in (ROOT / "src" / "course_assistant").rglob("*.py") if "eval" not in p.parts]
    files += [ROOT / "deploy" / "streamlit_app.py"]
    mods: set[str] = set()
    for f in files:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods.add(node.module.split(".")[0])
    return mods - std


def test_deploy_requirements_pin_the_lock_and_cover_every_direct_import():
    pins, lock = _pins(), _lock_versions()
    for name, version in pins.items():
        assert version in lock.get(name, set()), f"{name}=={version} is not the uv.lock version"
    imports = _deployed_imports() - NOT_DEPLOYED
    assert imports <= set(DIST), f"map these imports to a distribution: {imports - set(DIST)}"
    for mod in imports:
        assert DIST[mod] in pins, f"{mod} ({DIST[mod]}) is imported but not in requirements"
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for spec in [*project["dependencies"], *project["optional-dependencies"]["app"],
                 *project["optional-dependencies"]["openai"]]:  # fmt: skip
        assert re.split(r"[<>=!~ \[]", spec)[0].lower() in pins, spec
    # the demo's import closure never pulls an extra that is not installed on the host
    extras = sorted(NOT_DEPLOYED)
    code = ("import sys; import course_assistant.app.wiring, course_assistant.app.team_wiring, "
            "course_assistant.app.deploy, course_assistant.demo.cache, course_assistant.llm.client;"
            f" print(sorted(m for m in {extras!r} if m in sys.modules))")  # fmt: skip
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, cwd=ROOT)  # fmt: skip
    assert out.stdout.strip() == "[]"


def test_deploy_requirements_equal_a_fresh_uv_export():
    import shutil

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH; the lock check above still runs")
    out = subprocess.run([uv, "export", "--frozen", "--no-hashes", "--no-dev", "--extra", "app",
                          "--extra", "openai", "--no-emit-project", "--no-header"],
                         capture_output=True, text=True, check=True, cwd=ROOT)  # fmt: skip

    def lines(text: str) -> list[str]:
        return [
            x.strip() for x in text.splitlines() if x.strip() and not x.lstrip().startswith("#")
        ]

    assert lines(REQUIREMENTS.read_text(encoding="utf-8")) == lines(out.stdout)


def test_guide_says_the_app_is_locked_until_the_demo_cache_is_committed(tmp_path):
    guide = GUIDE.read_text(encoding="utf-8")
    assert "offers live questions only" not in guide
    assert "demo/replay.json" in guide and "locked page" in guide
    assert deploy.course_problem({"CA_REPLAY": str(tmp_path / "replay.json")}) is not None


def test_a_hash_with_too_many_iterations_is_refused():
    """D3.2 verifier pass 2: 99,999,999 iterations would make each unlock cost minutes."""
    from course_assistant.demo import passkey
    from course_assistant.demo._errors import PasskeyConfigError

    good = passkey.hash_passcode("123456", iterations=passkey.MIN_ITERATIONS)
    huge = good.replace(f"${passkey.MIN_ITERATIONS}$", "$99999999$", 1)
    with pytest.raises(ValueError):
        passkey.hash_passcode("123456", iterations=passkey.MAX_ITERATIONS + 1)
    with pytest.raises(PasskeyConfigError):
        passkey.verify_passcode("123456", huge)
