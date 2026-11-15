"""N1.2: the OpenAI wrapper, cost, ledger and trace."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import pytest

from course_assistant.llm import (
    InputItem,
    LLMError,
    Prices,
    ResponsesRequest,
    respond,
)
from course_assistant.llm.cost import call_cost, load_prices
from course_assistant.llm.ledger import Ledger
from course_assistant.llm.trace import TRACE_KEYS, JsonlSink, strip_text
from tests.llm.fakes import FakeClient, connection_error, make_response, status_error

PRICES = load_prices()


def req(**kw: Any) -> ResponsesRequest:
    base: dict[str, Any] = dict(
        purpose="answer",
        model="gpt-6.1-sol",
        effort="low",
        instructions="Answer only from the materials.",
        input=(InputItem(role="user", content="Why both offline and online evals?"),),
        vector_store_ids=("vs_sample",),
        max_num_results=8,
        filters={"type": "eq", "key": "session", "value": "s02"},
    )
    base.update(kw)
    return ResponsesRequest(**base)


def run(outcomes, request=None, **kw):
    client = FakeClient(outcomes)
    result = respond(request or req(), client=client, prices=PRICES, sleep=lambda s: None, **kw)
    return result, client


# ---------- OAI-01 ----------


def test_always_store_false_and_explicit_effort():
    _, client = run([make_response()])
    call = client.responses.calls[0]
    assert call["store"] is False
    assert call["reasoning"] == {"effort": "low"}
    assert call["include"] == ["file_search_call.results"]
    tool = call["tools"][0]
    assert tool["type"] == "file_search" and tool["vector_store_ids"] == ["vs_sample"]
    assert tool["filters"] == {"type": "eq", "key": "session", "value": "s02"}
    assert call["input"] == [{"role": "user", "content": "Why both offline and online evals?"}]


def test_no_file_search_means_no_tools_or_include():
    _, client = run([make_response(file_search=False, citations=())],
                    req(vector_store_ids=(), filters=None, purpose="route"))  # fmt: skip
    call = client.responses.calls[0]
    assert "tools" not in call and "include" not in call and call["store"] is False


def test_parses_text_citations_hits_usage_and_snapshot():
    result, _ = run([make_response()])
    assert result.text.startswith("Offline evals")
    assert [(c.file_id, c.index) for c in result.citations] == [("file-s02-09", 30)]
    assert [(h.file_id, h.score) for h in result.hits] == [
        ("file-s02-09", 0.82),
        ("file-s02-p3", 0.61),
    ]
    assert result.hits[0].attributes == {"session": "s02"}
    assert (result.tokens_in, result.tokens_cached, result.tokens_out) == (4000, 1200, 700)
    assert result.tokens_reasoning == 300 and result.file_search_calls == 1
    assert result.model_snapshot == "gpt-6.1-sol-2026-10-01" and not result.refusal


def test_structured_output_sends_a_strict_json_schema():
    schema = {"type": "object", "properties": {"category": {"type": "string"}},
              "required": ["category"], "additionalProperties": False}  # fmt: skip
    _, client = run([make_response(text='{"category": "course"}', citations=(), file_search=False)],
                    req(vector_store_ids=(), filters=None, json_schema=schema))  # fmt: skip
    fmt = client.responses.calls[0]["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["schema"] == schema


@pytest.mark.contract
def test_store_cannot_be_turned_on():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        req(store=True)


# ---------- OAI-02 ----------


@pytest.mark.parametrize("err", [429, 500, 503, 408, 409, "conn"])
def test_transient_errors_are_retried_then_succeed(err):
    first = connection_error() if err == "conn" else status_error(err)
    result, client = run([first, make_response()])
    assert len(client.responses.calls) == 2 and result.text


def test_retries_stop_at_three_attempts():
    with pytest.raises(LLMError) as exc:
        run([status_error(429)] * 5)
    assert exc.value.kind == "transport" and exc.value.attempts == 3


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_config_errors_are_never_retried(code):
    client = FakeClient([status_error(code), make_response()])
    with pytest.raises(LLMError) as exc:
        respond(req(), client=client, prices=PRICES, sleep=lambda s: None)
    assert exc.value.kind == "config" and len(client.responses.calls) == 1


def test_refusal_is_reported_never_retried():
    result, client = run([make_response(refusal="I can't help with that.")])
    assert result.refusal and result.text == "" and len(client.responses.calls) == 1


@pytest.mark.safety
def test_error_text_never_contains_the_key():
    for code in (401, 429):
        with pytest.raises(LLMError) as exc:
            run([status_error(code)] * 3)
        assert "sk-" not in str(exc.value) and "SECRET" not in str(exc.value)


def test_deadline_stops_retrying():
    clock = iter([0.0, 0.0, 30.0, 30.0, 30.0])
    client = FakeClient([status_error(503)] * 3)
    with pytest.raises(LLMError) as exc:
        respond(req(), client=client, prices=PRICES, sleep=lambda s: None,
                clock=lambda: next(clock), deadline_s=20)  # fmt: skip
    assert exc.value.kind == "transport" and len(client.responses.calls) < 3


# ---------- OAI-03 ----------


def test_cost_counts_uncached_cached_output_and_file_search():
    p = PRICES.for_model("gpt-6.1-sol-2026-10-01")
    cost = call_cost(p, tokens_in=4000, tokens_cached=1200, tokens_out=700, file_search_calls=1,
                     file_search_per_1k=PRICES.file_search_per_1k)  # fmt: skip
    expected = (2800 * 2.00 + 1200 * 0.10 + 700 * 10.00) / 1e6 + 2.50 / 1000
    assert math.isclose(cost, expected, rel_tol=1e-12)
    result, _ = run([make_response()])
    assert math.isclose(result.cost_usd, expected, rel_tol=1e-12)


def test_reasoning_tokens_are_counted_once_inside_output():
    p = PRICES.for_model("gpt-6-luna")
    a = call_cost(p, tokens_in=100, tokens_cached=0, tokens_out=50, file_search_calls=0,
                  file_search_per_1k=2.5)  # fmt: skip
    assert math.isclose(a, (100 * 0.10 + 50 * 0.50) / 1e6)


@pytest.mark.parametrize("bad", ["gpt-unknown", "claude-opus-5-5"])
def test_unknown_models_are_refused(bad):
    with pytest.raises(KeyError):
        PRICES.for_model(bad)


@pytest.mark.parametrize(
    "kw",
    [{"tokens_in": -1}, {"tokens_cached": 5000}, {"tokens_out": -3}, {"file_search_calls": -1}],
)
def test_impossible_token_counts_are_refused(kw):
    base: dict[str, Any] = dict(tokens_in=4000, tokens_cached=0, tokens_out=10, file_search_calls=0,
                file_search_per_1k=2.5)  # fmt: skip
    base.update(kw)
    with pytest.raises(ValueError):
        call_cost(PRICES.for_model("gpt-6-luna"), **base)


def test_prices_file_names_its_source_and_date():
    for name, p in PRICES.models.items():
        assert p.checked_on and p.source, name
    assert PRICES.file_search_per_1k == 2.50


# ---------- OAI-04 ----------


def test_every_call_appends_one_text_free_ledger_line(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", monthly_budget_usd=100)
    run([make_response(), make_response()], ledger=ledger)
    run([make_response()], ledger=ledger)
    lines = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["purpose"] == "answer" and row["model"].startswith("gpt-6.1-sol")
    assert "Offline" not in lines[0] and "offline" not in lines[0]


def test_month_total_and_budget_warning(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", monthly_budget_usd=0.03)
    run([make_response()], ledger=ledger)
    assert ledger.warnings == []
    run([make_response()], ledger=ledger)
    assert math.isclose(ledger.month_total(), 2 * run([make_response()])[0].cost_usd)
    assert ledger.warnings and "80%" in ledger.warnings[-1]


# ---------- OAI-05 ----------


@pytest.mark.privacy
def test_learner_text_never_reaches_the_trace(tmp_path):
    secret = "my father is in hospital, can I get an extension"
    sink = JsonlSink(tmp_path / "trace.jsonl")
    run(
        [make_response(text=secret)],
        request=req(input=(InputItem(role="user", content=secret),), instructions=secret),
        sink=sink,
    )
    raw = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert "father" not in raw and "hospital" not in raw and raw.strip()
    assert set(json.loads(raw.splitlines()[0])) <= TRACE_KEYS


@pytest.mark.privacy
def test_strip_text_is_an_allowlist():
    rid = "r-0123456789abcdef"
    rec = {"request_id": rid, "question": "secret", "output": "secret", "anything_new": "secret",
           "tokens_in": 1, "cost_usd": 0.1}  # fmt: skip
    assert strip_text(rec) == {"request_id": rid, "tokens_in": 1, "cost_usd": 0.1}


# ---------- OAI-06 ----------


@pytest.mark.contract
def test_fakes_are_real_sdk_objects():
    from openai.types.responses import Response

    resp = make_response()
    assert isinstance(resp, Response)
    assert resp.usage is not None and resp.usage.output_tokens_details.reasoning_tokens == 300


def test_prices_type_is_exported():
    assert Prices is type(PRICES)


def test_cache_writes_are_priced_separately():
    p = PRICES.for_model("gpt-6.1-sol")
    cost = call_cost(p, tokens_in=1000, tokens_cached=200, tokens_out=0, file_search_calls=0,
                     file_search_per_1k=2.5, tokens_cache_write=300)  # fmt: skip
    assert math.isclose(cost, (500 * 2.00 + 200 * 0.10 + 300 * 2.00) / 1e6)


# ---------- verifier pass 1 regressions ----------


def _commentary_response():
    from openai.types.responses import Response

    base = make_response(text="Evals run offline.", citations=(("file-x", 18),))
    data = base.model_dump()
    commentary = {
        "id": "msg_0", "type": "message", "role": "assistant", "status": "completed",
        "phase": "commentary",
        "content": [{"type": "output_text", "text": "Let me search the slides.",
                     "annotations": [{"type": "file_citation", "file_id": "file-x",
                                      "filename": "x", "index": 25}]}],
    }  # fmt: skip
    data["output"].insert(0, commentary)
    two = data["output"][-1]["content"][0]
    shifted = [{**two["annotations"][0], "index": 5}]
    data["output"][-1]["content"].append({**two, "text": " Then online.", "annotations": shifted})
    return Response.model_validate(data)


def test_commentary_is_dropped_and_citation_indices_are_shifted():
    result, _ = run([_commentary_response()])
    assert result.text == "Evals run offline. Then online."
    assert [c.index for c in result.citations] == [18, 18 + 5]
    assert result.text[23:] == " online."  # same place as index 5 in its own part


def test_refusal_carries_no_citations():
    resp = make_response()
    data = resp.model_dump()
    data["output"][-1]["content"].append({"type": "refusal", "refusal": "no"})
    from openai.types.responses import Response

    result, _ = run([Response.model_validate(data)])
    assert result.refusal and result.text == "" and result.citations == ()


def test_sdk_retries_are_off_so_three_attempts_mean_three_requests():
    import httpx2
    import openai

    count = {"n": 0}

    def handler(request):
        count["n"] += 1
        return httpx2.Response(429, json={"error": {"message": "slow down"}})

    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    client = openai.OpenAI(api_key="test", base_url="https://example.invalid/v1", http_client=http)

    def per_attempt(timeout):
        return httpx2.Client(transport=httpx2.MockTransport(handler), timeout=timeout)

    with pytest.raises(LLMError) as exc:
        respond(req(), client=client, prices=PRICES, sleep=lambda s: None, http_factory=per_attempt)
    assert exc.value.attempts == 3 and count["n"] == 3


def test_each_attempt_gets_only_the_time_left():
    seen = []

    class Recording(FakeClient):
        def with_options(self, **kw):
            seen.append(kw)
            return self

    t = iter([0.0, 0.0, 12.0, 12.0, 12.0, 12.0])
    client = Recording([status_error(503), make_response()])
    respond(req(), client=client, prices=PRICES, sleep=lambda s: None, clock=lambda: next(t),
            deadline_s=20)  # fmt: skip
    assert seen[0] == {"max_retries": 0, "timeout": 20.0}
    assert seen[1]["max_retries"] == 0 and seen[1]["timeout"] == 8.0


def test_unpriced_snapshot_is_an_llm_error_and_still_ledgered(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", monthly_budget_usd=100)
    with pytest.raises(LLMError) as exc:
        run([make_response(model="gpt-6.1-sol-mini")], ledger=ledger)
    assert exc.value.kind == "config"
    row = json.loads((tmp_path / "l.jsonl").read_text(encoding="utf-8"))
    assert row["priced"] is False and row["tokens_in"] == 4000


@pytest.mark.privacy
def test_any_sink_receives_only_stripped_records():
    got = []

    class Custom:
        def emit(self, record):
            got.append(record)

    secret = "my father is in hospital"
    run([make_response(text=secret)], sink=Custom())
    assert got and secret not in json.dumps(got) and set(got[0]) <= TRACE_KEYS


@pytest.mark.privacy
def test_allowlisted_keys_cannot_carry_sentences():
    rec = {"status": "my father is ill", "route": "course", "error": "transport",
           "request_id": "r-0123456789abcdef", "cost_usd": 0.1}  # fmt: skip
    assert strip_text(rec) == {"route": "course", "error": "transport",
                               "request_id": "r-0123456789abcdef", "cost_usd": 0.1}  # fmt: skip


@pytest.mark.parametrize("model", ["gpt-6.1-sol-2", "gpt-6.1-sol-9-turbo", "gpt-6.1-sol-mini"])
def test_only_dated_snapshots_share_a_price(model):
    with pytest.raises(KeyError):
        PRICES.for_model(model)
    assert PRICES.for_model("gpt-6.1-sol-2026-10-01") is PRICES.models["gpt-6.1-sol"]


def test_hits_without_scores_are_kept_not_dropped():
    from openai.types.responses import Response

    data = make_response().model_dump()
    data["output"][0]["results"][1]["score"] = None
    result, _ = run([Response.model_validate(data)])
    assert [h.score for h in result.hits] == [0.82, None]


@pytest.mark.privacy
def test_request_repr_hides_learner_text():
    r = req(input=(InputItem(role="user", content="my exam results were bad"),))
    assert "exam" not in repr(r) and "Answer only" not in repr(r)


def test_ledger_records_cache_writes_and_reasoning(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", monthly_budget_usd=100)
    run([make_response()], ledger=ledger)
    row = json.loads((tmp_path / "l.jsonl").read_text(encoding="utf-8"))
    assert row["tokens_reasoning"] == 300 and row["tokens_cache_write"] == 0 and row["priced"]


# ---------- verifier pass 2 regressions ----------


def test_a_slow_attempt_cannot_outlast_the_deadline():
    import time as _time

    class Slow(FakeClient):
        def __init__(self):
            super().__init__([])

        def with_options(self, **kw):
            return self

    slow = Slow()
    slow.responses.create = lambda **kw: _time.sleep(2.0) or make_response()  # type: ignore[method-assign]
    started = _time.monotonic()
    with pytest.raises(LLMError) as exc:
        respond(req(), client=slow, prices=PRICES, sleep=lambda s: None, deadline_s=0.3)
    assert exc.value.kind == "transport" and _time.monotonic() - started < 1.0


def test_a_corrupt_ledger_is_a_config_error(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_text('{"at": "2026-11-1', encoding="utf-8")
    with pytest.raises(LLMError) as exc:
        run([make_response()], ledger=Ledger(path, monthly_budget_usd=100))
    assert exc.value.kind == "config"


def test_an_unreadable_body_is_a_parse_error_not_retried():
    client = FakeClient([ValueError("Expecting value"), make_response()])
    with pytest.raises(LLMError) as exc:
        respond(req(), client=client, prices=PRICES, sleep=lambda s: None)
    assert exc.value.kind == "parse" and len(client.responses.calls) == 1


@pytest.mark.privacy
def test_one_word_answers_cannot_ride_in_enum_or_id_fields():
    rec = {"learner_pid": "Paris", "route": "photosynthesis", "status": "mitochondria",
           "error": "Napoleon", "model": "Napoleon", "tokens_in": 3}  # fmt: skip
    assert strip_text(rec) == {"tokens_in": 3}


def test_a_client_that_cannot_turn_off_retries_is_refused():
    class Bare:
        def __init__(self):
            self.responses = FakeClient([make_response()]).responses

    with pytest.raises(LLMError) as exc:
        respond(req(), client=Bare(), prices=PRICES, sleep=lambda s: None)
    assert exc.value.kind == "config"


def test_unpriced_ledger_line_keeps_reasoning_and_cache_writes(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", monthly_budget_usd=100)
    with pytest.raises(LLMError):
        run([make_response(model="gpt-6.1-sol-mini")], ledger=ledger)
    row = json.loads((tmp_path / "l.jsonl").read_text(encoding="utf-8"))
    assert row["tokens_reasoning"] == 300 and "tokens_cache_write" in row


# ---------- verifier pass 3 regressions ----------


def test_unpriced_call_with_a_corrupt_ledger_is_still_an_llm_error(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(LLMError):
        run([make_response(model="gpt-6.1-sol-mini")], ledger=Ledger(path, monthly_budget_usd=1))


@pytest.mark.privacy
@pytest.mark.parametrize("err", [OSError("disk Paris"), TypeError("Paris")])
def test_sink_failures_on_any_path_never_carry_their_text(err):
    class Broken:
        def emit(self, record):
            raise err

    for outcomes in ([make_response()], [status_error(503)] * 3):
        with pytest.raises(LLMError) as exc:
            run(outcomes, sink=Broken())
        assert "Paris" not in str(exc.value) and exc.value.kind == "config"


def test_abandoned_attempts_close_their_connection_and_do_not_pile_up():
    import threading
    import time as _time

    import httpx2
    import openai

    closed = []

    def slow(request):
        _time.sleep(5)
        return httpx2.Response(200, json={})

    class Tracking(httpx2.Client):
        def close(self):
            closed.append(True)
            super().close()

    client = openai.OpenAI(api_key="test", base_url="https://example.invalid/v1")
    before = threading.active_count()
    for _ in range(3):
        with pytest.raises(LLMError):
            respond(req(), client=client, prices=PRICES, sleep=lambda s: None, deadline_s=0.2,
                    http_factory=lambda t: Tracking(transport=httpx2.MockTransport(slow),
                                                    timeout=t))  # fmt: skip
    assert len(closed) == 3
    assert all(t.daemon for t in threading.enumerate() if t.name == "llm-attempt")
    assert threading.active_count() - before <= 3


@pytest.mark.privacy
def test_version_and_model_cannot_carry_words():
    rec = {"version": "paris", "model": "gpt-the-answer-is-paris", "cost_usd": float("nan"),
           "model_snapshot": "gpt-6.1-sol-2026-10-01"}  # fmt: skip
    assert strip_text(rec) == {"model_snapshot": "gpt-6.1-sol-2026-10-01"}


# ---------- verifier pass 4 regressions ----------


def test_a_deeply_nested_ledger_line_is_still_an_llm_error(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_text("[" * 100000 + "\n", encoding="utf-8")
    with pytest.raises(LLMError) as exc:
        run([make_response()], ledger=Ledger(path, monthly_budget_usd=10))
    assert exc.value.kind == "config"


@pytest.mark.privacy
def test_client_setup_failures_never_carry_their_text():
    class Bad(FakeClient):
        def with_options(self, **kw):
            raise TypeError("SINKTEXT")

    with pytest.raises(LLMError) as exc:
        respond(req(), client=Bad([make_response()]), prices=PRICES, sleep=lambda s: None)
    assert "SINKTEXT" not in str(exc.value) and exc.value.__context__ is None


def test_a_custom_http_client_needs_an_explicit_factory():
    import httpx2
    import openai

    custom = httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(500)))
    client = openai.OpenAI(api_key="t", base_url="https://example.invalid/v1", http_client=custom)
    with pytest.raises(LLMError) as exc:
        respond(req(), client=client, prices=PRICES, sleep=lambda s: None)
    assert exc.value.kind == "config" and "http_factory" in str(exc.value)


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), 0, -1])
def test_the_deadline_must_be_finite_and_positive(deadline):
    with pytest.raises(LLMError) as exc:
        respond(req(), client=FakeClient([make_response()]), prices=PRICES, deadline_s=deadline)
    assert exc.value.kind == "config"


@pytest.mark.privacy
def test_trace_patterns_refuse_words_posing_as_ids():
    rec = {"model": "gpt-5-zebrafish", "version": "decade", "at": "2026-11-15T9876543210"}
    assert strip_text(rec) == {}


# ---------- verifier pass 5 regressions ----------


@pytest.mark.privacy
def test_no_error_ever_chains_provider_text():
    from openai.types.responses import Response

    spaced = make_response().model_dump()
    spaced["output"][0]["results"][0]["file_id"] = "file with PROVIDERTEXT spaces"
    with pytest.raises(LLMError) as exc:
        run([make_response(model="gpt-9-PROVIDERTEXT")])
    assert exc.value.__context__ is None and exc.value.__cause__ is None
    assert "PROVIDERTEXT" not in str(exc.value)
    result, _ = run([Response.model_validate(spaced)])  # an odd but valid id simply passes
    assert result.hits[0].file_id.startswith("file with")


def test_negative_token_counts_are_a_parse_error():
    from openai.types.responses import Response

    data = make_response().model_dump()
    data["usage"]["output_tokens_details"]["reasoning_tokens"] = -1
    with pytest.raises(LLMError) as exc:
        run([Response.model_construct(**data)])
    assert exc.value.kind == "parse"


@pytest.mark.parametrize("deadline", [10**400, True, 1e300, "20"])
def test_absurd_deadlines_are_config_errors(deadline):
    with pytest.raises(LLMError) as exc:
        client = FakeClient([make_response()])
        respond(req(), client=client, prices=PRICES, deadline_s=deadline)  # type: ignore[arg-type]
    assert exc.value.kind == "config"


def test_a_held_ledger_lock_cannot_hold_the_call_past_its_deadline(tmp_path):
    import fcntl
    import time as _time

    path = tmp_path / "l.jsonl"
    path.write_text("", encoding="utf-8")
    holder = path.open("a", encoding="utf-8")
    fcntl.flock(holder, fcntl.LOCK_EX)
    started = _time.monotonic()
    with pytest.raises(LLMError) as exc:
        run([make_response()], ledger=Ledger(path, monthly_budget_usd=10), deadline_s=1.0)
    assert exc.value.kind == "config" and _time.monotonic() - started < 3.0
    holder.close()


def test_client_setup_oddities_are_config_errors():
    import openai

    class RaisingProperty(FakeClient):
        @property
        def with_options(self):  # type: ignore[override]
            raise RuntimeError("PROVIDERTEXT")

    with pytest.raises(LLMError) as exc:
        respond(req(), client=RaisingProperty([make_response()]), prices=PRICES)
    assert exc.value.kind == "config" and "PROVIDERTEXT" not in str(exc.value)
    real = openai.OpenAI(api_key="t", base_url="https://example.invalid/v1")
    with pytest.raises(LLMError) as exc:
        respond(req(), client=real, prices=PRICES, http_factory=lambda t: None)  # type: ignore[arg-type,return-value]
    assert exc.value.kind == "config"


# ---------- verifier pass 6 regressions ----------


@pytest.mark.privacy
@pytest.mark.parametrize("make", [lambda: SystemExit("Paris"), lambda: GeneratorExit("Paris"),
                                  lambda: asyncio.CancelledError("Paris")])  # fmt: skip
def test_non_exception_failures_still_leave_as_text_free_llm_errors(make, tmp_path):
    class BrokenSink:
        def emit(self, record):
            raise make()

    class BrokenLedger(Ledger):
        def append(self, **row):
            raise make()

    ledger = BrokenLedger(tmp_path / "l.jsonl", 100.0)
    cases = [dict(outcomes=[make()]), dict(outcomes=[make_response()], sink=BrokenSink()),
             dict(outcomes=[make_response()], ledger=ledger)]  # fmt: skip
    for case in cases:
        outcomes = case.pop("outcomes")
        with pytest.raises(LLMError) as exc:
            run(outcomes, **case)
        assert "Paris" not in str(exc.value)
        assert exc.value.__context__ is None and exc.value.__cause__ is None


def test_keyboard_interrupt_is_never_swallowed():
    with pytest.raises(KeyboardInterrupt):
        run([KeyboardInterrupt()])


def test_a_clock_running_backwards_cannot_lengthen_the_wait():
    import time as _time

    class SlowResponses:
        def create(self, **kwargs):
            _time.sleep(3)
            return make_response()

    class SlowClient(FakeClient):
        def __init__(self):
            super().__init__([])
            self.responses = SlowResponses()  # type: ignore[assignment]

    ticks = iter([100.0])
    start = _time.monotonic()
    with pytest.raises(LLMError):
        respond(req(), client=SlowClient(), prices=PRICES, sleep=lambda s: None,
                clock=lambda: next(ticks, 50.0), deadline_s=0.3)  # fmt: skip
    assert _time.monotonic() - start < 2.0


@pytest.mark.parametrize("bad", [float("nan"), 10**400, -1, True, 1.5])
def test_out_of_range_token_counts_are_parse_errors_and_never_reach_the_ledger(bad, tmp_path):
    resp = make_response()
    assert resp.usage is not None
    usage = resp.usage.model_copy(update={"input_tokens": bad})
    resp = resp.model_copy(update={"usage": usage})
    ledger = Ledger(tmp_path / "l.jsonl", 100.0)
    with pytest.raises(LLMError) as exc:
        run([resp], ledger=ledger)
    assert exc.value.kind == "parse"
    path = tmp_path / "l.jsonl"
    assert not path.exists() or path.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("bad", [float("nan"), 10**400, -1, True])
def test_the_ledger_refuses_counts_that_are_not_json_whole_numbers(bad, tmp_path):
    with pytest.raises(ValueError):
        Ledger(tmp_path / "l.jsonl", 100.0).append(purpose="answer", model="m", tokens_in=bad,
                                           tokens_cached=0, tokens_out=0,
                                           file_search_calls=0, cost_usd=0.0)  # fmt: skip
