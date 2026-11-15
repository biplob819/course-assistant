"""OpenAI access for the course assistant (N1.2)."""

from course_assistant.llm.client import (
    FileCitation,
    Hit,
    InputItem,
    LLMError,
    ResponsesRequest,
    ResponsesResult,
    build_kwargs,
    respond,
)
from course_assistant.llm.cost import ModelPrice, Prices, call_cost, load_prices
from course_assistant.llm.ledger import Ledger
from course_assistant.llm.trace import TRACE_KEYS, JsonlSink, TraceSink, strip_text

__all__ = [
    "TRACE_KEYS",
    "FileCitation",
    "Hit",
    "InputItem",
    "JsonlSink",
    "LLMError",
    "Ledger",
    "ModelPrice",
    "Prices",
    "ResponsesRequest",
    "ResponsesResult",
    "TraceSink",
    "build_kwargs",
    "call_cost",
    "load_prices",
    "respond",
    "strip_text",
]
