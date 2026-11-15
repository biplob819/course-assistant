"""The one way the assistant calls OpenAI (OAI-01, OAI-02): Responses API, store=false,
explicit reasoning effort, transient-only retries, priced, ledgered and traced without text."""

from __future__ import annotations

import json
import math
import random
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal

import httpx2
import openai
from openai._base_client import SyncHttpxClientWrapper
from pydantic import Field

from course_assistant.llm.cost import Prices, call_cost
from course_assistant.llm.ledger import Ledger
from course_assistant.llm.trace import TraceSink, strip_text
from course_assistant.schemas._base import JsonValue, Model, Name, NonBlank, NonNegInt, PosInt

Effort = Literal["minimal", "low", "medium", "high"]
Purpose = Literal["answer", "route", "support", "integrity", "hint", "digest", "eval", "ingest"]
# A refusal is not an error: it comes back as a result with refusal=True (spec N1.2).
ErrorKind = Literal["transport", "parse", "config"]
RETRY_STATUS = {408, 409, 429}
MAX_ATTEMPTS = 3


class InputItem(Model):
    role: Literal["user", "assistant", "developer"]
    content: str


class ResponsesRequest(Model):
    purpose: Purpose
    model: Name
    effort: Effort  # required: never left to a per-model default (EvalKit D-24)
    instructions: NonBlank = Field(repr=False)  # never in a repr or a log line
    input: tuple[InputItem, ...] = Field(min_length=1, repr=False)
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
    score: float | None  # None when the provider returned no score; never silently dropped
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
        self.kind: ErrorKind = kind
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
    if isinstance(exc, ValueError | openai.APIResponseValidationError):  # a body we cannot read
        return "parse", type(exc).__name__, False
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return "transport", type(exc).__name__, True
    if isinstance(exc, openai.APIStatusError):
        code = exc.status_code
        if code in RETRY_STATUS or code >= 500:
            return "transport", f"HTTP {code}", True
        return "config", f"HTTP {code}", False
    return "config", type(exc).__name__, False


def _parse(resp: Any) -> dict[str, Any]:
    """Final-answer text only (commentary-phase messages are dropped), with each citation's
    index shifted to its place in the joined text."""
    text = ""
    citations: list[FileCitation] = []
    hits: list[Hit] = []
    refusal = False
    searches = 0
    for item in resp.output:
        if item.type == "file_search_call":
            searches += 1
            for res in item.results or []:
                if res.file_id is not None:
                    score = None if res.score is None else float(res.score)
                    hits.append(Hit(file_id=res.file_id, score=score,
                                    attributes=res.attributes or {}))  # fmt: skip
        elif item.type == "message":
            if getattr(item, "phase", None) == "commentary":
                continue
            for part in item.content:
                if part.type == "refusal":
                    refusal = True
                elif part.type == "output_text":
                    offset = len(text)
                    text += part.text
                    for ann in part.annotations:
                        if ann.type == "file_citation":
                            citations.append(
                                FileCitation(file_id=ann.file_id, index=offset + ann.index)
                            )
    usage = resp.usage
    if usage is None:
        raise LLMError("parse", "response has no usage")
    return {
        "text": "" if refusal else text,
        "citations": () if refusal else tuple(citations),
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


def _default_http(timeout: float) -> httpx2.Client:
    return httpx2.Client(timeout=timeout)


class _Overrun(Exception):
    pass


def _within(seconds: float, call: Callable[[], Any], on_overrun: Callable[[], None]) -> Any:
    """Run one attempt in a daemon thread and wait at most `seconds` for it. On overrun
    `on_overrun` closes the attempt's own HTTP client, so the blocked read ends and the thread
    exits; a daemon thread never holds up interpreter exit (verifier N1.2 pass 3)."""
    box: dict[str, Any] = {}
    done = threading.Event()

    def work() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=work, daemon=True, name="llm-attempt").start()
    if not done.wait(max(seconds, 0.001)):
        on_overrun()
        raise _Overrun
    if "error" in box:
        raise box["error"]
    return box["value"]


def _emit(sink: TraceSink, record: dict[str, Any], attempts: int) -> None:
    """Strip, then emit. Any sink failure becomes a config error without the sink's text,
    raised outside the except block so the original is not even kept as __context__."""
    failed: str | None = None
    try:
        sink.emit(strip_text(record))
    except Exception as exc:
        failed = type(exc).__name__
    if failed is not None:
        raise LLMError("config", f"trace sink failed ({failed})", attempts)


def _record(ledger: Ledger, attempts: int, **row: Any) -> None:
    failed: str | None = None
    try:
        ledger.append(**row)
    except Exception as exc:  # any failure, including RecursionError on a malformed line
        failed = type(exc).__name__
    if failed is not None:
        raise LLMError("config", f"spend ledger unusable ({failed})", attempts)


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
    http_factory: Callable[[float], httpx2.Client] | None = None,
) -> ResponsesResult:
    if not (isinstance(deadline_s, int | float) and math.isfinite(deadline_s) and deadline_s > 0):
        raise LLMError("config", "deadline must be a finite positive number of seconds")
    if (
        isinstance(client, openai.OpenAI)
        and http_factory is None
        and not isinstance(client._client, SyncHttpxClientWrapper)
    ):
        # a custom http_client (proxy, mTLS, test transport) would be silently replaced by the
        # per-attempt client; the caller must say how to build one (D-33)
        raise LLMError("config", "client has a custom http_client: pass http_factory")
    make_http = http_factory or _default_http
    request_id = f"r-{uuid.uuid4().hex[:16]}"
    kwargs = build_kwargs(request)
    start = clock()
    attempts = 0
    last: tuple[ErrorKind, str] = ("transport", "no attempt")
    while attempts < MAX_ATTEMPTS:
        remaining = deadline_s - (clock() - start)
        if remaining <= 0:
            last = ("transport", "deadline reached")
            break
        attempts += 1
        # the SDK's own retries are off, each read may use only the time left, and the whole
        # attempt is bounded by a wall-clock wait, so a response that trickles in slowly
        # cannot outlast the deadline (verifier N1.2 pass 2)
        if not hasattr(client, "with_options"):
            raise LLMError("config", "client cannot turn off its own retries", attempts)
        setup_failed: str | None = None
        http: httpx2.Client | None = None
        caller: Any = None
        try:
            http = make_http(remaining) if isinstance(client, openai.OpenAI) else None
            options: dict[str, Any] = {"max_retries": 0, "timeout": remaining}
            if http is not None:
                options["http_client"] = http  # this attempt's own pool, closed afterwards
            caller = client.with_options(**options)
        except Exception as exc:
            setup_failed = type(exc).__name__
        if setup_failed is not None:
            raise LLMError("config", f"client setup failed ({setup_failed})", attempts)
        close = http.close if http is not None else (lambda: None)
        try:
            resp = _within(remaining, lambda c=caller: c.responses.create(**kwargs), close)
        except _Overrun:
            last = ("transport", "deadline reached")
            break
        except Exception as exc:
            close()
            kind, detail, retry = _classify(exc)
            last = (kind, detail)
            if not retry or attempts >= MAX_ATTEMPTS:
                break
            delay = min(4.0, 0.5 * 2 ** (attempts - 1)) * (0.5 + random.random() / 2)
            if clock() - start + delay > deadline_s:
                break
            sleep(delay)
            continue
        close()
        try:
            parsed = _parse(resp)
        except LLMError:
            raise LLMError("parse", "response has no usage", attempts) from None
        except (ValueError, TypeError, AttributeError) as exc:
            raise LLMError("parse", type(exc).__name__, attempts) from None
        try:
            cost = call_cost(
                prices.for_model(parsed["model_snapshot"]),
                tokens_in=parsed["tokens_in"],
                tokens_cached=parsed["tokens_cached"],
                tokens_out=parsed["tokens_out"],
                file_search_calls=parsed["file_search_calls"],
                file_search_per_1k=prices.file_search_per_1k,
                tokens_cache_write=parsed["tokens_cache_write"],
            )
        except (KeyError, ValueError) as exc:
            # the call was made and billed: record its tokens unpriced before failing
            if ledger is not None:
                _record(ledger, attempts, purpose=request.purpose, model=parsed["model_snapshot"],
                              tokens_in=parsed["tokens_in"], tokens_cached=parsed["tokens_cached"],
                              tokens_out=parsed["tokens_out"],
                              file_search_calls=parsed["file_search_calls"], cost_usd=0.0,
                              run_id=run_id, priced=False,
                              tokens_cache_write=parsed["tokens_cache_write"],
                              tokens_reasoning=parsed["tokens_reasoning"])  # fmt: skip
            kind: ErrorKind = "config" if isinstance(exc, KeyError) else "parse"
            raise LLMError(kind, "call could not be priced", attempts) from None
        result = ResponsesResult(
            **parsed,
            cost_usd=cost,
            latency_ms=int((clock() - start) * 1000),
            request_id=request_id,
            attempts=attempts,
        )
        if ledger is not None:
            _record(ledger, attempts, purpose=request.purpose, model=result.model_snapshot,
                          tokens_in=result.tokens_in, tokens_cached=result.tokens_cached,
                          tokens_out=result.tokens_out,
                          file_search_calls=result.file_search_calls, cost_usd=cost,
                          run_id=run_id, tokens_cache_write=result.tokens_cache_write,
                          tokens_reasoning=result.tokens_reasoning)  # fmt: skip
        if sink is not None:
            record = {**result.model_dump(), "purpose": request.purpose, "model": request.model}
            record |= {"n_citations": len(result.citations), "n_hits": len(result.hits)}
            _emit(sink, record, attempts)  # stripped inside, whatever the sink (OAI-05)
        return result
    if sink is not None:
        failure = {"request_id": request_id, "purpose": request.purpose, "model": request.model}
        _emit(sink, {**failure, "error": last[0], "attempts": attempts}, attempts)
    raise LLMError(last[0], last[1], attempts)
