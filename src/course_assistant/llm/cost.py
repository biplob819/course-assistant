"""Prices and per-call cost (OAI-03). Every money value is finite and non-negative (L-6)."""

from __future__ import annotations

import math
from importlib import resources
from typing import Annotated

import yaml
from pydantic import Field

from course_assistant.schemas._base import Model, Name

Price = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]


class ModelPrice(Model):
    input: Price
    output: Price
    cached_input: Price
    cache_write: Price
    checked_on: Name
    source: Name


class Prices(Model):
    file_search_per_1k: Price
    file_search_checked_on: Name
    models: dict[str, ModelPrice]

    def for_model(self, model: str) -> ModelPrice:
        """Exact alias, or a dated snapshot of it ("gpt-6.1-sol-2026-10-01")."""
        if model in self.models:
            return self.models[model]
        for alias, price in self.models.items():
            if model.startswith(alias + "-") and model[len(alias) + 1 :][:1].isdigit():
                return price
        raise KeyError(f"no price for model {model!r}; add it to prices.yaml")


def load_prices() -> Prices:
    text = resources.files("course_assistant.llm").joinpath("prices.yaml").read_text("utf-8")
    import json

    return Prices.model_validate_json(json.dumps(yaml.safe_load(text)))


def call_cost(
    price: ModelPrice,
    *,
    tokens_in: int,
    tokens_cached: int,
    tokens_out: int,
    file_search_calls: int,
    tokens_cache_write: int = 0,
    file_search_per_1k: float,
) -> float:
    """(input - cached - written) x in + cached x cached_in + written x cache_write
    + output x out + file-search calls x rate."""
    counts = (tokens_in, tokens_cached, tokens_out, file_search_calls, tokens_cache_write)
    if any(not isinstance(c, int) or isinstance(c, bool) or c < 0 for c in counts):
        raise ValueError("token and call counts must be non-negative integers")
    if tokens_cached + tokens_cache_write > tokens_in:
        raise ValueError("cached and cache-write tokens exceed the prompt")
    cost = (
        (tokens_in - tokens_cached - tokens_cache_write) * price.input
        + tokens_cached * price.cached_input
        + tokens_cache_write * price.cache_write
        + tokens_out * price.output
    ) / 1_000_000 + file_search_calls * file_search_per_1k / 1000
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("cost is not a finite non-negative number")
    return cost
