"""The live-mode guard and the executor (D3.1, RPL-02; D-16, EvalKit L-39).

Before any model call a live request passes, in order: the passkey (`PasskeyGate.require`),
the per-request cap, the session-token rate limit and, on the owner's key, a reservation of
the request's hold against today's budget in the persistent `SpendCounter`. The hold is the
one cost number: the guard reserves it, `CappedSource` (the executor) never lets the request's
calls reserve past it, and the counter is settled to what the executor counted.

A visitor's own key (OWNER D-45) still needs the passkey, the cap and the rate limit, and
holds nothing against the owner's budget.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from course_assistant.answer.replay import Source
from course_assistant.demo._errors import GuardRefused
from course_assistant.demo.passkey import PasskeyGate
from course_assistant.demo.spend import MICRO, SpendCounter, to_micro
from course_assistant.llm import LLMError, Prices, ResponsesRequest, ResponsesResult, call_cost
from course_assistant.llm.client import MAX_ATTEMPTS

LOG = logging.getLogger("course_assistant.demo")
MAX_SESSIONS = 10_000  # rate-limit memory bound: the oldest idle sessions are forgotten first
FILE_SEARCH_CALLS_BOUND = 3  # file_search calls one Responses call may make, at most
CHUNK_TOKENS = 800  # the vector store's chunk size: tokens per retrieved result
ITEM_OVERHEAD_TOKENS = 16  # role and framing per input item

STORE_DOWN = (
    "The live budget store is unavailable right now. Replay mode always works, or try again later."
)
BUDGET_USED = "Today's live budget is used up. Replay mode is always available."
OWNER_KEY_OFF = "Live mode on the demo's key is off. You can use your own OpenAI key."


@dataclass(frozen=True)
class GuardConfig:
    """Placeholder amounts until the owner sets the demo budget (D3.2 reads them from the
    secret store). The daily cap must sit under the demo project's hard limit (RPL-03)."""

    request_hold_usd: float = 0.50
    daily_cap_usd: float = 5.00
    rate_limit: int = 5
    rate_window_s: float = 3600.0

    def __post_init__(self) -> None:
        for v in (self.request_hold_usd, self.daily_cap_usd, self.rate_window_s):
            if isinstance(v, bool) or not math.isfinite(v) or v <= 0:
                raise ValueError("guard amounts and windows must be positive and finite")
        if self.request_hold_usd > self.daily_cap_usd:
            raise ValueError("the request hold cannot be over the daily cap")
        if self.rate_limit < 1:
            raise ValueError("rate_limit must be at least 1")


@dataclass(frozen=True)
class Ticket:
    """A request that passed the guard: settle it with the executor's total."""

    day: str
    hold_micro: int
    session: str
    own_key: bool
    slot: float

    @property
    def hold_usd(self) -> float:
        return self.hold_micro / MICRO


class Guard:
    def __init__(self, gate: PasskeyGate, spend: SpendCounter, config: GuardConfig | None = None,
                 *, clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], str] = lambda: datetime.now(UTC).date().isoformat(),
                 alert: Callable[[str, str], None] | None = None) -> None:  # fmt: skip
        self.gate = gate
        self.spend = spend
        self.config = config or GuardConfig()
        self._clock = clock
        self._today = today
        self._alert = alert
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, estimate_usd: float, session_token: str, *, own_key: bool = False) -> Ticket:
        """Refuse (`GuardRefused`) or admit one live request; nothing is called before this
        returns. On the owner's key, exactly `estimate_usd` is held against today's budget."""
        self.gate.require(session_token)
        hold = to_micro(estimate_usd)
        cap = to_micro(self.config.request_hold_usd)
        if hold == 0 or hold > cap:
            limit = self.config.request_hold_usd
            raise GuardRefused(f"This request could cost up to ${estimate_usd:.2f}, over the "
                               f"${limit:.2f} per-request limit.")  # fmt: skip
        slot = self._take_slot(session_token)
        day = self._today()
        if not own_key:
            try:
                held = self.spend.reserve(day, hold, to_micro(self.config.daily_cap_usd))
            except Exception as exc:  # the store is down: refuse readably, never echo it
                self._give_back(session_token, slot)
                LOG.warning("demo spend store unavailable (%s)", type(exc).__name__)
                self._raise_alert("spend_store", type(exc).__name__)
                raise GuardRefused(STORE_DOWN) from None
            if not held:
                self._give_back(session_token, slot)
                raise GuardRefused(BUDGET_USED)
        return Ticket(day=day, hold_micro=hold, session=session_token, own_key=own_key,
                      slot=slot)  # fmt: skip

    def settle(self, ticket: Ticket, actual_usd: float) -> None:
        """Settle the hold to the executor's total. If the store is down the whole hold stays:
        today's budget is overcounted, never undercounted."""
        if ticket.own_key:
            return  # nothing was held against the owner's budget
        try:
            actual = to_micro(actual_usd)
        except ValueError:
            actual = ticket.hold_micro  # a bad total never frees the hold (L-6)
            self._raise_alert("settle_value", "ValueError")
        if actual > ticket.hold_micro:
            LOG.warning("demo request spent past its hold")
            self._raise_alert("over_hold", "bound")
        try:
            self.spend.settle(ticket.day, ticket.hold_micro, actual)
        except Exception as exc:
            LOG.warning("demo spend settle failed (%s); hold kept", type(exc).__name__)
            self._raise_alert("spend_settle", type(exc).__name__)

    def remaining_today(self) -> float | None:
        """Today's live budget not yet spent or held; None when the store cannot be read."""
        try:
            spent = self.spend.spent(self._today())
        except Exception as exc:
            LOG.warning("demo spend store unavailable (%s)", type(exc).__name__)
            return None
        return max(0, to_micro(self.config.daily_cap_usd) - spent) / MICRO

    # ---------- rate limit ----------

    def _take_slot(self, session: str) -> float:
        with self._lock:
            now = self._clock()
            self._evict(now)
            hits = self._hits.setdefault(session, deque())
            while hits and now - hits[0] > self.config.rate_window_s:
                hits.popleft()
            if len(hits) >= self.config.rate_limit:
                raise GuardRefused(
                    f"Rate limit: {self.config.rate_limit} live questions per "
                    f"{self.config.rate_window_s / 60:.0f} minutes. Replay mode is always "
                    "available."
                )
            hits.append(now)
            return now

    def _give_back(self, session: str, slot: float) -> None:
        with self._lock:  # nothing ran: return the rate-limit slot
            hits = self._hits.get(session)
            if hits and slot in hits:
                hits.remove(slot)

    def _evict(self, now: float) -> None:
        if len(self._hits) <= MAX_SESSIONS:
            return
        window = self.config.rate_window_s
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > window]:
            del self._hits[key]
        while len(self._hits) > MAX_SESSIONS:
            del self._hits[min(self._hits, key=lambda k: self._hits[k][-1])]

    def _raise_alert(self, what: str, kind: str) -> None:
        if self._alert is not None:
            try:
                self._alert(what, kind)
            except Exception:
                LOG.warning("demo alert hook failed")


def call_bound_usd(request: ResponsesRequest, prices: Prices) -> float:
    """The most one attempt of `request` can cost: every UTF-8 byte of the instructions,
    input and schema counted as a token (over-counts every script, L-39), plus the retrieved
    chunks and file-search calls when the call searches, and `max_output_tokens` out."""
    price = prices.for_model(request.model)
    tokens_in = len(request.instructions.encode("utf-8"))
    tokens_in += sum(len(i.content.encode("utf-8")) + ITEM_OVERHEAD_TOKENS for i in request.input)
    if request.json_schema is not None:
        import json

        tokens_in += len(json.dumps(request.json_schema).encode("utf-8"))
    searches = FILE_SEARCH_CALLS_BOUND if request.vector_store_ids else 0
    tokens_in += searches * request.max_num_results * CHUNK_TOKENS
    return call_cost(price, tokens_in=tokens_in, tokens_cached=0,
                     tokens_out=request.max_output_tokens, file_search_calls=searches,
                     file_search_per_1k=prices.file_search_per_1k)  # fmt: skip


class CappedSource:
    """The executor's cost control around a request's `Source`. Each call first reserves its
    worst case (the single-attempt bound times N1.2's `MAX_ATTEMPTS`); a call that would take
    the request past its hold is refused with an `LLMError` (kind `config`: the turn ends with
    status `error` and nothing is spent). After the call, the actual cost plus one full bound
    per retried attempt is counted; a failed call counts its bound for every attempt it made
    (cancelled and timed-out calls are never free)."""

    def __init__(self, source: Source, hold_usd: float, prices: Prices, *,
                 alert: Callable[[str, str], None] | None = None) -> None:  # fmt: skip
        self.source = source
        self.hold_usd = hold_usd
        self.prices = prices
        self._alert = alert
        self._lock = threading.Lock()
        self._counted = 0.0
        self._pending = 0.0
        self.calls = 0
        self.refused = 0

    @property
    def total_usd(self) -> float:
        with self._lock:
            return self._counted + self._pending  # a call still running counts in full

    def call(self, request_id: str, request: ResponsesRequest) -> ResponsesResult:
        single = call_bound_usd(request, self.prices)
        worst = single * MAX_ATTEMPTS
        with self._lock:
            if self._counted + self._pending + worst > self.hold_usd + 1e-12:
                self.refused += 1
                raise LLMError("config", "request cost cap reached", 1)
            self._pending += worst
            self.calls += 1
        try:
            result = self.source.call(request_id, request)
        except LLMError as exc:
            self._count(worst, single * min(max(exc.attempts, 1), MAX_ATTEMPTS))
            raise
        except BaseException:
            self._count(worst, worst)
            raise
        actual = result.cost_usd
        if (
            isinstance(actual, bool)
            or not isinstance(actual, int | float)
            or not (math.isfinite(actual) and actual >= 0)
        ):
            self._count(worst, worst)
            raise LLMError("config", "a call reported a bad cost", 1)
        if actual > single:
            LOG.warning("a demo call cost more than its bound")
            if self._alert is not None:
                try:
                    self._alert("call_bound", "bound")
                except Exception:
                    LOG.warning("demo alert hook failed")
        retried = min(max(result.attempts, 1), MAX_ATTEMPTS) - 1
        self._count(worst, actual + single * retried)
        return result

    def _count(self, pending: float, counted: float) -> None:
        with self._lock:
            self._pending -= pending
            self._counted += counted
