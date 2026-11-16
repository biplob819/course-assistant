"""An offline `evalkit run` of course-ta (E2.3 tests and the evalreg fixture builder).

Public golden cases only, scripted model outputs per case (through N1.2's real `respond`),
fake EvalKit adapters for the extractor and the groundedness judges, and a fake dual probe
panel. Runs in a given folder (EvalKit reads ./.env there: a fresh folder has none), with the
environment patched for the run and restored after, so a script can use it too.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evalkit import cli
from evalkit import run as R
from evalkit.adapters import CompletionRequest

from course_assistant.answer.replay import Recorder, Recording
from course_assistant.eval import dataset, scorers, system
from course_assistant.eval.calibrate import SupportTap
from course_assistant.eval.judges import JudgePanel, ProbeJudge
from course_assistant.eval.regression import PlaceholderSolutions
from course_assistant.schemas.config import GoldenCase
from tests.eval import scripted_eval as E

NOW = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)
EVALKIT_YAML = (
    "models: [course-ta]\nsystems: {course-ta: course_ta}\n"
    "judges: [claude-opus-5-5, gpt-6.1-sol]\nextractor: claude-haiku-5-5\nseed: 7\n"
)
SUPPORT_HEAD = "You check one claim against the source sentences it cites."
UNSUPPORTED_MARK = "unverified"  # a support sentence holding this is unsupported for the judges


def support_reply(req: CompletionRequest, *, strict_judge: bool = False) -> str:
    """A fake support judge (EvalKit's groundedness rubric): supported, citing the first unit,
    unless the sentence holds UNSUPPORTED_MARK (or, for the strict judge, 'mostly')."""
    claim = req.prompt.split("Claim:", 1)[1].split("Cited source sentences:", 1)[0]
    nums = re.findall(r"^\[(\d+)\]", req.prompt.split("Cited source sentences:", 1)[1], re.M)
    bad = UNSUPPORTED_MARK in claim or (strict_judge and "mostly" in claim)
    return json.dumps({"verdict": "unsupported" if bad else "supported",
                       "evidence": [] if bad else [int(nums[0])], "quote": None,
                       "rationale": "scripted"})  # fmt: skip


def probe_reply(violation_mark: str | None = None, *, strict_judge: bool = False,
                ) -> Callable[[CompletionRequest], str]:  # fmt: skip
    """Probe judges: `violation` when the prompt holds `violation_mark`, else `clean`; support
    prompts go to `support_reply`."""

    def reply(req: CompletionRequest) -> str:
        if req.prompt.startswith(SUPPORT_HEAD):
            return support_reply(req, strict_judge=strict_judge)
        bad = violation_mark is not None and violation_mark in req.prompt
        return json.dumps({"verdict": "violation" if bad else "clean",
                           "rationale": E.PROBE_RATIONALE})  # fmt: skip

    return reply


async def _no_sleep(_: float) -> None:
    return None


def fake_panel(*, violation_mark: str | None = None, cache: Path | None = None,
               ) -> tuple[JudgePanel, list[E.FakeAdapter]]:  # fmt: skip
    """Two fake judges; the second is stricter on support (so the two can disagree)."""
    fakes = [E.FakeAdapter(probe_reply(violation_mark)),
             E.FakeAdapter(probe_reply(violation_mark, strict_judge=True))]  # fmt: skip
    p = JudgePanel([ProbeJudge("claude-opus-5-5", "claude-opus-5-5", fakes[0]),
                    ProbeJudge("gpt-6.1-sol", "gpt-6.1-sol", fakes[1])], cache=cache,
                   retry_sleep=_no_sleep)  # fmt: skip
    return p, fakes


@contextlib.contextmanager
def _patched(folder: Path) -> Iterator[None]:
    import evalkit.onboarding as onboarding

    saved_env = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
    saved = (onboarding.SDK_MODULE, R.make_adapters, Path.cwd())
    fake = E.FakeAdapter(E.evalkit_reply())
    os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-a-key"
    os.environ["OPENAI_API_KEY"] = "sk-test-not-a-key"
    onboarding.SDK_MODULE = {}  # provider SDKs are never used here
    R.make_adapters = lambda config, prices: {"anthropic": fake, "openai": fake,  # type: ignore[assignment]
                                              **R.system_adapters(config)}  # fmt: skip
    os.chdir(folder)
    try:
        yield
    finally:
        os.chdir(saved[2])
        onboarding.SDK_MODULE, R.make_adapters = saved[0], saved[1]  # type: ignore[assignment]
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class RecordedPerCase:
    """`E.PerCase`, with every call of each case recorded by A2.5's `Recorder`."""

    def __init__(self, scripts: Mapping[str, dict[str, list[Any]]]) -> None:
        self.inner = E.PerCase(scripts)
        self.recorders = {rid: Recorder(r) for rid, r in self.inner.responders.items()}

    def call(self, request_id: str, request: Any) -> Any:
        return self.recorders[request_id].call(request_id, request)

    @property
    def recordings(self) -> list[Recording]:
        return [r for rid in sorted(self.recorders) for r in self.recorders[rid].recordings]


@dataclass
class OfflineRun:
    code: int
    run_dir: Path
    recorder: RecordedPerCase
    tap: SupportTap
    panel: JudgePanel
    fakes: list[E.FakeAdapter]


def offline_run(folder: Path, cases: Sequence[GoldenCase],
                scripts: Mapping[str, dict[str, list[Any]]], *,
                violation_mark: str | None = None,
                cache: Path | None = None) -> OfflineRun:  # fmt: skip
    """Run course-ta over `cases` offline; the system's calls are recorded and tapped."""
    folder.mkdir(parents=True, exist_ok=True)
    rows = dataset.build_rows(cases, E.CORPUS, E.GOLDEN_VERSION)
    task = dataset.write_task_dir(folder / "task", rows)
    (folder / "evalkit.yaml").write_text(EVALKIT_YAML, encoding="utf-8")
    recorder = RecordedPerCase(scripts)
    tap = SupportTap(recorder)
    system.configure(system.Runtime(config=E.CONFIG, corpus=E.CORPUS, assessments=E.ASSESSMENTS,
                                    source=tap, now=lambda: NOW, timer=lambda: 0.0))  # fmt: skip
    panel, fakes = fake_panel(violation_mark=violation_mark, cache=cache)
    scorers.configure_judges(panel, PlaceholderSolutions(), E.ASSESSMENTS)
    buf = io.StringIO()
    try:
        with _patched(folder), contextlib.redirect_stdout(buf):
            code = cli.main(["run", "evalkit.yaml", "--task", str(task), "--n", str(len(cases)),
                             "--live-judges", "--json"])  # fmt: skip
    finally:
        system.configure(None)
        scorers.configure_judges(None)
    out = json.loads(buf.getvalue())
    if "run_dir" not in out:
        raise RuntimeError(f"offline run failed: {out}")
    return OfflineRun(code, folder / out["run_dir"], recorder, tap, panel, fakes)


def mark_heldout(run_dir: Path, case_ids: set[str]) -> None:
    """Relabel some public cases as held-out in a run folder's cases (a stand-in for held-out
    cases, which tests never read)."""
    path = run_dir / "cases.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for r in rows:
        if r["id"] in case_ids:
            r["meta"]["split"] = "heldout"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
