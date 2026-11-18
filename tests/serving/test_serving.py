"""P4.1 groundwork (D-57 (4)): the pages serve only the gate-approved corpus version named by
the corpus store's live pointer (`LIVE.json`), never simply the latest ingest. A removal is
served at once; a missing or unreadable pointer locks a pilot page; the demo serves its replay
corpus; the served version id is in every trace line and on the staff view. No network: the
owner's OpenAI client is replaced by N1.2's scripted step client."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from course_assistant.app import team_wiring, wiring
from course_assistant.app.chat import ChatState, session_labels
from course_assistant.app.team import TeamState
from course_assistant.demo._errors import GuardRefused
from course_assistant.demo.passkey import NEEDED
from course_assistant.ingest import serving
from course_assistant.ingest.serving import LIVE_FILE, ServedCorpus, ServingError
from tests.answer import scripted as s
from tests.answer.test_ask import CORPUS, QUESTION, S01, happy
from tests.app.team_rig import STAFF_CODE, STAFF_HASH
from tests.demo.test_demo import CODE, HASH
from tests.serving.helpers import NEW_FILE, Course, with_addition, without_session

LIVE_V = CORPUS.corpus_version
SECRETS = {"DEMO_PASSKEY_HASH": HASH, "STAFF_PASSKEY_HASH": STAFF_HASH}


def chat_for(course: Course, **over: str) -> Any:
    return wiring.from_env(course.env(**over), secrets_store=SECRETS)


def ready(chat: Any) -> ChatState:
    st = ChatState.new()
    chat.unlock(st, CODE)
    chat.accept_notice(st)
    return st


def traces(course: Course) -> list[dict[str, Any]]:
    path = course.app_dir / "trace.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ======================= the served view (ingest.serving) =======================


@pytest.mark.parametrize("damage", ["missing", "not json", "a list", "no version",
                                    "not a sha", "unknown version", "binary"])  # fmt: skip
def test_no_pointer_or_an_unreadable_one_serves_nothing(tmp_path, damage):
    course = Course(tmp_path, live=damage != "missing")
    text = {"not json": "{", "a list": "[]", "no version": "{}",
            "not a sha": json.dumps({"corpus_version": "../../etc/passwd"}),
            "unknown version": json.dumps({"corpus_version": "f" * 64})}.get(damage)  # fmt: skip
    if text is not None:
        course.live_file.write_text(text, encoding="utf-8")
    if damage == "binary":
        course.live_file.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ServingError) as exc:
        ServedCorpus(course.store)()
    assert "passwd" not in str(exc.value) and str(tmp_path) not in str(exc.value)


def test_a_newer_ingest_is_not_served_until_the_pointer_flips(tmp_path):
    course = Course(tmp_path)
    served = ServedCorpus(course.store)
    candidate = with_addition()
    course.ingest(candidate)
    assert course.store.current() == candidate  # the latest ingest is the candidate
    assert served() == CORPUS  # but the live version is what is served
    course.mark_live(candidate.corpus_version)  # the gate passed
    assert served() == candidate  # same object, no restart


def test_a_removal_is_served_at_once_and_a_removal_only_flip_too(tmp_path):
    course = Course(tmp_path)
    served = ServedCorpus(course.store)
    removal = without_session("s02")
    course.ingest(removal)
    view = served()  # before any flip: the removed unit is no longer served
    assert {u.session for u in view.units} == {"s01", "s03"}
    assert view.units == removal.units and view.corpus_version == removal.corpus_version
    mixed = with_addition(removal)  # a later candidate adds a unit: still not served
    course.ingest(mixed)
    assert served().corpus_version == removal.corpus_version


def test_a_removal_only_flip_by_the_release_gate_is_served(tmp_path):
    pytest.importorskip("evalkit")
    from course_assistant.eval import release

    course = Course(tmp_path)
    served = ServedCorpus(course.store)
    removal = without_session("s03")
    course.ingest(removal)
    now = dt.datetime(2026, 11, 17, tzinfo=dt.UTC)
    assert release.apply_removals(course.store, now=now, settings={})  # E2.4 shortcut
    assert json.loads(course.live_file.read_text(encoding="utf-8"))["how"] == "removal"
    assert served() == removal


def test_everything_removed_serves_nothing(tmp_path):
    course = Course(tmp_path)
    from tests.serving.helpers import corpus_of, new_unit

    course.ingest(corpus_of([new_unit()]))  # every live unit removed, one new one waiting
    with pytest.raises(ServingError):
        ServedCorpus(course.store)()


def test_the_view_equals_the_release_gates_serving_corpus(tmp_path):
    pytest.importorskip("evalkit")
    from course_assistant.eval import release

    assert LIVE_FILE == release.LIVE_FILE
    course = Course(tmp_path)
    steps = [None, with_addition(), without_session("s02"),
             with_addition(without_session("s01")), CORPUS]  # fmt: skip
    for candidate in steps:
        if candidate is not None:
            course.ingest(candidate)
        assert serving.serving_corpus(course.store) == release.serving_corpus(course.store)


# ======================= the learner page (wiring) =======================


def test_a_pilot_course_with_no_live_pointer_does_not_start(tmp_path):
    course = Course(tmp_path, live=False)
    with pytest.raises(wiring.WiringError, match="release gate") as exc:
        chat_for(course)
    assert str(tmp_path) not in str(exc.value)
    problem = wiring.serving_problem(course.env())
    assert problem is not None and str(tmp_path) not in problem
    course.mark_live(LIVE_V)
    assert wiring.serving_problem(course.env()) is None
    course.live_file.write_text("{", encoding="utf-8")
    assert wiring.serving_problem(course.env()) is not None
    with pytest.raises(wiring.WiringError):
        chat_for(course)


def test_the_pilot_chat_serves_the_live_corpus_not_the_newer_ingest(tmp_path):
    course = Course(tmp_path)
    chat = chat_for(course)
    st = ready(chat)
    course.ingest(with_addition())
    assert chat.locked_reason(st) is None
    assert chat.corpus == CORPUS
    assert chat.session_options(st) == session_labels(CORPUS)  # no Session 4
    assistant = chat.demo.assistant_for(object())
    assert assistant.corpus == CORPUS


def test_a_live_turn_answers_and_traces_only_the_served_version(tmp_path, monkeypatch):
    import openai

    from course_assistant import llm
    from tests.answer.test_ask import PRICES

    queues = happy()
    for step, items in happy(file_id=NEW_FILE).items():
        queues[step] = [*queues[step], *items]
    client = s.StepClient(**queues)

    class ScriptedOpenAI:  # a class, as N1.2 checks isinstance(client, openai.OpenAI)
        def __new__(cls, **_: Any) -> Any:
            return client

    monkeypatch.setattr(openai, "OpenAI", ScriptedOpenAI)
    monkeypatch.setattr(llm, "load_prices", lambda: PRICES)  # the scripted models' prices
    course = Course(tmp_path)
    chat = chat_for(course, OPENAI_API_KEY="sk-test-owner-key-p41")
    st = ready(chat)
    first = chat.ask(st, QUESTION)
    assert first.status == "answered"
    course.ingest(with_addition())  # a newer ingest that has not been gated
    chat.locked_reason(st)
    second = chat.ask(st, QUESTION)  # the draft cites a unit of the newer ingest only
    assert second.status != "answered"
    searches = [json.dumps(kw["tools"]) for step, kw in client.calls if step == "answer"]
    candidate = with_addition().corpus_version
    assert len(searches) == 2 and all(LIVE_V in f and candidate not in f for f in searches)
    assert '"served_version"' not in searches[0] and '"served_version"' in searches[1]
    records = traces(course)
    assert records and all(r.get("version") == LIVE_V for r in records)
    raw = (course.app_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert with_addition().corpus_version not in raw
    assert S01 not in raw  # ids only, no unit ids


def test_each_request_searches_the_served_files_only_while_a_candidate_waits(tmp_path):
    course = Course(tmp_path)
    chat = chat_for(course)
    ready(chat)
    assert chat.demo.assistant_for(object()).version_key == "corpus_version"
    candidate = with_addition()
    course.ingest(candidate)
    waiting = chat.demo.assistant_for(object())
    assert waiting.corpus == CORPUS and waiting.version_key == "served_version"
    course.mark_live(candidate.corpus_version)  # the gate passed: no re-sync, no restart
    flipped = chat.demo.assistant_for(object())
    assert flipped.corpus == candidate and flipped.version_key == "corpus_version"
    removal = without_session("s02", candidate)
    course.ingest(removal)  # removal-only: served at once, on its own corpus_version
    gone = chat.demo.assistant_for(object())
    assert gone.corpus == removal and gone.version_key == "corpus_version"


def test_a_removal_reaches_the_running_chat_at_once(tmp_path):
    course = Course(tmp_path)
    chat = chat_for(course)
    st = ready(chat)
    removal = without_session("s02")
    course.ingest(removal)
    assert chat.locked_reason(st) is None
    assert chat.session_options(st) == session_labels(removal)
    assert chat.demo.assistant_for(object()).corpus == removal
    course.mark_live(removal.corpus_version)  # the removal-only flip
    assert chat.locked_reason(st) is None and chat.corpus == removal


@pytest.mark.safety
def test_a_pointer_lost_after_startup_locks_the_page_and_the_passkey_still_rules(tmp_path):
    course = Course(tmp_path)
    chat = chat_for(course)
    fresh = ChatState.new()
    assert chat.locked_reason(fresh) == NEEDED  # a good pointer opens nothing by itself
    st = ready(chat)
    course.live_file.write_text("{", encoding="utf-8")
    assert chat.locked_reason(st) == wiring.SERVING_LOCKED
    assert chat.locked_reason(fresh) == wiring.SERVING_LOCKED
    for call in (lambda: chat.session_options(st), lambda: chat.ask(st, QUESTION),
                 lambda: chat.demo.assistant_for(object())):  # fmt: skip
        with pytest.raises(GuardRefused):
            call()
    with pytest.raises(GuardRefused):
        chat.unlock(fresh, CODE)
        chat.session_options(fresh)
    course.mark_live(LIVE_V)
    assert chat.locked_reason(st) is None  # restored: the passkey session holds
    assert chat.locked_reason(ChatState.new()) == NEEDED


def test_demo_mode_serves_the_replay_corpus_and_ignores_any_pointer(tmp_path):
    from tests.deploy.test_deploy import _course_files, demo_fixture

    fx = demo_fixture()
    replay, _ = _course_files(tmp_path)
    env = {"CA_REPLAY": str(replay), "CA_APP_DIR": str(tmp_path / "app")}
    assert wiring.serving_problem(env) is None  # the choice: the demo needs no LIVE.json
    chat = wiring.from_env(env, secrets_store=SECRETS)
    st = ChatState.new()
    chat.unlock(st, CODE)
    assert chat.locked_reason(st) is None and chat.corpus == fx.corpus
    no_calls: Any = object()  # nothing is called
    assistant: Any = chat.demo.assistant_for(no_calls)
    assert assistant.corpus == fx.corpus
    assistant.sink.emit({"step": "route", "request_id": "r-" + "0" * 16})
    line = json.loads((tmp_path / "app" / "trace.jsonl").read_text(encoding="utf-8"))
    assert line["version"] == fx.corpus.corpus_version


def test_the_versioned_sink_adds_the_id_only_and_keeps_deletion():
    from course_assistant.llm import strip_text

    class Inner:
        def __init__(self) -> None:
            self.got: list[dict[str, Any]] = []
            self.deleted: list[str] = []

        def emit(self, record: dict[str, Any]) -> None:
            self.got.append(record)

        def delete_pid(self, pid: str, conversation_ids: Any = None, known: Any = ()) -> int:
            self.deleted.append(pid)
            return 1

    inner = Inner()
    sink = wiring.VersionedSink(inner, lambda: LIVE_V)
    sink.emit({"step": "route", "version": "1.0"})
    assert inner.got == [{"step": "route", "version": LIVE_V}]
    assert strip_text(inner.got[0]) == inner.got[0]  # the trace allowlist keeps it
    assert sink.delete_pid("0123456789abcdef") == 1 and inner.deleted == ["0123456789abcdef"]
    none = wiring.VersionedSink(inner, lambda: None)
    none.emit({"step": "route"})
    assert "version" not in inner.got[-1]


# ======================= the teaching-team page (team_wiring) =======================


def team_for(course: Course) -> Any:
    return team_wiring.from_env(course.env(), secrets_store=SECRETS)


def test_the_staff_view_shows_the_served_version_not_the_newer_ingest(tmp_path):
    course = Course(tmp_path)
    team = team_for(course)
    st = TeamState.new()
    team.unlock(st, STAFF_CODE)
    course.ingest(with_addition())
    assert team.locked_reason(st) is None
    assert team.manifest_view(st).corpus_version == LIVE_V
    removal = without_session("s02")
    course.ingest(removal)
    course.mark_live(removal.corpus_version)
    assert team.locked_reason(st) is None
    assert team.manifest_view(st).corpus_version == removal.corpus_version


def test_the_ops_view_shows_the_served_version_read_only(tmp_path):
    course = Course(tmp_path)
    team = team_for(course)
    st = TeamState.new()
    team.unlock(st, STAFF_CODE)
    course.ingest(with_addition())
    assert team.locked_reason(st) is None
    assert team.ops(st).corpus_version == LIVE_V  # not the waiting candidate
    removal = without_session("s02")
    course.ingest(removal)
    assert team.locked_reason(st) is None
    assert team.ops(st).corpus_version == removal.corpus_version
    assert course.live_file.exists() and json.loads(
        course.live_file.read_text(encoding="utf-8"))["corpus_version"] == LIVE_V  # fmt: skip


@pytest.mark.safety
def test_the_staff_view_locks_without_a_pointer_and_keeps_its_own_passkey(tmp_path):
    course = Course(tmp_path, live=False)
    with pytest.raises(team_wiring.WiringError, match="release gate"):
        team_for(course)
    course.mark_live(LIVE_V)
    team = team_for(course)
    st = TeamState.new()
    with pytest.raises(GuardRefused):
        team.unlock(st, CODE)  # the learner code never opens the staff view
    team.unlock(st, STAFF_CODE)
    course.live_file.unlink()
    assert team.locked_reason(st) == wiring.SERVING_LOCKED
    with pytest.raises(GuardRefused):
        team.manifest_view(st)
    course.mark_live(LIVE_V)
    assert team.locked_reason(st) is None
