"""D3.1 demo replay and guard: RPL-01 to RPL-04.

Offline only: A2.5 replays, scripted model outputs through N1.2's real `respond`, a
file-backed spend counter, an in-memory KV and an injected REST transport. Public split only.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hmac
import io
import json
import logging
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from course_assistant import cli
from course_assistant.answer import golden, responder
from course_assistant.answer.ask import Assistant
from course_assistant.answer.replay import Live, Replay
from course_assistant.answer.route import RouteStep
from course_assistant.demo import cache as demo_cache
from course_assistant.demo import passkey, spend
from course_assistant.demo._errors import DemoCacheError, GuardRefused, PasskeyConfigError
from course_assistant.demo.guard import (
    BUDGET_USED,
    STORE_DOWN,
    CappedSource,
    Guard,
    GuardConfig,
    call_bound_usd,
)
from course_assistant.demo.live import LiveDemo
from course_assistant.escalation import team_queue
from course_assistant.llm import InputItem, LLMError, ResponsesRequest
from course_assistant.llm.client import MAX_ATTEMPTS
from course_assistant.store.db import Store
from tests.answer import scripted as s
from tests.answer.test_ask import (
    ASSESSMENTS,
    CONFIG,
    CORPUS,
    PID,
    PRICES,
    QUESTION,
    S01,
    Clock,
    Sink,
    happy,
    ids,
)

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_CASES = ROOT / "golden" / "public" / "cases.jsonl"  # never golden/heldout
SPEC = ROOT / "specs" / "D3.1-demo-guard" / "spec.md"
CODE = "4815162342"
OTHER_CODE = "1123581321"
SALT = b"0123456789abcdef"
FAST = 100_000  # the minimum iterations: fast tests, same scheme
HASH = passkey.hash_passcode(CODE, salt=SALT, iterations=FAST)
VISITOR_KEY = "sk-visitor-MARKER-7f3a9c"
DAY = "2026-11-20"
SESSION = "sess-a"
IP = "203.0.113.7"


def public_rows() -> list[dict[str, Any]]:
    return [json.loads(x) for x in PUBLIC_CASES.read_text(encoding="utf-8").splitlines()
            if x.strip()]  # fmt: skip


# ---------- helpers ----------


class Secret:
    """A secret store whose passkey hash can be changed or removed (revocation)."""

    def __init__(self, value: str | None = HASH) -> None:
        self.value = value

    def __call__(self) -> str | None:
        return self.value


class Tick:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class MemoryKV:
    """An in-memory KV with the hosted store's INCRBY semantics (atomic per command)."""

    def __init__(self) -> None:
        self.data: dict[str, int] = {}
        self.expiry: dict[str, int] = {}
        self.lock = threading.Lock()
        self.down = False

    def incrby(self, key: str, amount: int) -> int:
        if self.down:
            raise ConnectionError("kv down: token=abc")
        with self.lock:
            self.data[key] = self.data.get(key, 0) + amount
            return self.data[key]

    def get(self, key: str) -> int | None:
        if self.down:
            raise ConnectionError("kv down")
        return self.data.get(key)

    def expire(self, key: str, seconds: int) -> None:
        self.expiry[key] = seconds


class DownCounter:
    def reserve(self, day: str, amount: int, cap: int) -> bool:
        raise ConnectionError("db at 10.0.0.1 password=hunter2")

    def settle(self, day: str, reserved: int, actual: int) -> None:
        raise ConnectionError("down")

    def spent(self, day: str) -> int:
        raise ConnectionError("down")


def unlocked_gate(secret: Secret | None = None, clock: Tick | None = None,
                  session: str = SESSION) -> passkey.PasskeyGate:  # fmt: skip
    gate = passkey.PasskeyGate(secret or Secret(), clock=clock or Tick())
    gate.unlock(CODE, session=session, ip=IP)
    return gate


def guard_with(counter: Any, gate: passkey.PasskeyGate | None = None,
               config: GuardConfig | None = None, clock: Tick | None = None,
               alerts: list | None = None) -> Guard:  # fmt: skip
    return Guard(
        gate or unlocked_gate(),
        counter,
        config or GuardConfig(),
        clock=clock or Tick(),
        today=lambda: DAY,
        alert=None if alerts is None else lambda w, k: alerts.append((w, k)),
    )


def scripted(**queues: list[Any]) -> tuple[Any, s.StepClient]:
    client = s.StepClient(**queues)
    return responder(client=client, prices=PRICES, sleep=lambda _: None), client


class Rig:
    """A LiveDemo over scripted outputs, a file-backed counter and an in-memory store."""

    def __init__(self, tmp: Path, *, owner: dict[str, list[Any]] | None = None,
                 visitor: dict[str, list[Any]] | None = None,
                 config: GuardConfig | None = None) -> None:  # fmt: skip
        self.secret = Secret()
        self.clock = Tick()
        self.gate = passkey.PasskeyGate(self.secret, clock=self.clock)
        self.counter = spend.FileSpendCounter(tmp / "spend.json")
        self.alerts: list[tuple[str, str]] = []
        self.guard = guard_with(self.counter, self.gate, config, self.clock, self.alerts)
        self.store = Store(":memory:")
        self.sink = Sink()
        self.new_id = ids()
        self.owner, self.owner_client = scripted(**(owner if owner is not None else happy()))
        self.visitor_queues = visitor if visitor is not None else happy()
        self.keys_seen: list[str] = []
        self.visitor_client: s.StepClient | None = None
        self.demo = LiveDemo(self.gate, self.guard, assistant_for=self.assistant_for,
                             owner_responder=self.owner, responder_for_key=self.for_key,
                             prices=PRICES, alert=self.note)  # fmt: skip

    def note(self, what: str, kind: str) -> None:
        self.alerts.append((what, kind))

    def assistant_for(self, source: Any) -> Assistant:
        return Assistant(CONFIG, CORPUS, ASSESSMENTS, self.store, source, sink=self.sink,
                         now=Clock(), timer=lambda: 0.0, new_id=self.new_id)  # fmt: skip

    def for_key(self, key: str) -> Any:
        self.keys_seen.append(key)
        rsp, self.visitor_client = scripted(**self.visitor_queues)
        return rsp

    def unlock(self, session: str = SESSION) -> None:
        self.gate.unlock(CODE, session=session, ip=IP)


# ======================= RPL-01 demo cache =======================


def test_curated_holds_about_twenty_public_questions_with_two_per_status():
    ids_ = [cid for cid, _ in demo_cache.CURATED]
    assert 18 <= len(ids_) <= 22 and len(set(ids_)) == len(ids_)
    for status in demo_cache.SHOWN:
        assert sum(1 for _, st in demo_cache.CURATED if st == status) >= demo_cache.MIN_PER_STATUS
    assert "error" not in {st for _, st in demo_cache.CURATED}
    rows = {r["id"]: r for r in public_rows()}
    for cid, status in demo_cache.CURATED:
        assert rows[cid]["split"] == "tuning"  # the public split, never held out
        assert rows[cid]["expected_status"] == status
    cases = demo_cache.curated_cases(public_rows())
    assert [c.source for c in cases] == ids_
    assert len({c.request_id for c in cases}) == len(cases)
    assert all(c.request_id == demo_cache.request_id_for(c.source) for c in cases)


def test_curated_cases_refuse_an_id_outside_the_public_split():
    rows = public_rows()
    with pytest.raises(DemoCacheError, match="not in the public"):
        demo_cache.curated_cases(rows, (("ta-heldout-001", "answered"),))
    private = [{**rows[0], "split": "heldout"}]
    with pytest.raises(DemoCacheError, match="not public"):
        demo_cache.curated_cases(private, ((rows[0]["id"], "answered"),))


def golden_by_source() -> tuple[golden.Fixture, dict[str, golden.Case]]:
    fx = golden.load()
    return fx, {c.source: c for c in fx.cases}


def rekeyed(golden_case: golden.Case, request_id: str) -> Replay:
    """The golden fixture's scripted recordings of a public case, under the demo request id:
    a fake live source that answers exactly what A2.5 recorded (no API call)."""
    return Replay(r.model_copy(update={"request_id": request_id})
                  for r in golden_case.recordings)  # fmt: skip


def build_from_golden(curated: tuple = demo_cache.CURATED) -> tuple[golden.Fixture, Any]:
    fx, by_source = golden_by_source()
    cases = demo_cache.curated_cases(public_rows(), curated)
    return demo_cache.build_cache(
        cases, config=fx.config, corpus=fx.corpus, assessments=fx.assessments,
        source_for=lambda c: rekeyed(by_source[c.source], c.request_id), now=fx.now)  # fmt: skip


@pytest.mark.golden
def test_demo_cache_built_through_ask_replays_to_identical_answers(tmp_path):
    fx, report = build_from_golden()
    assert report.ok() and not report.skipped
    assert len(fx.cases) == len(demo_cache.CURATED)
    assert report.per_status == {"answered": 6, "not_covered": 7, "hint_only": 4,
                                 "handed_over": 3}  # fmt: skip
    path = tmp_path / "replay.json"
    path.write_text(demo_cache.dumps(fx), encoding="utf-8")
    loaded = demo_cache.load_cache(path)
    assert loaded == fx
    assert demo_cache.verify_cache(loaded) == [(c.id, True) for c in fx.cases]
    page = demo_cache.DemoCache(loaded)
    for case in fx.cases:
        answer = page.answer(case.id)
        assert answer.status == case.expected_status == page.status(case.id)
        assert answer.rules_hash == Assistant.rules_hash()
    # a tampered stored answer is caught by the offline replay
    first = loaded.cases[0]
    assert first.expected is not None
    bad = first.model_copy(update={"expected": first.expected.model_copy(
        update={"text": "something else"})})  # fmt: skip
    tampered = loaded.model_copy(update={"cases": (bad, *loaded.cases[1:])})
    assert demo_cache.verify_cache(tampered)[0] == (bad.id, False)


def test_a_case_showing_another_status_is_left_out_and_too_few_fails_the_build():
    curated = (*demo_cache.CURATED[:-1], ("ta-b4-010", "answered"))  # really handed over
    fx, report = build_from_golden(curated)
    assert [(x.case_id, x.reason, x.got) for x in report.skipped] == [
        ("demo-20", "status", "handed_over")
    ]
    assert len(fx.cases) == len(curated) - 1 and report.per_status["handed_over"] == 2
    one_each = tuple(next(c for c in demo_cache.CURATED if c[1] == st) for st in demo_cache.SHOWN)
    with pytest.raises(DemoCacheError, match="at least two") as err:
        build_from_golden(one_each)
    assert err.value.report.per_status == dict.fromkeys(demo_cache.SHOWN, 1)  # type: ignore[attr-defined]
    rendered = err.value.report.render()  # type: ignore[attr-defined]
    rows = {r["id"]: r for r in public_rows()}
    assert not any(rows[cid]["question"] in rendered for cid, _ in demo_cache.CURATED)


def test_recordings_that_do_not_replay_are_left_out():
    fx, by_source = golden_by_source()
    cases = demo_cache.curated_cases(public_rows())

    def source_for(c: golden.Case) -> Replay:
        if c.source == "ta-b1-001":
            return Replay(())  # nothing recorded: the run cannot be served
        return rekeyed(by_source[c.source], c.request_id)

    built, report = demo_cache.build_cache(cases, config=fx.config, corpus=fx.corpus,
                                           assessments=fx.assessments, source_for=source_for,
                                           now=fx.now)  # fmt: skip
    assert [(x.case_id, x.reason) for x in report.skipped] == [("demo-01", "replay")]
    assert "demo-01" not in {c.id for c in built.cases}


@pytest.mark.safety
def test_demo_cache_script_refuses_without_yes_key_or_budget_and_dry_runs(
        tmp_path, monkeypatch, capsys):  # fmt: skip
    import demo_cache as script

    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps(CONFIG.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert script.main(["--config", str(cfg)]) == 2
    assert script.main(["--config", str(cfg), "--yes"]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err
    assert script.main(["--config", str(cfg), "--dry-run", "--out", str(tmp_path / "r.json")]) == 0
    out = capsys.readouterr().out
    assert "estimate: at most $10.00" in out and "20 curated" in out
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    assert script.main(["--config", str(cfg), "--yes", "--max-cost", "1"]) == 3
    assert not (tmp_path / "r.json").exists()
    # --check replays a cache offline and prints no question or answer text
    fx, _ = build_from_golden()
    path = tmp_path / "replay.json"
    path.write_text(demo_cache.dumps(fx), encoding="utf-8")
    capsys.readouterr()
    assert script.main(["--check", str(path)]) == 0
    printed = capsys.readouterr().out
    assert "20/20 replay exactly" in printed
    assert not any(c.question in printed or (c.expected and c.expected.text in printed)
                   for c in fx.cases)  # fmt: skip
    assert script.main(["--check", str(tmp_path / "missing.json")]) == 2


def test_make_demo_cache_runs_the_script():
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "demo-cache:" in text and "scripts/demo_cache.py" in text
    assert " --yes" not in text.split("demo-cache:")[1].split("\n\n")[0]  # caller confirms


# ======================= RPL-02 guard, counter, executor =======================


def test_guard_checks_passkey_cap_rate_limit_and_daily_budget(tmp_path):
    counter = spend.FileSpendCounter(tmp_path / "s.json")
    clock = Tick()
    gate = passkey.PasskeyGate(Secret(), clock=clock)
    g = guard_with(counter, gate, GuardConfig(request_hold_usd=0.5, daily_cap_usd=1.2,
                                              rate_limit=2, rate_window_s=60), clock)  # fmt: skip
    with pytest.raises(GuardRefused, match="passkey"):
        g.allow(0.5, SESSION)  # not unlocked: nothing else is even checked
    gate.unlock(CODE, session=SESSION, ip=IP)
    with pytest.raises(GuardRefused, match="per-request limit"):
        g.allow(0.51, SESSION)
    t1 = g.allow(0.5, SESSION)
    t2 = g.allow(0.5, SESSION)
    assert counter.spent(DAY) == 1_000_000 and t1.hold_micro == 500_000
    with pytest.raises(GuardRefused, match="Rate limit"):
        g.allow(0.5, SESSION)
    gate.unlock(CODE, session="sess-b", ip=IP)
    with pytest.raises(GuardRefused) as used:
        g.allow(0.5, "sess-b")  # 1.5 would pass the 1.2 daily cap
    assert used.value.reason == BUDGET_USED
    g.allow(0.2, "sess-b")  # the refused request gave its rate-limit slot back
    g.settle(t1, 0.1)
    g.settle(t2, 0.0)
    assert counter.spent(DAY) == 300_000
    assert g.remaining_today() == pytest.approx(0.9)
    clock.t += 61
    g.allow(0.5, SESSION)  # the window moved on


def test_file_counter_persists_and_never_passes_the_cap_under_concurrency(tmp_path):
    path = tmp_path / "spend.json"
    a, b = spend.FileSpendCounter(path), spend.FileSpendCounter(path)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(c: spend.FileSpendCounter) -> None:
        for _ in range(10):
            ok = c.reserve(DAY, 70_000, 1_000_000)
            with lock:
                results.append(ok)

    threads = [threading.Thread(target=worker, args=(c,)) for c in (a, b, a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 14 and spend.FileSpendCounter(path).spent(DAY) == 980_000
    a.settle(DAY, 70_000, 10_000)
    assert spend.FileSpendCounter(path).spent(DAY) == 920_000  # a restart reads it back
    a.settle(DAY, 5_000_000, 0)
    assert b.spent(DAY) == 0  # never below zero
    with pytest.raises(ValueError):
        a.reserve(DAY, -1, 10)


def test_kv_counter_reserves_atomically_and_gives_back_past_the_cap():
    kv = MemoryKV()
    counter = spend.KVSpendCounter(kv)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(25):
            ok = counter.reserve(DAY, 30_000, 1_000_000)
            with lock:
                results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 33 and counter.spent(DAY) == 990_000
    assert kv.expiry == {f"ca-demo-spend:{DAY}": spend.KEY_TTL_S}
    counter.settle(DAY, 30_000, 5_000)
    assert counter.spent(DAY) == 965_000
    counter.settle(DAY, 2_000_000, 0)
    assert counter.spent(DAY) == 0
    # the counter is persistent: a new process (a new counter on the same store) reads it
    assert spend.KVSpendCounter(kv).spent(DAY) == 0


@pytest.mark.safety
def test_rest_kv_speaks_redis_over_rest_and_never_shows_its_token():
    sent: list[tuple[str, dict[str, str], Any]] = []

    def post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Any:
        sent.append((url, headers, json.loads(body)))
        cmd = json.loads(body)
        return {"result": 42 if cmd[0] == "INCRBY" else ("7" if cmd[0] == "GET" else 1)}

    kv = spend.RestKV("https://kv.example.test/", "tok-SECRET-1", post=post)
    assert kv.incrby("k", 5) == 42 and kv.get("k") == 7
    kv.expire("k", 60)
    assert [b for _, _, b in sent] == [["INCRBY", "k", 5], ["GET", "k"], ["EXPIRE", "k", 60]]
    assert sent[0][0] == "https://kv.example.test"
    assert sent[0][1]["Authorization"] == "Bearer tok-SECRET-1"
    assert "tok-SECRET-1" not in repr(kv)

    def broken(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Any:
        raise OSError("connect to https://kv.example.test with tok-SECRET-1 failed")

    with pytest.raises(spend.KVError) as err:
        spend.RestKV("https://kv.example.test", "tok-SECRET-1", post=broken).incrby("k", 1)
    assert "tok-SECRET-1" not in str(err.value)
    with pytest.raises(spend.KVError):
        spend.RestKV("https://kv.example.test", "t", post=lambda *a: {"error": "x"}).get("k")
    with pytest.raises(ValueError):
        spend.RestKV("http://kv.example.test", "t")  # never in clear text


@pytest.mark.safety
def test_a_spend_store_outage_refuses_readably_and_returns_the_rate_limit_slot(caplog):
    alerts: list = []
    g = guard_with(DownCounter(), config=GuardConfig(rate_limit=1), alerts=alerts)
    for _ in range(3):  # the slot is given back every time, so the reason never changes
        with pytest.raises(GuardRefused) as err:
            g.allow(0.5, SESSION)
        assert err.value.reason == STORE_DOWN
    assert "hunter2" not in caplog.text and "10.0.0.1" not in caplog.text
    assert alerts == [("spend_store", "ConnectionError")] * 3
    assert g.remaining_today() is None
    g.settle(g_ticket := SimpleNamespace(day=DAY, hold_micro=1, own_key=False), 0.0)  # type: ignore[arg-type]
    assert g_ticket.hold_micro == 1 and alerts[-1] == ("spend_settle", "ConnectionError")
    kv = MemoryKV()
    kv.down = True
    with pytest.raises(GuardRefused) as err:
        guard_with(spend.KVSpendCounter(kv)).allow(0.5, SESSION)
    assert err.value.reason == STORE_DOWN


def test_one_cost_number_the_hold_reserved_is_the_executors_cap_and_settles_to_its_count(
        tmp_path):  # fmt: skip
    rig = Rig(tmp_path)
    rig.unlock()
    seen: list[float] = []
    real_reserve = rig.counter.reserve

    def reserve(day: str, amount: int, cap: int) -> bool:
        seen.append(amount / spend.MICRO)
        return real_reserve(day, amount, cap)

    rig.counter.reserve = reserve  # type: ignore[method-assign]
    turn = rig.demo.ask(SESSION, QUESTION, PID)
    assert turn.answer.status == "answered"
    assert seen == [rig.guard.config.request_hold_usd]  # the guard held exactly the hold
    # no retries: the executor counted exactly what the calls cost, and the counter holds it
    assert abs(rig.counter.spent(DAY) - spend.to_micro(turn.answer.cost_usd)) <= 1
    assert 0 < turn.answer.cost_usd <= rig.guard.config.request_hold_usd


def request(text: str = "q", *, model: str = s.LUNA, search: bool = False,
            out: int = 512) -> ResponsesRequest:  # fmt: skip
    return ResponsesRequest(purpose="route", model=model, effort="low",
                            instructions=RouteStep.ROUTE_RULES,
                            input=(InputItem(role="user", content=text),),
                            vector_store_ids=("vs_1",) if search else (),
                            max_output_tokens=out)  # fmt: skip


def test_the_executor_refuses_a_call_past_the_hold_and_the_turn_errors_with_nothing_spent(
        tmp_path):  # fmt: skip
    rig = Rig(tmp_path, config=GuardConfig(request_hold_usd=0.01, daily_cap_usd=1.0))
    rig.unlock()
    turn = rig.demo.ask(SESSION, QUESTION, PID)
    assert turn.answer.status == "error"  # the answer call's bound is over a cent: refused
    assert rig.owner_client.steps() == ["route"]  # only the cheap route call ever ran
    assert rig.counter.spent(DAY) <= spend.to_micro(0.01)
    capped = CappedSource(Live(lambda r: pytest.fail("called")), 0.000001, PRICES)
    with pytest.raises(LLMError) as err:
        capped.call("r-0000000000000000", request())
    assert err.value.kind == "config" and capped.refused == 1 and capped.total_usd == 0


def test_failed_and_retried_calls_are_counted_at_their_bound_not_free():
    single = call_bound_usd(request(), PRICES)
    rsp, _ = scripted(route=s.down())
    capped = CappedSource(Live(rsp), 1.0, PRICES)
    with pytest.raises(LLMError):
        capped.call("r-0000000000000000", request())
    assert capped.total_usd == pytest.approx(single * MAX_ATTEMPTS)  # three failed attempts
    rsp, _ = scripted(route=[s.status_error(500), s.route("course")])
    capped = CappedSource(Live(rsp), 1.0, PRICES)
    result = capped.call("r-0000000000000000", request())
    assert result.attempts == 2
    assert capped.total_usd == pytest.approx(result.cost_usd + single)
    # a call that is still running counts its whole worst case
    started, release = threading.Event(), threading.Event()

    def slow(r: ResponsesRequest) -> Any:
        started.set()
        release.wait(5)
        raise LLMError("transport", "timed out", 1)

    capped = CappedSource(Live(slow), 1.0, PRICES)

    def run() -> None:
        with pytest.raises(LLMError):
            capped.call("r-0000000000000000", request())

    t = threading.Thread(target=run)
    t.start()
    started.wait(5)
    assert capped.total_usd == pytest.approx(single * MAX_ATTEMPTS)
    release.set()
    t.join()
    assert capped.total_usd == pytest.approx(single)


def test_non_ascii_text_is_bounded_by_bytes_and_search_adds_its_chunks():
    ascii_ = call_bound_usd(request("a" * 300), PRICES)
    devanagari = call_bound_usd(request("क" * 300), PRICES)  # three bytes a character
    assert devanagari > ascii_
    assert call_bound_usd(request(model=s.SOL, search=True), PRICES) > call_bound_usd(
        request(model=s.SOL), PRICES) + 0.04  # fmt: skip


@pytest.mark.golden
def test_every_golden_request_fits_under_the_default_hold_and_replays_unchanged():
    fx = golden.load()
    hold = GuardConfig().request_hold_usd
    for case in fx.cases:
        replay = Replay(case.recordings)
        capped = CappedSource(replay, hold, PRICES)
        got = golden.run_case(fx, case, capped)
        replay.check_used(case.request_id)
        assert got == case.expected, case.id
        assert capped.refused == 0 and capped.total_usd <= hold
        assert capped.total_usd >= got.cost_usd - 1e-12  # never less than what was spent


def test_a_visitors_own_key_holds_nothing_but_meets_passkey_cap_and_rate_limit(tmp_path):
    rig = Rig(tmp_path, config=GuardConfig(rate_limit=1))
    with pytest.raises(GuardRefused, match="passkey"):
        rig.demo.ask(SESSION, QUESTION, PID, own_key=VISITOR_KEY)
    assert rig.keys_seen == []
    rig.unlock()
    turn = rig.demo.ask(SESSION, QUESTION, PID, own_key=VISITOR_KEY)
    assert turn.answer.status == "answered" and rig.keys_seen == [VISITOR_KEY]
    assert rig.counter.spent(DAY) == 0 and rig.owner_client.calls == []
    with pytest.raises(GuardRefused, match="Rate limit"):
        rig.demo.ask(SESSION, QUESTION, PID, own_key=VISITOR_KEY)
    with pytest.raises(GuardRefused, match="per-request"):
        rig.guard.allow(5.0, SESSION, own_key=True)


def test_a_handed_over_live_turn_is_queued_for_the_teaching_team(tmp_path):
    rig = Rig(tmp_path, owner={"route": [s.route("admin")], "answer": [s.cited("d", S01)]})
    rig.unlock()
    turn = rig.demo.ask(SESSION, "Can I move my deadline?", PID)
    assert turn.answer.status == "handed_over"
    queue = team_queue(rig.store, viewer="instructor-1")
    assert [q.question_id for q in queue] == [turn.answer.question_id]


class FaultyStore(Store):
    """A store whose deletion bookkeeping fails inside `_settle`, with text in the error."""

    def __init__(self, mode: str) -> None:
        super().__init__(":memory:")
        self.mode = mode
        self.reads = 0

    def deletion_keys(self, pid: str) -> frozenset[str]:
        if self.mode == "keys":
            raise RuntimeError("my father is in hospital")
        self.reads += 1
        return frozenset() if self.reads == 1 else frozenset({"k-new"})

    def deletions_by_key(self, keys: frozenset[str]) -> list[Any]:
        return [SimpleNamespace(conversation_id=None, covers=(), requested_at=dt.datetime(
            2026, 11, 20, 9, tzinfo=dt.UTC))]  # fmt: skip

    def reopen_traces(self, request: Any, *, at: dt.datetime) -> Any:
        raise RuntimeError("my father is in hospital")


class BrokenSink(Sink):
    def delete_pid(self, pid: str, conversation_ids: Any = None) -> int:
        raise OSError("disk full at /home/learner/my father is in hospital")


@pytest.mark.privacy
@pytest.mark.parametrize("mode", ["keys", "settle"])
def test_any_settle_exception_is_audited_and_alerted_without_text(mode, caplog):
    store = FaultyStore(mode)
    rsp, _ = scripted(**happy())
    alerts: list[tuple[str, str]] = []
    a = Assistant(CONFIG, CORPUS, ASSESSMENTS, store, rsp, sink=BrokenSink(), now=Clock(),
                  timer=lambda: 0.0, new_id=ids(),
                  alert=lambda st, k: alerts.append((st, k)))  # fmt: skip
    with caplog.at_level(logging.WARNING):
        answer = a.ask(QUESTION, PID)
    assert answer.status == "answered"  # the turn's answer is never hidden
    if mode == "keys":
        assert alerts == [("deletion_keys", "RuntimeError")]
    else:
        assert alerts == [("delete_traces", "OSError"), ("reopen_traces", "RuntimeError")]
    events = [e for e in store.audit_events() if e.action == "settle_failed"]
    assert len(events) == len(alerts) and {e.target for e in events} == {PID}
    assert {e.actor for e in events} == {"system-1"}
    assert "hospital" not in caplog.text and "hospital" not in json.dumps(
        [e.model_dump(mode="json") for e in store.audit_events()])  # fmt: skip
    # an alert hook that fails itself still never hides the answer
    a.alert = lambda st, k: (_ for _ in ()).throw(RuntimeError("hook"))
    a.source = Live(scripted(**happy())[0])
    assert a.ask(QUESTION, PID).status == "answered"


# ======================= RPL-03 separate demo project =======================


def test_operator_steps_name_the_separate_project_its_hard_limit_and_the_secrets():
    text = SPEC.read_text(encoding="utf-8")
    steps = text.split("### Operator steps (RPL-03")[1].split("## Rules")[0]
    for needed in ("separate project", "hard limit", "project API key", "OPENAI_API_KEY",
                   "DEMO_PASSKEY_HASH", "DEMO_KV_REST_URL", "DEMO_KV_REST_TOKEN"):  # fmt: skip
        assert needed in steps, needed
    assert passkey.SECRET_NAME == "DEMO_PASSKEY_HASH"
    assert (spend.KV_URL, spend.KV_TOKEN) == ("DEMO_KV_REST_URL", "DEMO_KV_REST_TOKEN")


@pytest.mark.safety
def test_secrets_come_only_from_the_environment_or_the_host_secret_store():
    env = {"DEMO_PASSKEY_HASH": "  " + HASH + "\n"}
    assert passkey.read_secret("DEMO_PASSKEY_HASH", env=env) == HASH
    assert passkey.read_secret("DEMO_PASSKEY_HASH", env={},
                               secrets_store={"DEMO_PASSKEY_HASH": HASH}) == HASH  # fmt: skip
    assert passkey.read_secret("DEMO_PASSKEY_HASH", env={"DEMO_PASSKEY_HASH": " "}) is None
    assert passkey.read_secret("DEMO_PASSKEY_HASH", env={}, secrets_store={}) is None
    gate = passkey.PasskeyGate(passkey.env_hash(env={}), clock=Tick())
    with pytest.raises(GuardRefused, match="closed"):
        gate.unlock(CODE, session=SESSION)  # no secret set: the gate is closed
    assert not (ROOT / ".streamlit" / "secrets.toml").exists()


# ======================= RPL-04 passkey gate =======================


def test_passkeys_are_salted_pbkdf2_hashes_compared_in_constant_time(monkeypatch):
    a = passkey.hash_passcode(CODE, iterations=FAST)
    b = passkey.hash_passcode(CODE, iterations=FAST)
    assert a != b and a.startswith("pbkdf2_sha256$100000$") and CODE not in a
    assert passkey.verify_passcode(CODE, a) and passkey.verify_passcode(CODE, b)
    assert not passkey.verify_passcode(OTHER_CODE, a)
    assert passkey.hash_passcode(CODE, salt=SALT, iterations=FAST) == HASH  # deterministic
    calls: list[int] = []
    real = hmac.compare_digest

    def spy(x: Any, y: Any) -> bool:
        calls.append(1)
        return real(x, y)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    passkey.verify_passcode(OTHER_CODE, a)
    passkey.verify_passcode("not-digits", a)  # derived and compared anyway, then refused
    assert len(calls) == 2
    for bad in ("12345", "1234567890123", "12a456", "", " 123456"):
        with pytest.raises(ValueError):
            passkey.hash_passcode(bad)
    assert passkey.hash_passcode(CODE).startswith("pbkdf2_sha256$600000$")


@pytest.mark.safety
def test_a_malformed_or_missing_hash_fails_closed():
    for bad in ("", "plain-4815162342", "pbkdf2_sha256$100000$c2FsdA==$AAAA",
                HASH.replace("100000", "1000"), "md5$1$a$b"):  # fmt: skip
        with pytest.raises(PasskeyConfigError):
            passkey.verify_passcode(CODE, bad)
        gate = passkey.PasskeyGate(Secret(bad), clock=Tick())
        with pytest.raises(GuardRefused, match="closed"):
            gate.unlock(CODE, session=SESSION)
        with pytest.raises(GuardRefused, match="closed"):
            gate.require(SESSION)

    def raising() -> str:
        raise RuntimeError("secret store down")

    with pytest.raises(GuardRefused, match="closed"):
        passkey.PasskeyGate(raising).require(SESSION)


def test_every_live_and_replay_route_requires_the_passkey_even_with_an_own_key(tmp_path):
    rig = Rig(tmp_path)
    fx, _ = build_from_golden()
    rig.demo.cache = demo_cache.DemoCache(fx)
    for call in (lambda: rig.demo.ask(SESSION, QUESTION, PID),
                 lambda: rig.demo.ask(SESSION, QUESTION, PID, own_key=VISITOR_KEY),
                 lambda: rig.demo.replay(SESSION, "demo-01"),
                 lambda: rig.guard.allow(0.5, SESSION),
                 lambda: rig.guard.allow(0.5, SESSION, own_key=True)):  # fmt: skip
        with pytest.raises(GuardRefused, match="passkey"):
            call()
    assert rig.owner_client.calls == [] and rig.keys_seen == [] and rig.counter.spent(DAY) == 0
    with pytest.raises(GuardRefused, match="Wrong"):
        rig.gate.unlock(OTHER_CODE, session=SESSION, ip=IP)
    with pytest.raises(GuardRefused, match="passkey"):
        rig.demo.replay(SESSION, "demo-01")
    rig.unlock()
    assert rig.demo.replay(SESSION, "demo-01").status == "answered"
    assert rig.demo.ask(SESSION, QUESTION, PID, own_key=VISITOR_KEY).answer.status == "answered"
    with pytest.raises(GuardRefused, match="passkey"):
        rig.demo.replay("sess-other", "demo-01")  # unlocking is per session
    rig.gate.lock_session(SESSION)
    with pytest.raises(GuardRefused, match="passkey"):
        rig.demo.replay(SESSION, "demo-01")


def test_changing_the_hash_revokes_every_session_and_removing_it_closes_the_gate(tmp_path):
    rig = Rig(tmp_path)
    rig.unlock()
    rig.unlock("sess-b")
    rig.gate.require(SESSION)
    rig.secret.value = passkey.hash_passcode(OTHER_CODE, iterations=FAST)  # owner rotates it
    for sess in (SESSION, "sess-b"):
        with pytest.raises(GuardRefused, match="passkey"):
            rig.gate.require(sess)
        with pytest.raises(GuardRefused, match="passkey"):
            rig.demo.ask(sess, QUESTION, PID, own_key=VISITOR_KEY)
    with pytest.raises(GuardRefused, match="Wrong"):
        rig.gate.unlock(CODE, session=SESSION, ip=IP)  # the old code no longer works
    rig.gate.unlock(OTHER_CODE, session=SESSION, ip=IP)
    rig.gate.require(SESSION)
    rig.secret.value = None  # removed: closed for everyone, unlocked or not
    with pytest.raises(GuardRefused, match="closed"):
        rig.gate.require(SESSION)
    with pytest.raises(GuardRefused, match="closed"):
        rig.gate.unlock(OTHER_CODE, session=SESSION, ip=IP)
    assert rig.keys_seen == []


@pytest.mark.safety
def test_failed_attempts_lock_the_session_and_the_ip_even_for_the_right_code():
    clock = Tick()
    gate = passkey.PasskeyGate(Secret(), clock=clock, max_failures=5, window_s=900)
    for _ in range(5):
        with pytest.raises(GuardRefused, match="Wrong"):
            gate.unlock(OTHER_CODE, session=SESSION, ip=IP)
    with pytest.raises(GuardRefused, match="Too many"):
        gate.unlock(CODE, session=SESSION, ip="198.51.100.1")  # the session is locked
    with pytest.raises(GuardRefused, match="Too many"):
        gate.unlock(CODE, session="fresh-session", ip=IP)  # and so is the IP
    gate.unlock(CODE, session="fresh-session", ip="198.51.100.1")  # others are not
    clock.t += 901
    gate.unlock(CODE, session=SESSION, ip=IP)  # the window passed
    gate.require(SESSION)
    # many sessions from one IP: the IP limit still stops guessing
    gate2 = passkey.PasskeyGate(Secret(), clock=Tick(), max_failures=3)
    for i in range(3):
        with pytest.raises(GuardRefused, match="Wrong"):
            gate2.unlock(OTHER_CODE, session=f"s{i}", ip=IP)
    with pytest.raises(GuardRefused, match="Too many"):
        gate2.unlock(CODE, session="s9", ip=IP)
    # the failure memory is bounded
    gate3 = passkey.PasskeyGate(Secret(), clock=Tick(), max_keys=50)
    for i in range(200):
        with pytest.raises(GuardRefused):
            gate3.unlock(OTHER_CODE, session=f"x{i}")
    assert len(gate3._failures) <= 50


@pytest.mark.safety
def test_passkey_hash_cli_reads_without_echo_and_prints_only_the_hash(monkeypatch):
    import getpass

    prompts: list[str] = []
    answers = iter([CODE, CODE])

    def fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert cli.main(["passkey-hash"]) == cli.EXIT_OK
    line = out.getvalue().strip()
    assert len(prompts) == 2 and "\n" not in line
    assert passkey.verify_passcode(CODE, line) and CODE not in out.getvalue() + err.getvalue()
    assert "DEMO_PASSKEY_HASH" in err.getvalue()
    for first, second in ((CODE, OTHER_CODE), ("12ab56", "12ab56"), ("123", "123")):
        answers = iter([first, second])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            assert cli.main(["passkey-hash"]) == cli.EXIT_CONFIG
        assert out.getvalue() == "" and first not in err.getvalue()


@pytest.mark.privacy
def test_no_passcode_hash_or_visitor_key_reaches_a_log_error_trace_or_repr(tmp_path, caplog):
    rig = Rig(tmp_path)
    errors: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for code in (OTHER_CODE, "99999999"):
            try:
                rig.gate.unlock(code, session=SESSION, ip=IP)
            except GuardRefused as exc:
                errors.append(str(exc))
        rig.unlock()
        turn = rig.demo.ask(SESSION, QUESTION, PID, own_key=VISITOR_KEY)
        rig.demo.ask(SESSION, QUESTION, PID)
    salt_b64 = HASH.split("$")[2]
    dumped = json.dumps(rig.sink.records) + caplog.text + " ".join(errors) + repr(rig.gate) + (
        repr(rig.demo) + repr(rig.guard) + turn.model_dump_json()
        + json.dumps([e.model_dump(mode="json") for e in rig.store.audit_events()])
        + (tmp_path / "spend.json").read_text(encoding="utf-8"))  # fmt: skip
    for secret in (CODE, OTHER_CODE, "99999999", HASH, salt_b64, VISITOR_KEY, QUESTION):
        assert secret not in dumped, secret
    assert "demo live: mode=own_key status=answered" in caplog.text  # aggregate only


# ---------- verifier pass 1 regressions ----------


@pytest.mark.safety
def test_simultaneous_wrong_guesses_never_pass_the_lockout():
    import threading

    gate = passkey.PasskeyGate(Secret(), clock=Tick(), max_failures=5)
    checked = []

    def guess():
        try:
            gate.unlock("000000", session="s", ip="1.1.1.1")
        except GuardRefused as exc:
            checked.append(str(exc))

    threads = [threading.Thread(target=guess) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for c in checked if c == passkey.WRONG) <= 5


@pytest.mark.safety
def test_rotating_sessions_with_no_ip_hit_the_global_cap():
    gate = passkey.PasskeyGate(Secret(), clock=Tick(), global_max_failures=10)
    wrong = 0
    for i in range(40):
        try:
            gate.unlock("000000", session=f"s{i}", ip=None)
        except GuardRefused as exc:
            wrong += str(exc) == passkey.WRONG
    assert wrong <= 10


# ---------- verifier pass 2 regressions ----------


@pytest.mark.safety
def test_a_flood_without_ip_never_locks_out_a_visitor_with_an_ip():
    gate = passkey.PasskeyGate(Secret(), clock=Tick(), global_max_failures=10)
    for i in range(40):
        with contextlib.suppress(GuardRefused):
            gate.unlock("000000", session=f"flood{i}", ip=None)
    assert not gate.locked("owner", "10.0.0.7")
