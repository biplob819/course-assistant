#!/usr/bin/env python3
"""The release gate (E2.4, Tech Spec §6.5): `make eval`, `make eval-check`, `make eval-offline`.

    uv run --env-file .env python scripts/release_gate.py eval --config CONFIG.yaml --yes
        a removal-only candidate goes live at once (no run); otherwise the held-out golden run
        (scripts/eval_live.py), then the gate; the live pointer flips only on exit 0
        (--no-flip in CI)
    uv run python scripts/release_gate.py check RUN --config CONFIG.yaml [--calibration RUN]
        the gate on an existing held-out run folder; no calls
    uv run python scripts/release_gate.py offline
        what needs no key and no private file: the regression fixture, the tuning log, drift
        and coverage of the public split on the sample course
    uv run python scripts/release_gate.py drift --config CONFIG.yaml
        kept, retired and converted golden cases for the ingested corpus (ids only)

Exit codes (EvalKit's): 0 passed, 1 a gate failed, 2 config or runtime error, 3 stopped at the
cost cap, 4 judging pending. `--json` prints the verdict as one JSON object. `gate.json` and
`offline.json` hold ids, numbers and messages only, never case text.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "scripts")]

HELDOUT_DIR = ROOT / "golden" / "heldout"  # private (git-ignored) but the MANIFEST
HELDOUT_MANIFEST = HELDOUT_DIR / "MANIFEST.json"
PUBLIC = ROOT / "golden" / "public" / "cases.jsonl"
TUNING_LOG = ROOT / "docs" / "measurements" / "tuning-log.jsonl"
DECISIONS = ROOT / "docs" / "decisions.md"
EVALREG = ROOT / "tests" / "eval" / "evalreg"
ASSESSMENTS = ROOT / "sample_course" / "assessments.yaml"
OFFLINE_OUT = ROOT / "runs" / "gate"
PUBLIC_MIN_B1, PUBLIC_MIN_B3 = 7, 0  # public half (E2.1); B3 groups sit by assessment (D-34)


def heldout_ids() -> frozenset[str]:
    from course_assistant.eval.dataset import heldout_ids as read_ids

    return read_ids(HELDOUT_MANIFEST)


def calibration_status(run_dir: Path) -> dict[str, str]:
    """`certified_calibration` of the calibration run, against the committed log."""
    from course_assistant.eval.calibrate import certified_calibration

    return certified_calibration(run_dir, log=TUNING_LOG)


def fixture_problems(fixture: Path | None = None) -> list[str]:
    """`regression_problems` on the committed fixture, rebuilt from the public split."""
    from tests.eval import scripted_eval as E

    from course_assistant.eval import dataset
    from course_assistant.eval.regression import regression_problems

    fixture = fixture or EVALREG
    try:
        meta = json.loads((fixture / "fixture.json").read_text(encoding="utf-8"))
        rows = dataset.build_rows([E.BY_ID[i] for i in meta["case_ids"]], E.CORPUS,
                                  E.GOLDEN_VERSION)  # fmt: skip
        return regression_problems(fixture, rows, corpus=E.CORPUS, config=E.CONFIG,
                                   assessments=E.ASSESSMENTS, log=TUNING_LOG)  # fmt: skip
    except Exception as exc:
        return [f"the fixture cannot be checked ({type(exc).__name__})"]


def log_problems(bar: float | None, env: Mapping[str, str], log_path: Path | None = None,
                 ) -> list[str]:  # fmt: skip
    """The tuning log: chain, continuity, the bar and ground version in use, fresh held-out,
    append-only against git (where the log is this repository's)."""
    from course_assistant.answer.ground import GroundStep
    from course_assistant.eval import tuning

    path = log_path or TUNING_LOG
    if not path.is_file():
        return ["no tuning log"]
    log = tuning.read_log(path)
    out = tuning.chain_problems(path)
    out += tuning.continuity_problems(log, DECISIONS.read_text(encoding="utf-8"))
    out += tuning.unlogged(log, bar, GroundStep.version())
    if tuning.fresh_heldout_required(log):
        out.append("the bar or support rules changed after the held-out split was scored: "
                   "write a fresh held-out set first (§5.4)")  # fmt: skip
    if path.resolve().is_relative_to(ROOT):
        out += tuning.append_only_problems(path.resolve(), ROOT, env) or []
    return out


def tracked_texts(root: Path | None = None) -> list[str]:
    """Every tracked file that is text by content (no NUL byte), whatever its suffix (D-54)."""
    from course_assistant.eval.release import text_of

    root = root or ROOT
    done = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=False)
    texts = []
    for f in done.stdout.decode("utf-8", errors="replace").split("\0"):
        path = root / f
        if f and path.is_file():
            text = text_of(path.read_bytes())
            if text is not None:
                texts.append(text)
    return texts


def private_heldout() -> list[Any] | None:
    if not (HELDOUT_DIR / "cases.jsonl").is_file():
        return None
    from course_assistant.eval.dataset import load_heldout

    return load_heldout(HELDOUT_DIR)


def exposure(held: list[Any] | None) -> list[str] | None:
    """D-54 where the private held-out file exists; None elsewhere."""
    from course_assistant.eval.release import heldout_exposure

    if held is None:
        return None
    return heldout_exposure({c.id: c.question for c in held}, tracked_texts())


def evalkit_dir(folder: Path) -> Path:
    if (folder / "run.json").is_file():
        return folder
    found = sorted((folder / "evalkit").glob("*/run.json"))
    if len(found) != 1:
        raise FileNotFoundError(f"{folder} holds {len(found)} EvalKit run folders (needs one)")
    return found[0].parent


def live_folder(run: Path) -> Path:
    """The eval_live folder around an EvalKit run folder (RUN/evalkit/<run>)."""
    return run.parent.parent if run.parent.name == "evalkit" else run


def load_course(config_path: Path, store_path: Path | None) -> tuple[Any, Any, Any]:
    from course_assistant.ingest import CorpusStore
    from course_assistant.schemas.config import load_app_config, load_assessments

    config = load_app_config(config_path)
    store = CorpusStore(store_path or config_path.parent / "corpus_store", config.cohort)
    return config, store, load_assessments(ASSESSMENTS)


def load_baseline(baseline_path: Path | None, store: Any,
                  ) -> tuple[Any, list[str], str | None]:  # fmt: skip
    """(values, snapshots, error). A first release (no baseline) is allowed only with no
    --baseline and no LIVE.json; a baseline given or expected but missing, unreadable or not
    `{"values": {...}, "snapshots": [...]}` is an error (exit 2 before the stops)."""
    from course_assistant.eval import release

    if baseline_path is not None:
        path, what = baseline_path, "the --baseline file"
    else:
        live = store.dir / release.LIVE_FILE
        if not live.exists():
            if (store.dir / release.BASELINE_FILE).exists():
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
    return data["values"], list(snaps), None


def check_run(run: Path, *, config_path: Path, store_path: Path | None,
              calibration: Path | None, baseline_path: Path | None,
              env: Mapping[str, str]) -> Any:  # fmt: skip
    """Gather every input of the verdict for one held-out run folder and apply the gate."""
    from evalkit import run as R

    from course_assistant.eval import release
    from course_assistant.eval.dataset import corpus_context, load_public, to_case
    from course_assistant.eval.release import ReleaseError
    from course_assistant.eval.tuning import recorded_bar

    config, store, assessments = load_course(config_path, store_path)
    corpus = store.current()
    if corpus is None:
        raise FileNotFoundError("no ingested corpus: run course-assistant ingest first")
    cases = R.read_cases(run)
    golden_versions = {str(c.meta.get("golden_corpus_version")) for c in cases.values()}
    drift_file = live_folder(run) / "drift.json"
    recorded = json.loads(drift_file.read_text(encoding="utf-8")) if drift_file.is_file() else {}
    retired = frozenset(str(x) for x in recorded.get("retired", ()))
    held = private_heldout()  # load_heldout: every line and the file match the MANIFEST
    notes = []
    expected: dict[str, Any] | None = None
    if held is not None and len(golden_versions) == 1:  # recompute where the cases exist
        version = next(iter(golden_versions))
        found = release.drift(held, corpus, version)
        if set(found.retired) != {i for i in retired if i in {c.id for c in held}}:
            raise ReleaseError("drift.json does not match the drift of the held-out cases on "
                               "this corpus: re-run the gate")  # fmt: skip
        retired = frozenset(found.retired)
        context = corpus_context(corpus)
        expected = {c.id: to_case(c, corpus, version, context=context) for c in found.cases}
    public = load_public(PUBLIC)
    if len(golden_versions) == 1:
        public = list(release.drift(public, corpus, next(iter(golden_versions))).cases)
    items = release.coverage_of_golden(public) + release.coverage_of_run(cases.values())
    try:
        bar: float | None = recorded_bar(R.read_records(run))
    except Exception:
        bar = config.grounding_bar
        notes.append("bar read from the config (the run recorded none)")
    base, snapshots, baseline_error = load_baseline(baseline_path, store)
    inputs = release.GateInputs(
        heldout_ids=heldout_ids(),
        calibration=calibration_status(calibration or run),
        fixture_problems=fixture_problems(),
        log_problems=log_problems(bar, env),
        coverage_problems=release.coverage_problems(items, corpus, assessments),
        exposure=exposure(held),
        expected_cases=expected,
        retired=retired,
        baseline=base,
        baseline_snapshots=snapshots,
        baseline_error=baseline_error,
    )
    result = release.verdict(run, inputs)
    result.notes += notes
    return result


def _report(result: Any, as_json: bool, out: Path | None) -> int:
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.to_json(), encoding="utf-8")
    if as_json:
        print(result.to_json(), end="")
        return int(result.exit_code)
    print(f"release gate on {result.run}: exit {result.exit_code}")
    for g in result.gates:
        print(f"  {'PASS' if g.passed else 'FAIL'} {g.name}: {g.value:.4f} over {g.n} "
              f"({g.target})")  # fmt: skip
    for kind, lines in (("FAIL", result.failures), ("ERROR", result.errors),
                        ("NOTE", result.notes)):  # fmt: skip
        for line in lines:
            print(f"  {kind} {line}")
    return int(result.exit_code)


def cmd_check(args: argparse.Namespace) -> int:
    run = evalkit_dir(args.run)
    result = check_run(run, config_path=args.config, store_path=args.store,
                       calibration=evalkit_dir(args.calibration) if args.calibration else None,
                       baseline_path=args.baseline, env=os.environ)  # fmt: skip
    return _report(result, args.json, args.out or live_folder(run) / "gate.json")


RANK = {2: 4, 3: 3, 4: 2, 1: 1, 0: 0}  # exit-code precedence: 2 > 3 > 4 > 1 > 0


def _stop_gate(path: Path, code: int, message: str) -> int:
    """gate.json for an eval-mode exit that has no verdict: the code and one fixed message."""
    path.parent.mkdir(parents=True, exist_ok=True)
    key = "errors" if code == 2 else "notes"
    data = {"exit_code": code, "run": path.parent.name, "failures": [], "errors": [], "notes": []}
    data[key] = [message]
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"release_gate: {message}", file=sys.stderr)
    return code


def cmd_eval(args: argparse.Namespace) -> int:
    """Every exit path writes gate.json: at the run folder once one exists, else in the gate
    folder, else under --out."""
    from course_assistant.answer.ask import Assistant
    from course_assistant.eval.release import apply_removals, flip_live

    args.gate_json = args.out / "gate.json"
    if args.split != "heldout":
        return _stop_gate(args.gate_json, 2, "the release gate runs on the held-out split (§6.5)")
    config, store, _ = load_course(args.config, args.store)
    now = dt.datetime.now(dt.UTC)
    if not args.no_flip and apply_removals(store, now=now):
        return _stop_gate(args.gate_json, 0, "the candidate only removes units: live at once, "
                          "no gate (§2.3)")  # fmt: skip
    import eval_live

    out = args.out / f"gate-{now.strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir(parents=True, exist_ok=False)
    args.gate_json = out / "gate.json"
    argv = ["--config", str(args.config), "--split", "heldout", "--max-cost", str(args.max_cost),
            "--out", str(out)]  # fmt: skip
    if args.store:
        argv += ["--store", str(args.store)]
    if args.yes:
        argv.append("--yes")
    live_code = eval_live.main(argv)
    runs = sorted(out.glob("course-ta-heldout-*/evalkit/*/run.json"))
    if not runs:  # refused (2) or stopped (3) before EvalKit wrote a run folder
        code = live_code if live_code in (2, 3) else 2
        return _stop_gate(args.gate_json, code, f"eval_live exited {live_code} before a run "
                          "folder was written: nothing to gate")  # fmt: skip
    if len(runs) != 1:
        return _stop_gate(args.gate_json, 2, f"expected one run folder, found {len(runs)}")
    run = runs[0].parent
    args.gate_json = live_folder(run) / "gate.json"
    result = check_run(run, config_path=args.config, store_path=args.store,
                       calibration=evalkit_dir(args.calibration) if args.calibration else None,
                       baseline_path=None, env=os.environ)  # fmt: skip
    if live_code and RANK.get(live_code, 4) > RANK.get(result.exit_code, 4):
        # eval_live's own code outranks the verdict's (precedence 2 > 3 > 4 > 1 > 0)
        result.exit_code = live_code if live_code in RANK else 2
        (result.errors if result.exit_code == 2 else result.notes).append(
            f"eval_live exited {live_code}"
        )
    elif live_code:
        result.notes.append(f"eval_live exited {live_code}")
    code = _report(result, args.json, args.gate_json)
    if result.exit_code == 0 and live_code == 0 and not args.no_flip:
        state = flip_live(store, result, rules_hash=Assistant.rules_hash(),
                          grounding_bar=config.grounding_bar, now=now)  # fmt: skip
        print(f"live corpus version is now {state['corpus_version'][:12]}", file=sys.stderr)
    return code


def offline(env: Mapping[str, str]) -> dict[str, Any]:
    """The offline gate: the parts that need no key and no private file."""
    from tests.eval import scripted_eval as E

    from course_assistant.eval import release
    from course_assistant.eval.dataset import load_public

    public = load_public(PUBLIC)
    found = release.drift(public, E.CORPUS, E.GOLDEN_VERSION)
    drifted = []
    if found.retired or found.converted:
        drifted.append(f"{len(found.retired)} retired and {len(found.converted)} converted "
                       "public case(s) on the sample course: update the golden set")  # fmt: skip
    items = release.coverage_of_golden(found.cases)
    coverage = release.coverage_problems(items, E.CORPUS, E.ASSESSMENTS, min_b1=PUBLIC_MIN_B1,
                                         min_b3=PUBLIC_MIN_B3)  # fmt: skip
    problems = {
        "fixture": fixture_problems(),
        "tuning_log": log_problems(None, env),
        "drift": drifted,
        "coverage": coverage,
    }
    failed = any(problems.values())
    return {"exit_code": 1 if failed else 0, "problems": problems,
            "corpus_version": E.CORPUS.corpus_version, "public_cases": len(public)}  # fmt: skip


def cmd_offline(args: argparse.Namespace) -> int:
    result = offline(os.environ)
    out = args.out or OFFLINE_OUT / "offline.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=1, sort_keys=True) + "\n"
    out.write_text(text, encoding="utf-8")
    if args.json:
        print(text, end="")
    else:
        print(f"offline release gate: exit {result['exit_code']}")
        for part, lines in result["problems"].items():
            for line in lines:
                print(f"  FAIL {part}: {line}")
    return int(result["exit_code"])


def cmd_drift(args: argparse.Namespace) -> int:
    from course_assistant.eval import release
    from course_assistant.eval.dataset import golden_version_for, load_public
    from course_assistant.schemas.config import load_manifest

    _, store, _ = load_course(args.config, args.store)
    corpus = store.current()
    if corpus is None:
        raise FileNotFoundError("no ingested corpus: run course-assistant ingest first")
    version = golden_version_for(corpus, load_manifest(ROOT / "sample_course" / "manifest.yaml"),
                                 ROOT)  # fmt: skip
    cases = load_public(PUBLIC) + (private_heldout() or [])
    record = release.drift(cases, corpus, version).record(corpus.corpus_version)
    print(json.dumps(record, indent=1, sort_keys=True))
    return 0


def _recover_out(argv: list[str]) -> Path | None:
    """--out's value from a command line argparse refused: `--out X`, `--out=X` and the
    unambiguous abbreviations `--o`, `--ou` (no other option starts with --o)."""
    for i, tok in enumerate(argv):
        name, eq, value = tok.partition("=")
        if len(name) >= 3 and "--out".startswith(name):
            if eq:
                return Path(value) if value else None
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                return Path(argv[i + 1])
    return None


def _argv_error_gate(argv: list[str]) -> None:
    """A command line that does not parse still leaves gate.json (exit 2). For eval: always
    runs/gate.json, and under --out too when it can be recovered; for check: --out when it can
    be recovered."""
    cmd = next((t for t in argv if t in ("eval", "check")), None)
    given = _recover_out(argv)
    targets: list[Path] = []
    if cmd == "eval":
        targets.append(ROOT / "runs" / "gate.json")
        if given is not None:
            targets.append(given / "gate.json")
    elif cmd == "check" and given is not None:
        targets.append(given)
    for out in dict.fromkeys(targets):
        with contextlib.suppress(OSError):
            _stop_gate(out, 2, "the command line could not be parsed")


def main(argv: list[str] | None = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("eval")
    e.add_argument("--config", required=True, type=Path)
    e.add_argument("--split", default="heldout")
    e.add_argument("--max-cost", type=float, default=40.0)
    e.add_argument("--calibration", type=Path, help="the calibration run (default: this run)")
    e.add_argument("--no-flip", action="store_true", help="report only (CI)")
    e.add_argument("--yes", action="store_true", help="confirm that this run spends money")
    e.add_argument("--out", type=Path, default=ROOT / "runs")
    e.set_defaults(func=cmd_eval)
    c = sub.add_parser("check")
    c.add_argument("run", type=Path)
    c.add_argument("--config", required=True, type=Path)
    c.add_argument("--calibration", type=Path, help="the calibration run (default: RUN)")
    c.add_argument("--baseline", type=Path, help="per-case baseline (default: the live one)")
    c.add_argument("--out", type=Path, help="gate.json path (default: next to the run)")
    c.set_defaults(func=cmd_check)
    o = sub.add_parser("offline")
    o.add_argument("--out", type=Path, help=f"result file (default {OFFLINE_OUT}/offline.json)")
    o.set_defaults(func=cmd_offline)
    d = sub.add_parser("drift")
    d.add_argument("--config", required=True, type=Path)
    d.set_defaults(func=cmd_drift)
    for p in (e, c, o, d):
        p.add_argument("--json", action="store_true", help="one JSON object on stdout")
        if p is not o:
            p.add_argument("--store", type=Path, help="corpus store folder (default: by config)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        if not exc.code:  # --help
            raise
        _argv_error_gate(argv_list)
        return 2
    from course_assistant.eval.dataset import DatasetError
    from course_assistant.eval.release import ReleaseError

    try:
        return int(args.func(args))
    except (ReleaseError, DatasetError, FileNotFoundError) as exc:
        message = str(exc)  # our own errors name ids and counts, never text
    except Exception as exc:  # anything else may quote input: its class only, and exit 2
        message = f"the gate could not run ({type(exc).__name__})"
    print(f"release_gate: {message}", file=sys.stderr)
    _error_gate(args, message)
    return 2


def _error_gate(args: argparse.Namespace, message: str) -> None:
    """A gate.json for a run that could not be gated: exit 2 and a fixed message, so nothing
    reads a crash as a failed or passed gate."""
    if args.cmd == "eval":  # the run's folder once known, else the gate folder, else --out
        out = getattr(args, "gate_json", None) or args.out / "gate.json"
    elif args.cmd != "check":
        return
    else:
        out = args.out
    if out is None:
        try:
            out = live_folder(evalkit_dir(args.run)) / "gate.json"
        except Exception:
            out = args.run / "gate.json" if args.run.is_dir() else None
    if out is None:
        return
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"exit_code": 2, "run": out.parent.name, "errors": [message],
                                   "failures": [], "notes": []}, indent=1, sort_keys=True)
                       + "\n", encoding="utf-8")  # fmt: skip
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
