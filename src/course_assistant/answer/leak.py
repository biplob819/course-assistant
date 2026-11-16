"""Post-answer leak scan (A2.5, Tech Spec §2 "post-answer rule recheck", §5 point 7).

A model-written text that is about to be shown is refused if it holds the canary planted in
the rules, anything shaped like a learner pid (16 hex characters, D-13), or a run of more than
8 consecutive words copied from any rules text. Words are compared lower-cased, as runs of
letters and digits, so punctuation, line breaks and case cannot hide a copied fragment.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from typing import Literal

from course_assistant.answer.answer import AnswerStep
from course_assistant.answer.ground import GroundStep
from course_assistant.answer.hint import HintStep
from course_assistant.answer.integrity import IntegrityStep
from course_assistant.answer.route import RouteStep

Finding = Literal["canary", "pid", "rules"]
FRAGMENT_WORDS = 9  # "more than 8 words" (§5 point 7)
_PID = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{16}(?![0-9a-fA-F])")
_WORD = re.compile(r"[^\W_]+")


def rules_texts() -> tuple[str, ...]:
    """Every instruction text a model sees in ask(), read from the step classes."""
    return (RouteStep.ROUTE_RULES, AnswerStep.ANSWER_RULES, GroundStep.SUPPORT_RULES,
            IntegrityStep.INTEGRITY_RULES, HintStep.HINT_RULES)  # fmt: skip


def words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    return _WORD.findall(folded.replace("'", ""))


@functools.cache
def _fragments(texts: tuple[str, ...]) -> frozenset[tuple[str, ...]]:
    grams: set[tuple[str, ...]] = set()
    for text in texts:
        w = words(text)
        grams.update(tuple(w[i : i + FRAGMENT_WORDS]) for i in range(len(w) - FRAGMENT_WORDS + 1))
    return frozenset(grams)


def scan(text: str) -> tuple[Finding, ...]:
    """What the text leaks, in a fixed order; empty when it is clean."""
    found: list[Finding] = []
    folded = unicodedata.normalize("NFKC", text)
    if AnswerStep.CANARY.casefold() in folded.casefold():
        found.append("canary")
    if _PID.search(folded):
        found.append("pid")
    w = words(text)
    grams = _fragments(rules_texts())
    if any(tuple(w[i : i + FRAGMENT_WORDS]) in grams for i in range(len(w) - FRAGMENT_WORDS + 1)):
        found.append("rules")
    return tuple(found)
