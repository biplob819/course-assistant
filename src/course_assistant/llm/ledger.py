"""Append-only spend ledger (OAI-04): one JSON line per priced call, never any text."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import re
from pathlib import Path

from course_assistant.llm.trace import MODEL_PATTERN

WARN_AT = 0.8


def _lock(fh, timeout_s: float) -> None:
    """An exclusive lock, waited for at most `timeout_s` (a held lock never blocks a call past
    its deadline, verifier N1.2 pass 5)."""
    import time

    end = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= end:
                raise OSError("spend ledger is locked by another writer") from None
            time.sleep(0.02)


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
               tokens_reasoning: int = 0, priced: bool = True,
               lock_timeout_s: float = 5.0) -> None:  # fmt: skip
        if not math.isfinite(cost_usd) or cost_usd < 0:
            raise ValueError("cost must be a finite non-negative number")
        if not isinstance(model, str) or not (model == "unknown" or MODEL_PATTERN.fullmatch(model)):
            raise ValueError("model must be a model name")  # never free text (verifier N1.2 pass 7)
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{0,64}", run_id):
            raise ValueError("run_id must be up to 64 letters, digits, dot, dash or underscore")
        if not isinstance(purpose, str) or not re.fullmatch(r"[a-z_]{1,32}", purpose):
            raise ValueError("purpose must be a short lowercase word")
        counts = {
            "tokens_in": tokens_in,
            "tokens_cached": tokens_cached,
            "tokens_out": tokens_out,
            "file_search_calls": file_search_calls,
            "tokens_cache_write": tokens_cache_write,
            "tokens_reasoning": tokens_reasoning,
        }
        for name, value in counts.items():  # JSON-safe whole numbers only (verifier N1.2 pass 6)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 10**15:
                raise ValueError(f"{name} must be a whole number between 0 and 10**15")
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
            _lock(fh, lock_timeout_s)
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
