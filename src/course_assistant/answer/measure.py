"""Small measurements for the A2.5 smoke run: recall@k of a search against gold units, and
percentiles, kept here so the live script's arithmetic is tested offline."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from course_assistant.ingest.corpus import Corpus, unit_key
from course_assistant.schemas.request import SearchHit


def recall_at_k(hits: Sequence[SearchHit], gold: Iterable[str], corpus: Corpus,
                k: int = 8) -> float:  # fmt: skip
    """Share of the gold locators (`s02:slides:9`, D-30) found among the first k distinct units
    of the search, in the order returned. No gold units is an error, never a free 1.0."""
    gold_set = set(gold)
    if not gold_set:
        raise ValueError("recall needs at least one gold unit")
    if k < 1:
        raise ValueError("k must be at least 1")
    by_index = {u.unit_index: u for u in corpus.units}
    seen: list[str] = []
    for h in hits:
        unit = by_index.get(h.unit_index)
        if unit is None or unit.file_id != h.file_id:
            continue
        key = unit_key(unit)
        if key not in seen:
            seen.append(key)
        if len(seen) == k:
            break
    return len(gold_set & set(seen)) / len(gold_set)


def percentile(values: Iterable[float], q: float) -> float:
    """Nearest-rank percentile (q in (0, 100]); no values is an error."""
    data = sorted(values)
    if not data:
        raise ValueError("no values")
    if not 0 < q <= 100:
        raise ValueError("q must be in (0, 100]")
    if any(not math.isfinite(v) for v in data):
        raise ValueError("values must be finite")
    rank = max(1, math.ceil(q / 100 * len(data)))
    return data[rank - 1]
