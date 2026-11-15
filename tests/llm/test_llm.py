"""N1.2: the OpenAI wrapper, cost, ledger and trace."""

from __future__ import annotations

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
    rec = {"request_id": "r", "question": "secret", "output": "secret", "anything_new": "secret",
           "tokens_in": 1, "cost_usd": 0.1}  # fmt: skip
    assert strip_text(rec) == {"request_id": "r", "tokens_in": 1, "cost_usd": 0.1}


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
