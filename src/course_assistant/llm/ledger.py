"""Append-only spend ledger (OAI-04): one JSON line per priced call, never any text."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
from pathlib import Path

WARN_AT = 0.8


class Ledger:
    def __init__(self, path: Path, monthly_budget_usd: float) -> None:
        if not math.isfinite(monthly_budget_usd) or monthly_budget_usd < 0:
            raise ValueError("monthly budget must be a finite non-negative number")
        self.path = Path(path)
        self.budget = monthly_budget_usd
        self.warnings: list[str] = []

    def append(self, *, purpose: str, model: str, tokens_in: int, tokens_cached: int,
               tokens_out: int, file_search_calls: int, cost_usd: float, run_id: str = "",
               at: dt.datetime | None = None, tokens_cache_write: int = 0,
               tokens_reasoning: int = 0, priced: bool = True) -> None:  # fmt: skip
        if not math.isfinite(cost_usd) or cost_usd < 0:
            raise ValueError("cost must be a finite non-negative number")
        when = (at or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
        row = {
            "at": when.isoformat(timespec="seconds"), "purpose": purpose, "model": model,
            "tokens_in": tokens_in, "tokens_cached": tokens_cached, "tokens_out": tokens_out,
            "file_search_calls": file_search_calls, "cost_usd": round(cost_usd, 8),
            "run_id": run_id, "tokens_cache_write": tokens_cache_write,
            "tokens_reasoning": tokens_reasoning, "priced": priced,
        }  # fmt: skip
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # read the month total and append under one exclusive lock, so two writers cannot
        # both see the same total and both miss (or both report) the 80% crossing
        with self.path.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                before = self._total(fh, when.strftime("%Y-%m"))
                fh.write(json.dumps(row, sort_keys=True) + "\n")
                fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        after = before + cost_usd
        if self.budget and before < WARN_AT * self.budget <= after:
            self.warnings.append(
                f"spend this month ${after:.2f} passed 80% of the ${self.budget:.2f} budget"
            )

    @staticmethod
    def _total(fh, month: str) -> float:
        fh.seek(0)
        costs = []
        for n, line in enumerate(fh.read().splitlines(), start=1):
            if line.strip():
                try:
                    row = json.loads(line)
                    at, cost = str(row["at"]), float(row["cost_usd"])
                except (ValueError, KeyError, TypeError):
                    raise ValueError(f"spend ledger line {n} is malformed") from None
                if at.startswith(month):
                    costs.append(cost)
        return math.fsum(costs)

    def month_total(self, month: str | None = None) -> float:
        month = month or dt.datetime.now(dt.UTC).strftime("%Y-%m")
        if not self.path.is_file():
            return 0.0
        with self.path.open("r", encoding="utf-8") as fh:
            return self._total(fh, month)
