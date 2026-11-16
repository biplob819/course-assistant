"""Post-answer leak scan (A2.5, Tech Spec §2 "post-answer rule recheck", §5 point 7).

A model-written text that is about to be shown is refused if it holds the canary planted in
the rules, anything shaped like a learner pid (16 hex characters, D-13), or a run of more than
8 consecutive words copied from any rules text. Text is normalised first (NFKD, accents,
combining marks, format, control and blank characters removed, known look-alikes mapped to
Latin, case folded, citation markers removed); then any remaining non-ASCII letter is a
wildcard in the comparison, so a look-alike from any script still lines up (D-43).
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
# a pid: 16 hex characters standing as one token, in any grouping, with up to three separator
# characters between any two of them (verifier A2.5 passes 1-5)
_PID = re.compile(r"(?<![0-9a-z])[0-9a-f](?:[^0-9a-z\n]{0,3}[0-9a-f]){15}(?![0-9a-z])")


def _seps(found: str) -> set[str]:
    return {c for c in found if not c.isalnum()}


def _continues(text: str, at: int, step: int, seps: set[str]) -> bool:
    """Does the hex run carry on past `at` (forwards for step 1, backwards for -1)? Only if
    the next character is a separator the match used (or '-' or ':' when it used none) and
    the token beyond it is a whole all-hex group of 4 or more, as in a UUID. A label ("id",
    "code"), a number or a short hex word beside the pid never counts (verifier A2.5 pass 6)."""
    i = at if step == 1 else at - 1
    if not (0 <= i < len(text)) or text[i] not in (seps or {"-", ":"}):
        return False
    j, token = i + step, []
    while 0 <= j < len(text) and text[j].isalnum():
        token.append(text[j])
        j += step
    return len(token) >= 4 and all(c in "0123456789abcdef" for c in token)


def _pid_like(folded: str) -> bool:
    for m in _PID.finditer(folded):
        found = m.group(0)
        if not (re.search(r"[a-f]", found) and re.search(r"[0-9]", found)):
            continue
        seps = _seps(found)
        if _continues(folded, m.end(), 1, seps) or _continues(folded, m.start(), -1, seps):
            continue  # part of a longer id, such as a UUID
        return True
    return False


# citation markers in every form the app or a model writes; removed (not spaced) so one inside
# a word cannot split it
_CITE = re.compile(
    r"\[[0-9]{1,3}(?:\s*[,;]\s*[0-9]{1,3})*\]|【[^】]{0,40}】"
    r"|\((?:unit|session|slide|p\.)[^)]{0,30}\)"
)
# letters that render as nothing (Hangul fillers, braille blank) and control characters
_BLANKS = dict.fromkeys(map(ord, "\u115f\u1160\u3164\uffa0\u2800"), None)
# Cherokee and Armenian letters that render as Latin ones (verifier A2.5 pass 2)
_MORE_CONFUSABLES = str.maketrans({
    "Ꭺ": "a", "Ᏼ": "b", "Ꮯ": "c", "Ꭰ": "d", "Ꭼ": "e", "Ꮐ": "g", "Ꮋ": "h", "Ꭵ": "i",
    "Ꮶ": "k", "Ꮮ": "l", "Ꮇ": "m", "Ꮎ": "o", "Ꮲ": "p", "Ꮪ": "s", "Ꭲ": "t", "Ꮩ": "v",
    "Ꮃ": "w", "Ꮓ": "z", "ꭺ": "a", "ꮯ": "c", "ꮎ": "o", "ꮪ": "s", "ꭼ": "e", "ꮃ": "w", "ꮖ": "i",
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
    """The text as the scan compares it: accents, combining marks, format and control
    characters and blank letters removed, compatibility forms folded, known look-alikes mapped
    to Latin, case folded, citation markers removed. Any other non-ASCII letter is left for
    the wildcard comparison below (D-43)."""
    folded = unicodedata.normalize("NFKD", text).translate(_BLANKS)
    folded = "".join(
        ch for ch in folded
        if unicodedata.category(ch) not in ("Cf", "Mn", "Me")
        and (unicodedata.category(ch) != "Cc" or ch in "\n\t")
    )  # fmt: skip
    folded = unicodedata.normalize("NFKC", folded)
    folded = folded.translate(_CONFUSABLES).translate(_MORE_CONFUSABLES).casefold()
    # again after casefold, which can turn a mapped capital into an unmapped small letter
    folded = folded.translate(_CONFUSABLES).translate(_MORE_CONFUSABLES)
    return _CITE.sub("", folded)


def _skeleton(text: str) -> str:
    """Letters and digits only; an ASCII one stays, any other letter becomes '?', a wildcard
    that matches any one character (so a look-alike from any script still lines up)."""
    out = []
    for ch in normalise(text):
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        elif ch.isalnum():
            out.append("?")
    return "".join(out)


def _like(window: str, target: str, min_exact: float = 0.7) -> bool:
    """Same length, every position equal or a wildcard, and most positions exact."""
    if len(window) != len(target):
        return False
    exact = 0
    for a, b in zip(window, target, strict=True):
        if a == b:
            exact += 1
        elif a != "?":
            return False
    return exact >= min_exact * len(target)


def _contains_like(haystack: str, needle: str) -> bool:
    n = len(needle)
    return any(_like(haystack[i : i + n], needle) for i in range(len(haystack) - n + 1))


def _alnum(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def rules_texts() -> tuple[str, ...]:
    """Every instruction text a model sees in ask(), read from the step classes."""
    return (RouteStep.ROUTE_RULES, AnswerStep.ANSWER_RULES, GroundStep.SUPPORT_RULES,
            IntegrityStep.INTEGRITY_RULES, HintStep.HINT_RULES)  # fmt: skip


def words(text: str) -> list[str]:
    """Words as runs of letters and digits; a non-ASCII letter is a '?' wildcard."""
    folded = normalise(text).replace("’", "").replace("'", "")
    out = []
    for w in _WORD.findall(folded):
        out.append("".join(c if c.isascii() else "?" for c in w))
    return out


@functools.cache
def _fragments(texts: tuple[str, ...]) -> dict[tuple[int, ...], list[tuple[str, ...]]]:
    """Every 9-word run of the rules, indexed by its word lengths for the wildcard match."""
    grams: dict[tuple[int, ...], list[tuple[str, ...]]] = {}
    for text in texts:
        w = words(text)
        for i in range(len(w) - FRAGMENT_WORDS + 1):
            gram = tuple(w[i : i + FRAGMENT_WORDS])
            grams.setdefault(tuple(map(len, gram)), []).append(gram)
    return grams


def _copies_rules(w: list[str]) -> bool:
    grams = _fragments(rules_texts())
    for i in range(len(w) - FRAGMENT_WORDS + 1):
        window = tuple(w[i : i + FRAGMENT_WORDS])
        for gram in grams.get(tuple(map(len, window)), ()):
            if _like("".join(window), "".join(gram)):
                return True
    return False


def scan(text: str) -> tuple[Finding, ...]:
    """What the text leaks, in a fixed order; empty when it is clean."""
    found: list[Finding] = []
    canary = AnswerStep.CANARY.casefold()
    canary = "".join(c for c in canary if c.isalnum())
    flat = _skeleton(text)
    if _contains_like(flat, canary) or _contains_like(flat[::-1], canary):
        found.append("canary")  # reversed too (right-to-left override)
    folded = normalise(text)
    if _pid_like(folded):
        found.append("pid")
    w = words(text)
    reversed_words = words(normalise(text)[::-1])
    if _copies_rules(w) or _copies_rules(reversed_words):
        found.append("rules")
    return tuple(found)
