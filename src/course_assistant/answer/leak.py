"""Post-answer leak scan (A2.5, Tech Spec §2 "post-answer rule recheck", §5 point 7).

A model-written text that is about to be shown is refused if it holds the canary planted in
the rules, anything shaped like a learner pid (16 hex characters, D-13), or a run of more than
8 consecutive words copied from any rules text. Text is normalised first (NFKC, no format
characters, look-alikes mapped to Latin, case folded), and words are compared as runs of
letters and digits.
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
# 16 hex characters, also when split by short runs of separators (verifier A2.5 passes 1, 2).
# A pid is a hash, so it holds at least one letter a-f: an all-digit run (a list of numbers,
# a long figure) is not a pid and never turns a good answer into not_covered
_PID = re.compile(r"(?<![0-9a-f])(?:[0-9a-f][^0-9a-z]{0,3}){15}[0-9a-f](?![0-9a-f])")
_CITE = re.compile(r"\[[0-9]{1,3}\]|\((?:unit|session|slide|p\.)[^)]{0,30}\)")
# Cherokee and Armenian letters that render as Latin ones (verifier A2.5 pass 2)
_MORE_CONFUSABLES = str.maketrans({
    "Ꭺ": "a", "Ᏼ": "b", "Ꮯ": "c", "Ꭰ": "d", "Ꭼ": "e", "Ꮐ": "g", "Ꮋ": "h", "Ꭵ": "i",
    "Ꮶ": "k", "Ꮮ": "l", "Ꮇ": "m", "Ꮎ": "o", "Ꮲ": "p", "Ꮪ": "s", "Ꭲ": "t", "Ꮩ": "v",
    "Ꮃ": "w", "Ꮓ": "z", "ꭺ": "a", "ꮯ": "c", "ꮎ": "o", "ꮪ": "s",
    "օ": "o", "ս": "u", "ո": "n", "հ": "h", "ց": "g", "զ": "q", "ա": "w", "ւ": "l",
    "Օ": "o", "Ս": "u", "Ո": "n",
    # Lisu capitals, which render as Latin capitals
    "ꓮ": "a", "ꓐ": "b", "ꓚ": "c", "ꓓ": "d", "ꓰ": "e", "ꓝ": "f", "ꓖ": "g", "ꓧ": "h",
    "ꓲ": "i", "ꓙ": "j", "ꓗ": "k", "ꓡ": "l", "ꓟ": "m", "ꓠ": "n", "ꓳ": "o", "ꓑ": "p",
    "ꓣ": "r", "ꓢ": "s", "ꓔ": "t", "ꓴ": "u", "ꓦ": "v", "ꓪ": "w", "ꓫ": "x", "ꓬ": "y",
    "ꓜ": "z",
})  # fmt: skip
_WORD = re.compile(r"[^\W_]+")
# common look-alikes mapped to Latin before scanning (Cyrillic and Greek letters that render
# as Latin ones); NFKC already folds fullwidth and compatibility forms
_CONFUSABLES = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "ԁ": "d",
        "ɡ": "g",
        "һ": "h",
        "ӏ": "l",
        "ԛ": "q",
        "ԝ": "w",
        "А": "a",
        "В": "b",
        "Е": "e",
        "К": "k",
        "М": "m",
        "Н": "h",
        "О": "o",
        "Р": "p",
        "С": "c",
        "Т": "t",
        "Х": "x",
        "У": "y",
        "І": "i",
        "Ј": "j",
        "Ѕ": "s",
        "α": "a",
        "ο": "o",
        "ρ": "p",
        "ε": "e",
        "ι": "i",
        "κ": "k",
        "ν": "v",
        "τ": "t",
        "Α": "a",
        "Β": "b",
        "Ε": "e",
        "Ι": "i",
        "Κ": "k",
        "Μ": "m",
        "Ν": "n",
        "Ο": "o",
        "Ρ": "p",
        "Τ": "t",
        "Χ": "x",
        "Υ": "y",
        "Ζ": "z",
    }
)


def normalise(text: str) -> str:
    """The text as the scan compares it: accents, combining marks and format characters
    removed, compatibility forms folded, look-alikes mapped to Latin, case folded, citation
    markers dropped."""
    # NFKD splits accents off their letters; combining marks (Mn, Me) and format characters
    # (Cf) are then dropped, so neither can split a word or hide a letter (pass 2)
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if unicodedata.category(ch) not in ("Cf", "Mn", "Me"))
    folded = unicodedata.normalize("NFKC", folded)
    folded = folded.translate(_CONFUSABLES).translate(_MORE_CONFUSABLES).casefold()
    return _CITE.sub(" ", folded)  # a citation marker dropped inside a fragment hides nothing


def _alnum(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def rules_texts() -> tuple[str, ...]:
    """Every instruction text a model sees in ask(), read from the step classes."""
    return (RouteStep.ROUTE_RULES, AnswerStep.ANSWER_RULES, GroundStep.SUPPORT_RULES,
            IntegrityStep.INTEGRITY_RULES, HintStep.HINT_RULES)  # fmt: skip


def words(text: str) -> list[str]:
    folded = normalise(text).replace("’", "'")
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
    folded = normalise(text)
    # the canary on letters and digits only, so separators or invisibles cannot split it
    canary, flat = _alnum(AnswerStep.CANARY.casefold()), _alnum(folded)
    if canary in flat or canary in flat[::-1]:  # reversed too (right-to-left override)
        found.append("canary")
    if any(re.search(r"[a-f]", m.group(0)) for m in _PID.finditer(folded)):
        found.append("pid")
    w = words(text)
    grams = _fragments(rules_texts())
    if any(tuple(w[i : i + FRAGMENT_WORDS]) in grams for i in range(len(w) - FRAGMENT_WORDS + 1)):
        found.append("rules")
    return tuple(found)
