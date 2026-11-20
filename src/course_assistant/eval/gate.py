"""One release gate for every course (E2.4, R3.1; Tech Spec §6.5).

`check_run` gathers every input of `release.verdict` for one finished held-out run and applies
it: the §6.4 thresholds, certified judge calibration, the regression fixture, the tuning log
(hash chain, continuity with the decisions, the bar and ground version in use, a fresh held-out
set after a change, append-only against git), the coverage rule, the held-out exposure guard,
the held-out cases checked against their source, drift, and the paired comparison with the last
live run. `approve` then writes the live pointer (`LIVE.json` and `live-baseline.json`) that
the pages and `course-assistant ask` serve.

`scripts/release_gate.py` (the sample course, from this repository's fixed paths) and
`course-assistant eval` (a course team's own files, by path) both call these functions; only
where each input comes from differs. Every input that may read a private file or call git is a
callable, evaluated in the same order for both.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from course_assistant.eval import release
from course_assistant.ingest.corpus import CorpusStore
from course_assistant.schemas.config import AppConfig, GoldenCase
from course_assistant.schemas.corpus import AssessmentList


def live_folder(run: Path) -> Path:
    """The eval folder around an EvalKit run folder (RUN/evalkit/<run>)."""
    return run.parent.parent if run.parent.name == "evalkit" else run


def load_baseline(baseline_path: Path | None, store: Any,
                  ) -> tuple[Any, list[str], str | None]:  # fmt: skip
    """(values, snapshots, error). A first release (no baseline) is allowed only with no
    --baseline and no LIVE.json; a baseline given or expected but missing, unreadable or not
    `{"values": {...}, "snapshots": [...]}` is an error (exit 2 before the stops)."""
    state: Any = {}
    if baseline_path is not None:
        path, what = baseline_path, "the --baseline file"
    else:
        live = store.dir / release.LIVE_FILE
        # lexists: a dangling or looping symlink is a file that cannot be read, not an absence
        if not os.path.lexists(live):
            if os.path.lexists(store.dir / release.BASELINE_FILE):
                return None, [], "live-baseline.json exists without LIVE.json: the store is " \
                    "inconsistent"  # fmt: skip
            return None, [], None  # nothing has gone live: a first release
        try:
            state = json.loads(live.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, [], "LIVE.json is unreadable: the live baseline cannot be found"
        if not isinstance(state, dict) or not state.get("corpus_version"):
            return None, [], "LIVE.json is malformed: the live baseline cannot be found"
        path, what = store.dir / release.BASELINE_FILE, "the live baseline (live-baseline.json)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [], f"{what} is missing"
    except (OSError, ValueError):
        return None, [], f"{what} is unreadable or not JSON"
    if not isinstance(data, dict) or set(data) - {"run", "values", "snapshots"} \
            or "values" not in data or data["values"] is None:  # fmt: skip
        return None, [], f"{what} is malformed (needs values and snapshots)"
    snaps = data.get("snapshots", [])
    if not isinstance(snaps, list) or not all(isinstance(x, str) for x in snaps):
        return None, [], f"{what} is malformed (snapshots must be a list of names)"
    if baseline_path is None and data.get("run") != state.get("run"):
        return None, [], (f"{what} belongs to run {data.get('run')!r}, not the live run "
                          f"{state.get('run')!r}")  # fmt: skip
    return data["values"], list(snaps), None


def tracked_texts(root: Path) -> list[str]:
    """Every tracked file that is text by content (no NUL byte), whatever its suffix (D-54)."""
    done = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=False)
    texts = []
    for f in done.stdout.decode("utf-8", errors="replace").split("\0"):
        path = root / f
        if f and path.is_file():
            text = release.text_of(path.read_bytes())
            if text is not None:
                texts.append(text)
    return texts


def git_root(folder: Path) -> Path | None:
    """The top of the git work tree holding `folder`, or None outside git (or without git)."""
    try:
        done = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=folder,
                              capture_output=True, text=True, check=False)  # fmt: skip
    except (FileNotFoundError, NotADirectoryError):
        return None
    top = done.stdout.strip()
    return Path(top).resolve() if done.returncode == 0 and top else None


def exposure(held: Sequence[GoldenCase] | None, root: Path | None) -> list[str] | None:
    """D-54: held-out ids whose question appears in a tracked file of `root`'s repository;
    None (the guard is skipped, never a pass claimed) without the private cases or without a
    repository."""
    if held is None or root is None:
        return None
    return release.heldout_exposure({c.id: c.question for c in held}, tracked_texts(root))


def log_problems(bar: float | None, env: Mapping[str, str], path: Path, *,
                 decisions_text: str, root: Path | None) -> list[str]:  # fmt: skip
    """The tuning log: chain, continuity, the bar and ground version in use, fresh held-out,
    append-only against git (where the log sits in `root`'s repository)."""
    from course_assistant.answer.ground import GroundStep
    from course_assistant.eval import tuning

    if not path.is_file():
        return ["no tuning log"]
    log = tuning.read_log(path)
    out = tuning.chain_problems(path)
    out += tuning.continuity_problems(log, decisions_text)
    out += tuning.unlogged(log, bar, GroundStep.version())
    if tuning.fresh_heldout_required(log):
        out.append("the bar or support rules changed after the held-out split was scored: "
                   "write a fresh held-out set first (§5.4)")  # fmt: skip
    if root is not None and path.resolve().is_relative_to(root):
        out += tuning.append_only_problems(path.resolve(), root, env) or []
    return out


def gated_settings(config: AppConfig, assessments_bytes: bytes) -> dict[str, Any]:
    """The settings §6.5 gates besides the corpus: the rules hash, the bar and the models (with
    their effort). Recorded in LIVE.json at a flip; the removal-only shortcut needs them all
    unchanged."""
    from course_assistant.answer.ask import Assistant

    # the whole config and the assessments file too, so retrieval breadth, the window, the
    # vector store or an assessment change all send a removal through the full gate (pass 10)
    whole = json.dumps(config.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return {"rules_hash": Assistant.rules_hash(), "grounding_bar": config.grounding_bar,
            "answer_model": config.answer_model, "check_model": config.check_model,
            "reasoning_effort": dict(config.reasoning_effort),
            "config_sha256": hashlib.sha256(whole.encode("utf-8")).hexdigest(),
            "assessments_sha256": hashlib.sha256(assessments_bytes).hexdigest()}  # fmt: skip


def check_run(run: Path, *, config: AppConfig, store: CorpusStore, assessments: AssessmentList,
              held: Callable[[], list[GoldenCase] | None],
              heldout_ids: Callable[[], frozenset[str]],
              public: Callable[[], list[GoldenCase]],
              calibration: Callable[[], Mapping[str, str]],
              fixture: Callable[[], Sequence[str]],
              log: Callable[[float | None], Sequence[str]],
              exposure: Callable[[list[GoldenCase] | None], Sequence[str] | None],
              baseline_path: Path | None) -> release.Verdict:  # fmt: skip
    """Gather every input of the verdict for one held-out run folder and apply the gate.

    `held` gives the private held-out cases, already checked against their source (None where
    they are absent: the gate then cannot pass), `heldout_ids` the ids the run must hold, and
    `public` the tuning split (for the coverage rule)."""
    from evalkit import run as R

    from course_assistant.eval.dataset import corpus_context, to_case
    from course_assistant.eval.release import ReleaseError
    from course_assistant.eval.tuning import recorded_bar

    corpus = store.current()
    if corpus is None:
        raise FileNotFoundError("no ingested corpus: run course-assistant ingest first")
    cases = R.read_cases(run)
    golden_versions = {str(c.meta.get("golden_corpus_version")) for c in cases.values()}
    drift_file = live_folder(run) / "drift.json"
    recorded = json.loads(drift_file.read_text(encoding="utf-8")) if drift_file.is_file() else {}
    retired = frozenset(str(x) for x in recorded.get("retired", ()))
    held_cases = held()
    notes = []
    expected: dict[str, Any] | None = None
    converted: frozenset[str] = frozenset()
    if held_cases is not None and len(golden_versions) == 1:  # recompute where the cases exist
        version = next(iter(golden_versions))
        found = release.drift(held_cases, corpus, version)
        if set(found.retired) != {i for i in retired if i in {c.id for c in held_cases}}:
            raise ReleaseError("drift.json does not match the drift of the held-out cases on "
                               "this corpus: re-run the gate")  # fmt: skip
        retired = frozenset(found.retired)
        converted = frozenset(found.converted)
        context = corpus_context(corpus)
        expected = {c.id: to_case(c, corpus, version, context=context) for c in found.cases}
    tuning_cases = public()
    if len(golden_versions) == 1:
        tuning_cases = list(release.drift(tuning_cases, corpus, next(iter(golden_versions))).cases)
    items = release.coverage_of_golden(tuning_cases) + release.coverage_of_run(cases.values())
    try:
        bar: float | None = recorded_bar(R.read_records(run))
    except Exception:
        bar = config.grounding_bar
        notes.append("bar read from the config (the run recorded none)")
    base, snapshots, baseline_error = load_baseline(baseline_path, store)
    inputs = release.GateInputs(
        heldout_ids=heldout_ids(),
        calibration=calibration(),
        fixture_problems=fixture(),
        log_problems=log(bar),
        coverage_problems=release.coverage_problems(items, corpus, assessments),
        exposure=exposure(held_cases),
        expected_cases=expected,
        retired=retired,
        baseline=base,
        baseline_snapshots=snapshots,
        baseline_error=baseline_error,
        baseline_from_live=baseline_path is None,
        converted=converted,
    )
    result = release.verdict(run, inputs)
    result.notes += notes
    return result


def approve(store: CorpusStore, result: release.Verdict, *, config: AppConfig,
            assessments_bytes: bytes, now: Any) -> dict[str, Any]:  # fmt: skip
    """Write the live pointer for a passing verdict (the record `ask` and the pages serve)."""
    from course_assistant.answer.ask import Assistant

    return release.flip_live(store, result, rules_hash=Assistant.rules_hash(),
                             grounding_bar=config.grounding_bar, now=now,
                             settings=gated_settings(config, assessments_bytes))  # fmt: skip
