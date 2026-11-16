"""Live demo requests behind the passkey and the guard (D3.1, RPL-02 and RPL-04).

`LiveDemo` is the one entry the demo pages call. Every method calls `PasskeyGate.require`
before anything else, with or without a visitor's own OpenAI key (OWNER D-45). A live
question then passes `Guard.allow`, runs one `Assistant.turn` through a `CappedSource`, is
settled to the executor's total, and a handed-over turn is queued for the teaching team
(B2.1). A visitor's key is used for that request only: never stored, logged or kept.
Logs are aggregate only: mode, status, cost and latency.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from course_assistant.answer.ask import Assistant, Turn
from course_assistant.answer.replay import Live, Responder, Source
from course_assistant.demo._errors import GuardRefused
from course_assistant.demo.cache import DemoCache
from course_assistant.demo.guard import OWNER_KEY_OFF, CappedSource, Guard
from course_assistant.demo.passkey import PasskeyGate
from course_assistant.escalation import queue_handover
from course_assistant.llm import Prices
from course_assistant.schemas.request import Answer

LOG = logging.getLogger("course_assistant.demo")
NO_KEY = "Paste an OpenAI API key, or use the demo's key."


class LiveDemo:
    def __init__(self, gate: PasskeyGate, guard: Guard, *,
                 assistant_for: Callable[[Source], Assistant],
                 owner_responder: Responder | None,
                 responder_for_key: Callable[[str], Responder], prices: Prices,
                 cache: DemoCache | None = None,
                 alert: Callable[[str, str], None] | None = None) -> None:  # fmt: skip
        if guard.gate is not gate:
            raise ValueError("the guard must use the same passkey gate")
        self.gate = gate
        self.guard = guard
        self.assistant_for = assistant_for
        self.owner_responder = owner_responder
        self.responder_for_key = responder_for_key
        self.prices = prices
        self.cache = cache
        self.alert = alert

    def __repr__(self) -> str:
        return "LiveDemo()"

    def ask(self, session: str, question: str, pid: str, *, own_key: str | None = None,
            conversation_id: str | None = None) -> Turn:  # fmt: skip
        """One live question. Raises `GuardRefused` (a readable reason) before any call when
        the passkey, the cap, the rate limit or the budget says no."""
        self.gate.require(session)
        if own_key is not None and (not isinstance(own_key, str) or not own_key.strip()):
            raise GuardRefused(NO_KEY)
        if own_key is None and self.owner_responder is None:
            raise GuardRefused(OWNER_KEY_OFF)
        ticket = self.guard.allow(self.guard.config.request_hold_usd, session,
                                  own_key=own_key is not None)  # fmt: skip
        capped: CappedSource | None = None
        try:
            if own_key is not None:
                responder = self.responder_for_key(own_key.strip())
            else:
                assert self.owner_responder is not None
                responder = self.owner_responder
            own_key = None  # the key lives only inside this request's responder
            capped = CappedSource(Live(responder), ticket.hold_usd, self.prices,
                                  alert=self.alert)  # fmt: skip
            assistant = self.assistant_for(capped)
            turn = assistant.turn(question, pid, None, conversation_id)
        finally:
            self.guard.settle(ticket, capped.total_usd if capped is not None else 0.0)
        queue_handover(turn, store=assistant.store)
        LOG.info("demo live: mode=%s status=%s cost_usd=%.4f latency_ms=%d",
                 "own_key" if ticket.own_key else "owner", turn.answer.status,
                 turn.answer.cost_usd, turn.answer.latency_ms)  # fmt: skip
        return turn

    def replay(self, session: str, case_id: str) -> Answer:
        """A stored demo Answer, behind the same passkey (every page, OWNER D-45)."""
        self.gate.require(session)
        if self.cache is None:
            raise GuardRefused("The demo replay file is not available yet.")
        return self.cache.answer(case_id)
