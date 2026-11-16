"""Golden replay (A2.5, Tech Spec §10): recorded sample-course requests replay through `ask()`
with no API calls and must give the stored `Answer`s exactly.

The fixture (`replay/golden.json`, packaged so `course-assistant eval-replay` runs from the
wheel) is built by `scripts/build_replay_fixture.py` from the public golden split only. It
holds the config, the sample corpus's structure (unit text replaced by a placeholder, so no
course text ships in the wheel), the sample briefs, and per request: the learner turn, any
earlier turns to seed, the recorded calls and the expected Answer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from course_assistant.answer.ask import Assistant
from course_assistant.answer.replay import Recording, Replay, Source
from course_assistant.ingest.corpus import Corpus
from course_assistant.schemas._base import Model, NonBlank
from course_assistant.schemas.config import AppConfig
from course_assistant.schemas.corpus import AssessmentList
from course_assistant.schemas.request import (
    Answer,
    Citation,
    Conversation,
    GroundingResult,
    Question,
)
from course_assistant.store.db import Store

FIXTURE = Path(__file__).with_name("replay") / "golden.json"
FORMAT = 1
PLACEHOLDER = "(unit text is not kept in the replay fixture)"


class Case(Model):
    id: str
    source: str  # the public golden case id it was built from
    scenario: str
    request_id: str
    learner_pid: str
    session: str | None
    question: NonBlank
    prior_turns: tuple[tuple[NonBlank, NonBlank], ...] = ()
    paused: bool = False
    expected_status: Literal["answered", "not_covered", "hint_only", "handed_over", "error"]
    recordings: tuple[Recording, ...]
    expected: Answer | None = None


class Fixture(Model):
    format: int
    note: str
    now: dt.datetime
    config: AppConfig
    corpus: Corpus
    assessments: AssessmentList
    cases: tuple[Case, ...]


def load(path: Path = FIXTURE) -> Fixture:
    return Fixture.model_validate_json(Path(path).read_text(encoding="utf-8"))


def ids_for(request_id: str) -> Callable[[], str]:
    """Deterministic 16-hex ids for one replayed request (new conversations)."""
    counter = itertools.count(1)
    return lambda: hashlib.sha256(f"{request_id}:{next(counter)}".encode()).hexdigest()[:16]


def seed(store: Store, fx: Fixture, case: Case) -> str | None:
    """Store the case's earlier turns as one conversation of its learner, 10 minutes apart and
    ending 5 minutes before `now`; return the conversation id (None when there are none)."""
    if not case.prior_turns:
        return None
    hexpart = case.request_id[2:]
    cid = f"c-{hexpart}"
    n = len(case.prior_turns)
    start = fx.now - dt.timedelta(minutes=5 + 10 * n)
    store.put_conversation(Conversation(conversation_id=cid, learner_pid=case.learner_pid,
                                        session_scope=case.session, started_at=start))  # fmt: skip
    unit = fx.corpus.units[0]
    for i, (q, a) in enumerate(case.prior_turns, start=1):
        at = start + dt.timedelta(minutes=10 * (i - 1))
        qid = f"q-{hashlib.sha256(f'{hexpart}:{i}'.encode()).hexdigest()[:16]}"
        store.put_question(Question(question_id=qid, conversation_id=cid, turn_index=i,
                                    learner_pid=case.learner_pid, session_scope=case.session,
                                    text=q, asked_at=at))  # fmt: skip
        grounding = GroundingResult(citations_resolve=True, top_score=0.9,
                                    bar=fx.config.grounding_bar, sentence_support=(True,),
                                    passed=True, ground_version="seeded")  # fmt: skip
        store.put_answer(Answer(question_id=qid, status="answered", text=a,
                                citations=(Citation(file_id=unit.file_id,
                                                    unit_index=unit.unit_index,
                                                    label=unit.label, session=unit.session),),
                                grounding=grounding, integrity_passed=True,
                                model_snapshot=fx.config.answer_model,
                                rules_hash=Assistant.rules_hash(),
                                corpus_version=fx.corpus.corpus_version, top_search_score=0.9,
                                cost_usd=0.0, latency_ms=0, created_at=at))  # fmt: skip
    return cid


def run_case(fx: Fixture, case: Case, source: Source) -> Answer:
    """One request through `ask()` on a fresh in-memory store, its calls served by `source`."""
    store = Store(":memory:")
    try:
        cid = seed(store, fx, case)
        if case.paused:
            store.set_pause("all", True, "instructor-1", at=fx.now - dt.timedelta(minutes=1))
        assistant = Assistant(fx.config, fx.corpus, fx.assessments, store, source,
                              now=lambda: fx.now, timer=lambda: 0.0,
                              new_id=ids_for(case.request_id))  # fmt: skip
        return assistant.ask(case.question, case.learner_pid, case.session, cid,
                             request_id=case.request_id)  # fmt: skip
    finally:
        store.close()


def replay_case(fx: Fixture, case: Case) -> Answer:
    """One request from its recordings only; every recording must be used."""
    replay = Replay(case.recordings)
    answer = run_case(fx, case, replay)
    replay.check_used(case.request_id)
    return answer


def run(path: Path = FIXTURE) -> list[tuple[str, bool, str]]:
    """Replay every case: (case id, matched exactly, status got)."""
    fx = load(path)
    out = []
    for case in fx.cases:
        got = replay_case(fx, case)
        ok = case.expected is not None and got == case.expected
        out.append((case.id, ok, got.status))
    return out


def dumps(fx: Fixture) -> str:
    return json.dumps(fx.model_dump(mode="json"), indent=1, sort_keys=True,
                      ensure_ascii=False) + "\n"  # fmt: skip
