"""Fakes built from the OpenAI SDK's own models (L-9), so field names and nesting are real."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import openai
from openai.types.responses import Response


def make_response(
    text: str = "Offline evals run before release. [1]",
    citations: tuple[tuple[str, int], ...] = (("file-s02-09", 30),),
    hits: tuple[tuple[str, float], ...] = (("file-s02-09", 0.82), ("file-s02-p3", 0.61)),
    model: str = "gpt-6.1-sol-2026-10-01",
    tokens: tuple[int, int, int, int] = (4000, 1200, 700, 300),  # in, cached, out, reasoning
    refusal: str | None = None,
    file_search: bool = True,
) -> Response:
    tin, cached, tout, reasoning = tokens
    output: list[dict[str, Any]] = []
    if file_search:
        output.append({
            "id": "fs_1", "type": "file_search_call", "status": "completed",
            "queries": ["offline online evals"],
            "results": [
                {"file_id": f, "score": s, "filename": f + ".txt", "text": "slide text",
                 "attributes": {"session": "s02"}}
                for f, s in hits
            ],
        })  # fmt: skip
    content: list[dict[str, Any]]
    if refusal is not None:
        content = [{"type": "refusal", "refusal": refusal}]
    else:
        content = [{
            "type": "output_text", "text": text,
            "annotations": [
                {"type": "file_citation", "file_id": f, "filename": f + ".txt", "index": i}
                for f, i in citations
            ],
        }]  # fmt: skip
    output.append(
        {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
         "content": content}
    )  # fmt: skip
    return Response.model_validate({
        "id": "resp_1", "created_at": 1_763_190_000, "model": model, "object": "response",
        "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": {
            "input_tokens": tin,
            "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": 0},
            "output_tokens": tout, "output_tokens_details": {"reasoning_tokens": reasoning},
            "total_tokens": tin + tout,
        },
    })  # fmt: skip


def status_error(code: int) -> openai.APIStatusError:
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    resp = httpx.Response(code, request=req, json={"error": {"message": "x"}})
    classes = {
        400: openai.BadRequestError, 401: openai.AuthenticationError,
        403: openai.PermissionDeniedError, 404: openai.NotFoundError, 409: openai.ConflictError,
        429: openai.RateLimitError, 500: openai.InternalServerError,
        503: openai.InternalServerError,
    }  # fmt: skip
    cls = classes.get(code, openai.APIStatusError)
    return cls(
        "boom sk-proj-SECRETSECRETSECRETSECRET", response=resp, body=None
    )  # secret-scan: allow


def connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))


class FakeResponses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Response:
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


class FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.responses = FakeResponses(outcomes)
