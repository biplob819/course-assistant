"""A learner chat over D3.1's real gate and guard, scripted model outputs through N1.2's real
`respond`, a file-backed store and a file-backed trace sink (B2.5). No network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from course_assistant.answer import responder
from course_assistant.answer.ask import Assistant
from course_assistant.app.chat import ChatState, LearnerChat, PerThreadStore
from course_assistant.demo import passkey, spend
from course_assistant.demo.live import LiveDemo
from course_assistant.llm import JsonlSink
from tests.answer import scripted as s
from tests.answer.test_ask import (
    ASSESSMENTS,
    CONFIG,
    CORPUS,
    PRICES,
    S01,
    Clock,
    happy,
    ids,
)
from tests.demo.test_demo import CODE, IP, Secret, Tick, guard_with

OWN_KEY = "sk-proj-OWNKEY-b25-9d1e77c4"

KINDS: dict[str, dict[str, list[Any]]] = {
    "answered": happy(),
    "not_covered": {"route": [s.route("course")],
                    "answer": [s.cited("The draft names the user first.", S01, score=0.2)]},
    "graded_work": {"route": [s.route("graded_work", "x", "s01-a1")],
                    "answer": [s.cited("d", S01)],
                    "hint": [s.cited("Revisit Session 1 on naming the problem.", S01)],
                    "integrity": [s.integrity("clear")]},
    "admin": {"route": [s.route("admin")], "answer": [s.cited("d", S01)]},
    "off_topic": {"route": [s.route("off_topic")], "answer": [s.cited("d", S01)]},
    "injection": {"route": [s.route("injection")], "answer": [s.cited("d", S01)]},
    "error": {"route": s.down(), "answer": [s.cited("d", S01)]},
}  # fmt: skip


def queues(*kinds: str) -> dict[str, list[Any]]:
    """The scripted outputs of several turns in a row, one kind per turn."""
    out: dict[str, list[Any]] = {}
    for kind in kinds:
        for step, items in KINDS[kind].items():
            out.setdefault(step, []).extend(items)
    return out


class ChatRig:
    def __init__(self, tmp: Path, *, owner: dict[str, list[Any]] | None = None,
                 visitor: dict[str, list[Any]] | None = None, owner_key: bool = True,
                 key_factory: Any = None, gate: Any = None,
                 daily_questions: int | None = None) -> None:  # fmt: skip
        self.tmp = tmp
        self.secret = Secret()
        self.clock = Tick()
        self.gate = gate if gate is not None else passkey.PasskeyGate(self.secret, clock=self.clock)
        self.daily_questions = daily_questions
        self.counter = spend.FileSpendCounter(tmp / "spend.json")
        self.alerts: list[tuple[str, str]] = []
        self.guard = guard_with(self.counter, self.gate, None, self.clock, self.alerts)
        self.stores = PerThreadStore(tmp / "app.sqlite")
        self.sink = JsonlSink(tmp / "trace.jsonl")
        self.new_id = ids()
        self.owner_client = s.StepClient(**(owner if owner is not None else happy()))
        self.owner = responder(client=self.owner_client, prices=PRICES, sleep=lambda _: None)
        self.visitor_queues = visitor if visitor is not None else happy()
        self.visitor_client: s.StepClient | None = None
        self.keys_seen: list[str] = []
        self.key_factory = key_factory
        self.sessions: list[str | None] = []
        self.demo = LiveDemo(self.gate, self.guard, assistant_for=self.assistant_for,
                             owner_responder=self.owner if owner_key else None,
                             responder_for_key=self.for_key, prices=PRICES)  # fmt: skip
        self.chat = LearnerChat(self.gate, self.demo, assistant_for=self.assistant_for,
                                store=self.stores, traces=[self.sink], corpus=CORPUS,
                                now=Clock(), daily_questions=daily_questions)  # fmt: skip
        self.state = ChatState.new()

    def assistant_for(self, source: Any) -> Assistant:
        return Assistant(CONFIG, CORPUS, ASSESSMENTS, self.stores(), source, sink=self.sink,
                         now=Clock(), timer=lambda: 0.0, new_id=self.new_id)  # fmt: skip

    def for_key(self, key: str) -> Any:
        self.keys_seen.append(key)
        if self.key_factory is not None:
            return self.key_factory(key)
        return self.visitor_responder()

    def visitor_responder(self) -> Any:
        if self.visitor_client is None:  # one scripted client for every turn of the visitor
            self.visitor_client = s.StepClient(**self.visitor_queues)
        return responder(client=self.visitor_client, prices=PRICES, sleep=lambda _: None)

    def store(self) -> Any:
        return self.stores()

    def unlock(self, state: ChatState | None = None) -> None:
        self.chat.unlock(state or self.state, CODE, ip=IP)

    def ready(self, state: ChatState | None = None, *, improve: bool = False) -> ChatState:
        """Unlocked with the notice accepted."""
        st = state or self.state
        self.unlock(st)
        self.chat.accept_notice(st, improve_course=improve)
        return st
