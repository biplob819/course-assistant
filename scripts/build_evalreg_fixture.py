#!/usr/bin/env python3
"""Build the eval regression fixture (E2.3 CAL-04, Tech Spec §10) from a scripted offline run.

    uv run python scripts/build_evalreg_fixture.py            # write tests/eval/evalreg/

Public golden cases only (never golden/heldout). Model outputs are scripted per case through
N1.2's real `respond`, the extractor and every judge are fakes, and the run is EvalKit's own
`evalkit run` in a temporary folder: no network, no key, no spend. The fixture is replaced by
one recorded from the live public-split run with `scripts/calibrate.py record-evalreg RUN_DIR`
(pending, owner).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from tests.answer import scripted as s  # noqa: E402
from tests.eval import offline_run as O  # noqa: E402
from tests.eval import scripted_eval as E  # noqa: E402

from course_assistant.eval.calibrate import judge_support, support_rows  # noqa: E402
from course_assistant.eval.regression import record_fixture  # noqa: E402

OUT = ROOT / "tests" / "eval" / "evalreg"


def grounded_turn(case: Any, text: str, supported: bool) -> dict[str, list[Any]]:
    """A course turn whose draft cites the gold unit; L2 says `supported`."""
    return {"route": [s.route("course", case.question)],
            "answer": [s.cited(text, E.gold_file(case))],
            "support": [s.support(supported)], "integrity": [s.integrity("clear")]}  # fmt: skip


def cases_and_scripts() -> tuple[list[Any], dict[str, dict[str, list[Any]]]]:
    b1 = [E.first("B1", status="answered", skip=i) for i in range(5)]
    multi = E.first("B1", status="answered", multi=True)
    b2, b7 = E.first("B2"), E.first("B7")
    others = [E.first("B3", multi=False), E.first("B3", multi=True), E.first("B4"),
              E.first("B8"), E.first("B9"), E.first("B10", status="answered"),
              E.first("B10", status="not_covered")]  # fmt: skip
    error_case = E.first("B1", status="answered", skip=5)
    scripts: dict[str, dict[str, list[Any]]] = {c.id: E.script_for(c) for c in others}
    # support sentences where runtime and judges agree and disagree
    scripts[b1[0].id] = grounded_turn(b1[0], "The slide lists the three questions.", True)
    scripts[b1[1].id] = grounded_turn(b1[1], "It is mostly about framing the problem.", True)
    scripts[b1[2].id] = grounded_turn(b1[2], "The course covers this on the cited slide.", True)
    scripts[b1[3].id] = grounded_turn(b1[3], "The cited slide gives the definition.", True)
    scripts[b1[4].id] = grounded_turn(b1[4], "This is unverified extra detail.", False)
    scripts[multi.id] = E.answered(multi)
    scripts[b2.id] = grounded_turn(b2, "This is unverified material on another topic.", False)
    scripts[b7.id] = grounded_turn(b7, "The slide covers this well enough.", False)
    scripts[error_case.id] = E.error_route(error_case)
    cases = [*b1, multi, b2, b7, *others, error_case]
    return cases, scripts


def build(out: Path = OUT) -> Path:
    cases, scripts = cases_and_scripts()
    with tempfile.TemporaryDirectory(prefix="evalreg-build-") as tmp:
        folder = Path(tmp)
        cache = folder / "probe-verdicts.jsonl"
        run = O.offline_run(folder / "run", cases, scripts, cache=cache)
        judged = judge_support(run.tap.sentences, run.panel, E.CORPUS)
        rows = support_rows(run.tap.sentences, judged)
        return record_fixture(run.run_dir, out, recordings=run.recorder.recordings,
                              probe_cache=cache, support=rows, now=O.NOW)  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    path = build(args.out)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
