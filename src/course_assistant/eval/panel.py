"""The configured judge panel, by name only (no EvalKit import), so `doctor` can tell which
provider keys an evaluation needs without the `eval` extra's heavy imports.

OWNER D-80: one OpenAI judge (gpt-6.1-sol) and an OpenAI claim extractor (gpt-6-luna) while
the Anthropic account is unfunded. The dual panel (D-12) adds claude-opus-5-5 back here.
"""

from __future__ import annotations

from collections.abc import Sequence

JUDGES: tuple[str, ...] = ("gpt-6.1-sol",)
EXTRACTOR = "gpt-6-luna"


def family(alias: str) -> str:
    """The provider of a model alias: `anthropic`, `openai`, or the alias itself."""
    if alias.startswith("claude"):
        return "anthropic"
    if alias.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    return alias


def needed_keys(judges: Sequence[str] | None = None, extractor: str | None = None) -> set[str]:
    """Environment variable names an evaluation run needs: the answer model's OpenAI key plus
    one key per provider of the judges and the extractor (read at call time, so a changed
    panel is seen)."""
    judges = JUDGES if judges is None else judges
    extractor = EXTRACTOR if extractor is None else extractor
    return {"OPENAI_API_KEY"} | {f"{family(m).upper()}_API_KEY" for m in (*judges, extractor)}
