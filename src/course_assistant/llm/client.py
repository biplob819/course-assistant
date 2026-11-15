"""The one way the assistant calls OpenAI (OAI-01, OAI-02): Responses API, store=false,
explicit reasoning effort, transient-only retries, priced, ledgered and traced without text."""

from __future__ import annotations

import json
import random
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal

import openai
from pydantic import Field

from course_assistant.llm.cost import Prices, call_cost
from course_assistant.llm.ledger import Ledger
from course_assistant.llm.trace import TraceSink
from course_assistant.schemas._base import JsonValue, Model, Name, NonBlank, NonNegInt, PosInt

Effort = Literal["minimal", "low", "medium", "high"]
Purpose = Literal["answer", "route", "support", "integrity", "hint", "digest", "eval", "ingest"]
ErrorKind = Literal["transport", "refusal", "parse", "config", "budget"]
RETRY_STATUS = {408, 409, 429}
MAX_ATTEMPTS = 3


class InputItem(Model):
    role: Literal["user", "assistant", "developer"]
    content: str


class ResponsesRequest(Model):
    purpose: Purpose
    model: Name
    effort: Effort  # required: never left to a per-model default (EvalKit D-24)
    instructions: NonBlank
    input: tuple[InputItem, ...] = Field(min_length=1)
    vector_store_ids: tuple[Name, ...] = ()
    max_num_results: PosInt = 8
    filters: JsonValue | None = None
    json_schema: JsonValue | None = None
    max_output_tokens: PosInt = 2048


class FileCitation(Model):
    file_id: Name
    index: NonNegInt


class Hit(Model):
    file_id: Name
    score: float
    attributes: JsonValue = Field(default_factory=dict)


class ResponsesResult(Model):
    text: str
    citations: tuple[FileCitation, ...]
    hits: tuple[Hit, ...]
    refusal: bool
    model_snapshot: Name
    tokens_in: NonNegInt
    tokens_cached: NonNegInt
    tokens_cache_write: NonNegInt = 0
    tokens_out: NonNegInt  # includes reasoning tokens
    tokens_reasoning: NonNegInt
    file_search_calls: NonNegInt
    cost_usd: float
    latency_ms: NonNegInt
    request_id: Name
    attempts: PosInt


class LLMError(Exception):
    """A call that did not produce a result. The message never holds provider text or keys."""

    def __init__(self, kind: ErrorKind, detail: str, attempts: int = 1) -> None:
        super().__init__(f"{kind}: {detail} (after {attempts} attempt{'s' * (attempts != 1)})")
        self.kind = kind
        self.attempts = attempts


def _plain(value: Any) -> Any:
    """Frozen JSON back to plain dicts and lists for the SDK."""
    return json.loads(json.dumps(value))


def build_kwargs(r: ResponsesRequest) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": r.model,
        "instructions": r.instructions,
        "input": [{"role": i.role, "content": i.content} for i in r.input],
        "store": False,  # never stored at OpenAI (NFR3); not a request field, cannot be changed
        "reasoning": {"effort": r.effort},
        "max_output_tokens": r.max_output_tokens,
    }
    if r.vector_store_ids:
        tool: dict[str, Any] = {
            "type": "file_search",
            "vector_store_ids": list(r.vector_store_ids),
            "max_num_results": r.max_num_results,
        }
        if r.filters is not None:
            tool["filters"] = _plain(r.filters)
        kwargs["tools"] = [tool]
        kwargs["include"] = ["file_search_call.results"]
    if r.json_schema is not None:
        kwargs["text"] = {
            "format": {"type": "json_schema", "name": "result", "schema": _plain(r.json_schema),
                       "strict": True}
        }  # fmt: skip
    return kwargs


def _classify(exc: BaseException) -> tuple[ErrorKind, str, bool]:
    """(kind, safe detail, retryable). Details carry types and codes only, never messages."""
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return "transport", type(exc).__name__, True
    if isinstance(exc, openai.APIStatusError):
        code = exc.status_code
        if code in RETRY_STATUS or code >= 500:
            return "transport", f"HTTP {code}", True
        return "config", f"HTTP {code}", False
    return "config", type(exc).__name__, False


def _parse(resp: Any) -> dict[str, Any]:
    text_parts: list[str] = []
    citations: list[FileCitation] = []
    hits: list[Hit] = []
    refusal = False
    searches = 0
    for item in resp.output:
        if item.type == "file_search_call":
            searches += 1
            for res in item.results or []:
                if res.file_id is not None and res.score is not None:
                    hits.append(
                        Hit(file_id=res.file_id, score=float(res.score),
                            attributes=res.attributes or {})
                    )  # fmt: skip
        elif item.type == "message":
            for part in item.content:
                if part.type == "refusal":
                    refusal = True
                elif part.type == "output_text":
                    text_parts.append(part.text)
                    for ann in part.annotations:
                        if ann.type == "file_citation":
                            citations.append(FileCitation(file_id=ann.file_id, index=ann.index))
    usage = resp.usage
    if usage is None:
        raise LLMError("parse", "response has no usage")
    return {
        "text": "" if refusal else "".join(text_parts),
        "citations": tuple(citations),
        "hits": tuple(hits),
        "refusal": refusal,
        "model_snapshot": resp.model,
        "tokens_in": usage.input_tokens,
        "tokens_cached": usage.input_tokens_details.cached_tokens or 0,
        "tokens_cache_write": getattr(usage.input_tokens_details, "cache_write_tokens", 0) or 0,
        "tokens_out": usage.output_tokens,
        "tokens_reasoning": usage.output_tokens_details.reasoning_tokens or 0,
        "file_search_calls": searches,
    }


def respond(
    request: ResponsesRequest,
    *,
    client: Any,
    prices: Prices,
    ledger: Ledger | None = None,
    sink: TraceSink | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    deadline_s: float = 20.0,
    run_id: str = "",
) -> ResponsesResult:
    request_id = f"r-{uuid.uuid4().hex[:16]}"
    kwargs = build_kwargs(request)
    start = clock()
    attempts = 0
    last: tuple[ErrorKind, str] = ("transport", "no attempt")
    while attempts < MAX_ATTEMPTS:
        attempts += 1
        try:
            resp = client.responses.create(**kwargs)
        except Exception as exc:
            kind, detail, retry = _classify(exc)
            last = (kind, detail)
            if not retry or attempts >= MAX_ATTEMPTS:
                break
            delay = min(4.0, 0.5 * 2 ** (attempts - 1)) * (0.5 + random.random() / 2)
            if clock() - start + delay > deadline_s:
                break
            sleep(delay)
            continue
        parsed = _parse(resp)
        price = prices.for_model(parsed["model_snapshot"])
        cost = call_cost(
            price,
            tokens_in=parsed["tokens_in"],
            tokens_cached=parsed["tokens_cached"],
            tokens_out=parsed["tokens_out"],
            file_search_calls=parsed["file_search_calls"],
            file_search_per_1k=prices.file_search_per_1k,
            tokens_cache_write=parsed["tokens_cache_write"],
        )
        result = ResponsesResult(
            **parsed,
            cost_usd=cost,
            latency_ms=int((clock() - start) * 1000),
            request_id=request_id,
            attempts=attempts,
        )
        if ledger is not None:
            ledger.append(purpose=request.purpose, model=result.model_snapshot,
                          tokens_in=result.tokens_in, tokens_cached=result.tokens_cached,
                          tokens_out=result.tokens_out,
                          file_search_calls=result.file_search_calls, cost_usd=cost,
                          run_id=run_id)  # fmt: skip
        if sink is not None:
            record = {**result.model_dump(), "purpose": request.purpose, "model": request.model}
            record |= {"n_citations": len(result.citations), "n_hits": len(result.hits)}
            sink.emit(record)
        return result
    if sink is not None:
        sink.emit({"request_id": request_id, "purpose": request.purpose, "model": request.model,
                   "error": last[0], "attempts": attempts})  # fmt: skip
    raise LLMError(last[0], last[1], attempts)
