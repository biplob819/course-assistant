"""Command line entry point. Make targets call these subcommands (Tech Spec §4).

Exit codes: 0 passed, 1 a gate failed (a replay mismatch, a failed doctor check, a failed
evaluation threshold, a purge with overdue deletions), 2 config or runtime error (including a
failed reconcile, an unusable `init` folder or bad arguments), 3 stopped by a spend cap, 4
judging pending.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from course_assistant import __version__

if TYPE_CHECKING:
    from course_assistant.ingest import VectorStoreAPI

EXIT_OK = 0
EXIT_GATE = 1
EXIT_CONFIG = 2
EXIT_COST_CAP = 3
EXIT_PENDING = 4
# `ask`: the most one question may cost, the hosted demo's per-request hold (D3.1 GuardConfig)
QUESTION_HOLD_USD = 0.50

# `init` (R3.1 OSS-02): packaged template name -> file written in the course folder
INIT_FILES = (
    ("config.yaml", "config.yaml"),
    ("manifest.yaml", "manifest.yaml"),
    ("assessments.yaml", "assessments.yaml"),
    ("env.example", ".env.example"),
)
# `doctor` (R3.1 OSS-03)
VECTOR_STORE_ID = re.compile(r"vs_[A-Za-z0-9]+")
MIN_PYTHON = (3, 11)
EXTRAS = (  # extra, modules it installs, whether doctor fails without it
    ("openai", ("openai",), True),
    ("ingest", ("pypdf", "pptx"), False),
    ("eval", ("evalkit", "anthropic"), False),
    ("app", ("streamlit",), False),
)
REQUIRED_SECRETS = ("OPENAI_API_KEY",)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="course-assistant", description=__doc__)
    ap.add_argument("--version", action="version", version=f"course-assistant {__version__}")
    sub = ap.add_subparsers(dest="command")
    ing = sub.add_parser(
        "ingest",
        help="sync the cohort's vector store with the manifest",
        description="Validate the manifest, split, diff, upload and delete, reconcile. "
        "Paths default to the config file's folder.",
    )
    ing.add_argument("--config", required=True, type=Path, help="config.yaml")
    ing.add_argument("--manifest", type=Path, help="default: manifest.yaml next to the config")
    ing.add_argument("--root", type=Path, help="corpus root the manifest paths are relative to")
    ing.add_argument("--store", type=Path, help="corpus store folder (default: corpus_store/)")
    ing.add_argument("--dry-run", action="store_true", help="print the plan; make no API call")
    rep = sub.add_parser(
        "eval-replay",
        help="replay the golden requests through ask() with no API calls",
        description="Replay the recorded sample-course requests through ask() and compare "
        "each Answer with the stored one. Exit 0 when all match, 1 on a mismatch.",
    )
    rep.add_argument("--fixture", type=Path, help="default: the packaged golden replay file")
    rep.add_argument("--json", action="store_true", help="machine-readable output")
    ini = sub.add_parser(
        "init",
        help="write starter config, manifest, assessments and .env.example for a new course",
        description="Write config.yaml, manifest.yaml, assessments.yaml and .env.example into "
        "DIR. An existing file is never overwritten. No network call.",
    )
    ini.add_argument("dir", nargs="?", default=Path("."), type=Path, help="default: .")
    doc = sub.add_parser(
        "doctor",
        help="check Python, extras, config, secrets (names only), vector store id and corpus",
        description="Offline checks of a course folder. Exit 0 when nothing fails (warnings "
        "allowed), 1 when a check fails. Never prints a secret value; makes no network call.",
    )
    doc.add_argument("--config", type=Path, default=Path("config.yaml"), help="config.yaml")
    doc.add_argument("--manifest", type=Path, help="default: manifest.yaml next to the config")
    doc.add_argument("--root", type=Path, help="corpus root (default: the config's folder)")
    ask = sub.add_parser(
        "ask",
        help="ask one question of an ingested course (spends money; needs OPENAI_API_KEY)",
        description="Send one question through the full answer path (route, answer, grounding "
        "and integrity checks, hint) and print the reply with its status and sources. Serves "
        "the gate-approved corpus unless --ungated. Each question is capped at "
        f"${QUESTION_HOLD_USD:.2f} and refused when the month's ledger cannot cover it. "
        "Keeps no history and never prints the key.",
    )
    ask.add_argument("question", help="the question, in quotes")
    ask.add_argument("--config", required=True, type=Path, help="config.yaml")
    ask.add_argument("--session", help="limit the answer to one session (s01, s02, ...)")
    ask.add_argument("--assessments", type=Path,
                     help="default: assessments.yaml next to the config")  # fmt: skip
    ask.add_argument("--store", type=Path, help="corpus store folder (default: corpus_store/)")
    ask.add_argument("--ledger", type=Path, default=Path("runs") / "ledger.jsonl",
                     help="spend ledger (default: runs/ledger.jsonl)")  # fmt: skip
    ask.add_argument(
        "--ungated",
        action="store_true",
        help="answer from the latest ingest before the release gate approves it",
    )
    ask.add_argument("--json", action="store_true", help="machine-readable output")
    ev = sub.add_parser(
        "eval",
        help="evaluate your course on your own golden cases (spends money; eval extra)",
        description="Run your golden cases (one split per file: tuning or held-out) through "
        "the assistant with EvalKit and the probe judges. A held-out run goes through the "
        "release gate (thresholds, certified calibration in your tuning log, the tuning log "
        "itself, coverage, exposure, drift and the last live run) and, on a pass, approves the "
        "course version that `ask` and the pages serve. A tuning run checks the thresholds "
        "only and never approves. --dry-run writes the run folder and prints the estimate with "
        "no key and no call. Exit 0 pass, 1 a gate fails, 2 error, 3 cost cap, 4 judging "
        "pending.",
    )
    ev.add_argument("--config", required=True, type=Path, help="config.yaml (ingested)")
    ev.add_argument("--cases", required=True, type=Path, help="your golden cases (JSONL)")
    ev.add_argument("--manifest", type=Path, help="default: manifest.yaml next to the config")
    ev.add_argument("--root", type=Path, help="corpus root (default: the config's folder)")
    ev.add_argument("--store", type=Path, help="corpus store folder (default: corpus_store/)")
    ev.add_argument("--assessments", type=Path,
                    help="default: assessments.yaml next to the config")  # fmt: skip
    ev.add_argument("--solutions", type=Path,
                    help="folder of <assessment_id>.md model solutions, read only by the "
                    "integrity judge (needed for graded-work cases; keep it private)")  # fmt: skip
    ev.add_argument("--out", type=Path, default=Path("runs"), help="default: runs/")
    ev.add_argument("--ledger", type=Path, default=Path("runs") / "ledger.jsonl",
                    help="spend ledger (default: runs/ledger.jsonl)")  # fmt: skip
    ev.add_argument("--max-cost", type=float, default=40.0, help="stop if the estimate is over")
    ev.add_argument("--live-judges", action="store_true",
                    help="judge at full price now instead of provider batches")  # fmt: skip
    ev.add_argument("--dry-run", action="store_true", help="write the folder, call nothing")
    ev.add_argument("--yes", action="store_true", help="confirm that this run spends money")
    ev.add_argument("--no-approve", action="store_true",
                    help="report only: never approve the course version (CI)")  # fmt: skip
    chk = sub.add_parser(
        "eval-check",
        help="the release gate on a finished `eval` run folder (no calls, never approves)",
        description="A held-out run (with --config and --cases) gets the full release gate; a "
        "tuning run gets the thresholds only. Never approves. Exit 0 pass, 1 a gate fails, 2 "
        "error, 3 cost cap, 4 judging pending.",
    )
    chk.add_argument("run", type=Path, help="the run folder")
    chk.add_argument("--config", type=Path, help="config.yaml (needed for a held-out run)")
    chk.add_argument("--cases", type=Path, help="the held-out cases file the run was built from")
    chk.add_argument("--store", type=Path, help="corpus store folder (default: corpus_store/)")
    chk.add_argument("--assessments", type=Path,
                     help="default: assessments.yaml next to the config")  # fmt: skip
    chk.add_argument("--baseline", type=Path, help="per-case baseline (default: the live one)")
    chk.add_argument("--json", action="store_true", help="machine-readable output")
    for p in (ev, chk):
        p.add_argument("--public", type=Path,
                       help="your tuning-split cases (counted for the coverage rule)")  # fmt: skip
        p.add_argument("--tuning-log", type=Path,
                       help="default: tuning-log.jsonl next to the config")  # fmt: skip
        p.add_argument("--decisions", type=Path,
                       help="the decisions the tuning log cites (default: decisions.md next to "
                       "the config)")  # fmt: skip
        p.add_argument("--calibration", type=Path,
                       help="the run that certified the judges (default: this run)")  # fmt: skip
    pur = sub.add_parser(
        "purge",
        help="apply retention to the app store and retry open trace deletions (deletes)",
        description="Retry trace deletion for every open deletion request on the app's "
        "trace.jsonl, then delete conversation text after pilot end + 30 days, consent records "
        "after pilot end + 1 year and access-log rows after 1 year. Refuses without --yes and "
        "never creates a store. Exit 1 when a deletion request is incomplete after 7 days.",
    )
    pur.add_argument("--app-dir", required=True, type=Path,
                     help="the folder with app.sqlite and trace.jsonl (CA_APP_DIR)")  # fmt: skip
    pur.add_argument("--pilot-end", required=True, type=_date,
                     help="the pilot's last day, YYYY-MM-DD")  # fmt: skip
    pur.add_argument("--yes", action="store_true", help="confirm the deletions")
    sub.add_parser(
        "passkey-hash",
        help="turn the demo passcode into the salted hash for the secret store",
        description="Read a 6-12 digit passcode twice without echo and print only its salted "
        "PBKDF2 hash, to paste into the app's secret store as DEMO_PASSKEY_HASH. Writes no "
        "file and makes no network call; changing the hash revokes every unlocked session.",
    )
    return ap


def _init(target: Path) -> int:
    """Write the starter files; keep every file that exists (L-3)."""
    from importlib.resources import files

    if target.exists() and not target.is_dir():
        print(f"init: {target} is not a folder", file=sys.stderr)
        return EXIT_CONFIG
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"init: cannot create {target} ({exc.strerror})", file=sys.stderr)
        return EXIT_CONFIG
    templates = files("course_assistant") / "templates"
    for src, dest in INIT_FILES:
        path = target / dest
        if path.exists() or path.is_symlink():
            print(f"kept {dest} (exists)")
            continue
        data = (templates / src).read_bytes()
        try:
            with path.open("xb") as fh:  # exclusive create: never clobbers a file
                fh.write(data)
        except FileExistsError:
            print(f"kept {dest} (exists)")
            continue
        except OSError as exc:
            print(f"init: cannot write {dest} ({exc.strerror})", file=sys.stderr)
            return EXIT_CONFIG
        print(f"wrote {dest}")
    print("next: add your files to manifest.yaml, set the vector store id, then run "
          "`course-assistant doctor`")  # fmt: skip
    return EXIT_OK


@dataclass(frozen=True)
class Check:
    level: str  # "ok" | "warn" | "FAIL"
    check: str
    message: str

    def line(self) -> str:
        return f"{self.level:<4} {self.check}: {self.message}"


def _python_version() -> tuple[int, int, int]:
    v = sys.version_info
    return (v.major, v.minor, v.micro)


def _has_module(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _dotenv_names(path: Path) -> set[str]:
    """Names with a non-empty value in a .env file. Values are read only to test emptiness."""
    names: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return names
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        if value.strip().strip("'\"").strip():
            names.add(name.strip())
    return names


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def doctor_report(
    *,
    config: Path | None,
    manifest: Path | None,
    root: Path | None,
    env: Mapping[str, str],
) -> list[Check]:
    """Every doctor check, offline. Messages are fixed text plus file and secret names."""
    from course_assistant.schemas import ConfigError, load_app_config, load_manifest

    out: list[Check] = []
    py = _python_version()
    want = ".".join(map(str, MIN_PYTHON))
    out.append(Check("ok", "python", f"{py[0]}.{py[1]}.{py[2]}") if py[:2] >= MIN_PYTHON
               else Check("FAIL", "python", f"Python {want} or newer is required"))  # fmt: skip

    for extra, modules, required in EXTRAS:
        if all(_has_module(m) for m in modules):
            out.append(Check("ok", "extras", f"{extra} installed"))
        else:
            hint = f"{extra} not installed: pip install 'course-assistant[{extra}]'"
            out.append(Check("FAIL" if required else "warn", "extras", hint))

    base = config.parent if config is not None else (manifest.parent if manifest else Path("."))
    in_dotenv = _dotenv_names(base / ".env")
    for name in REQUIRED_SECRETS:
        if env.get(name, "").strip():
            out.append(Check("ok", "secrets", f"{name} is set"))
        elif name in in_dotenv:
            out.append(Check("FAIL", "secrets", f"{name} is in .env but not in the environment; "
                             "export it or set it in your host's secret store"))  # fmt: skip
        else:
            out.append(Check("FAIL", "secrets", f"{name} is not set"))
    if _has_module("evalkit"):
        # only the keys the configured judge panel needs (OWNER D-80: OpenAI only, no warning)
        from course_assistant.eval import panel

        for name in sorted(panel.needed_keys() - set(REQUIRED_SECRETS)):
            if not env.get(name, "").strip():
                out.append(Check("warn", "secrets", f"{name} is not set (needed only for "
                                 "evaluation: a configured judge uses that provider)"))  # fmt: skip

    cohort = None
    if config is not None:
        try:
            cfg = load_app_config(config)
        except ConfigError as exc:
            # ConfigError names the file and schema field only, never file content (D-29)
            out.append(Check("FAIL", "config", str(exc)))
        else:
            cohort = cfg.cohort
            out.append(Check("ok", "config", f"{config.name} is valid (cohort {cfg.cohort})"))
            if VECTOR_STORE_ID.fullmatch(cfg.vector_store_id):
                out.append(Check("ok", "vector_store", "id format is valid (not contacted)"))
            else:
                out.append(Check("FAIL", "vector_store", "vector_store_id is not a vector "
                                 "store id (vs_ then letters and digits); paste the id of the "
                                 "store in your OpenAI project"))  # fmt: skip

    man_path = manifest if manifest is not None else base / "manifest.yaml"
    corpus_root = root if root is not None else base
    try:
        man = load_manifest(man_path)
    except ConfigError as exc:
        out.append(Check("FAIL", "corpus", str(exc)))
        return out
    if cohort is not None and man.cohort != cohort:
        out.append(Check("FAIL", "corpus", f"{man_path.name} cohort is not the config cohort"))
        return out
    missing = [e.path for e in man.entries if not (corpus_root / e.path).is_file()]
    if missing:
        out.append(Check("FAIL", "corpus", f"{len(missing)} of {len(man.entries)} files missing "
                         f"under the corpus root, first: {missing[0]}"))  # fmt: skip
        return out
    changed = [e.path for e in man.entries if _sha256(corpus_root / e.path) != e.sha256]
    if changed:
        out.append(Check("FAIL", "corpus", f"{len(changed)} files differ from their manifest "
                         f"sha256, first: {changed[0]}"))  # fmt: skip
        return out
    out.append(Check("ok", "corpus", f"{len(man.entries)} files present and matching"))
    return out


def _doctor(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> int:
    report = doctor_report(
        config=args.config,
        manifest=args.manifest,
        root=args.root,
        env=os.environ if env is None else env,
    )
    for c in report:
        print(c.line())
    failed = sum(c.level == "FAIL" for c in report)
    print(f"doctor: {failed} failed" if failed else "doctor: ready")
    return EXIT_GATE if failed else EXIT_OK


def _openai_client() -> object:
    """The real OpenAI client (the key comes from the environment); tests replace this."""
    import openai

    return openai.OpenAI()


def _ask(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> int:
    from course_assistant.answer import responder
    from course_assistant.answer.ask import Assistant
    from course_assistant.answer.replay import Live
    from course_assistant.demo.guard import CappedSource
    from course_assistant.ingest import CorpusStore, IngestError
    from course_assistant.ingest.corpus import VERSION_KEY
    from course_assistant.ingest.serving import ServedCorpus, ServingError
    from course_assistant.llm import Ledger, load_prices
    from course_assistant.schemas import ConfigError, load_app_config
    from course_assistant.schemas.config import load_assessments
    from course_assistant.store.db import Store

    env = os.environ if env is None else env
    if not env.get("OPENAI_API_KEY", "").strip():
        print("ask: OPENAI_API_KEY is not set (export it or set it in your host's secret store)",
              file=sys.stderr)  # fmt: skip
        return EXIT_CONFIG
    base = args.config.parent
    try:
        config = load_app_config(args.config)
        assessments = load_assessments(args.assessments or base / "assessments.yaml")
        store = CorpusStore(args.store or base / "corpus_store", config.cohort)
        if args.ungated:
            corpus, key = store.current(), VERSION_KEY
            if corpus is None:
                raise ConfigError(f"no corpus; run course-assistant ingest --config "
                                  f"{args.config} first")  # fmt: skip
        else:
            corpus, key = ServedCorpus(store).read()
    except ServingError as exc:
        print(f"ask: {exc} (pass --ungated to try the latest ingest before the release gate)",
              file=sys.stderr)  # fmt: skip
        return EXIT_CONFIG
    except (ConfigError, IngestError) as exc:
        print(f"ask: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.session is not None and args.session not in {u.session for u in corpus.units}:
        print(f"ask: session {args.session} is not in the served corpus", file=sys.stderr)
        return EXIT_CONFIG
    try:
        ledger = Ledger(args.ledger, config.monthly_budget_usd)
        spent = ledger.month_total()
    except (OSError, ValueError) as exc:
        print(f"ask: cannot read the spend ledger ({type(exc).__name__})", file=sys.stderr)
        return EXIT_CONFIG
    if spent + QUESTION_HOLD_USD > config.monthly_budget_usd:
        print(f"ask: this month's spend ${spent:.2f} leaves less than one question "
              f"(${QUESTION_HOLD_USD:.2f}) of the ${config.monthly_budget_usd:.2f} monthly "
              "budget", file=sys.stderr)  # fmt: skip
        return EXIT_COST_CAP
    try:
        client = _openai_client()
    except ImportError:
        print("ask: openai is not installed: pip install 'course-assistant[openai]'",
              file=sys.stderr)  # fmt: skip
        return EXIT_CONFIG
    import uuid

    prices = load_prices()
    live = Live(responder(client=client, prices=prices, ledger=ledger, run_id="ask"))
    source = CappedSource(live, QUESTION_HOLD_USD, prices)
    assistant = Assistant(config, corpus, assessments, Store(":memory:"), source,
                          version_key=key)  # fmt: skip
    try:
        answer = assistant.ask(args.question, uuid.uuid4().hex[:16], args.session)
    except ValueError as exc:  # the question itself (empty or too long)
        print(f"ask: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    cites = [{"label": c.label, "session": c.session, "file_id": c.file_id}
             for c in answer.citations]  # fmt: skip
    if args.json:
        import json

        print(json.dumps({"status": answer.status, "text": answer.text, "citations": cites,
                          "cost_usd": answer.cost_usd, "latency_ms": answer.latency_ms,
                          "gated": not args.ungated}))  # fmt: skip
    else:
        note = "  (latest ingest, not gate-approved)" if args.ungated else ""
        print(f"status: {answer.status}{note}\n\n{answer.text}")
        if cites:
            print("\nsources:")
            for n, c in enumerate(cites, start=1):
                print(f"  [{n}] {c['label']}")
        print(f"\ncost: ${answer.cost_usd:.4f}  latency: {answer.latency_ms / 1000:.1f} s")
    return EXIT_CONFIG if answer.status == "error" else EXIT_OK


def _eval_runtime(config: object, corpus: object, assessments: object, folder: Path,
                  ledger_path: Path, budget: object) -> tuple[object, object, object]:  # fmt: skip
    """(system runtime, probe judge panel, recorder) for a live run; tests replace this."""
    from evalkit.cost import load_prices as evalkit_prices

    from course_assistant.answer import responder
    from course_assistant.answer.replay import Recorder
    from course_assistant.eval import system
    from course_assistant.eval.judges import build_panel
    from course_assistant.eval.panel import JUDGES
    from course_assistant.llm import JsonlSink, Ledger, load_prices

    cfg: Any = config
    ledger = Ledger(ledger_path, cfg.monthly_budget_usd)
    live = responder(client=_openai_client(), prices=load_prices(), ledger=ledger,
                     sink=JsonlSink(folder / "trace.jsonl"), run_id=folder.name)  # fmt: skip
    recorder = Recorder(live)
    runtime = system.Runtime(config=cfg, corpus=corpus, assessments=assessments,  # type: ignore[arg-type]
                             source=recorder)  # fmt: skip
    panel = build_panel(JUDGES, evalkit_prices(), cache=folder / "probe-verdicts.jsonl",
                        budget=budget)  # fmt: skip
    return runtime, panel, recorder


def _verdict(folder: Path, args: argparse.Namespace, env: Mapping[str, str],
             course_data: tuple[Any, Any, Any] | None = None) -> Any:  # fmt: skip
    """The verdict on a finished run: the full release gate for a held-out run, the
    thresholds alone (never an approval) for a tuning run. `course_data` is (config, store,
    assessments), loaded from the arguments when not given."""
    from course_assistant.eval import course

    run_dir = course.evalkit_run_dir(folder)
    thresholds = course.course_gate(run_dir)
    if thresholds.split != "heldout":
        thresholds.notes.append("a tuning run never approves a course version")
        return thresholds
    if args.config is None or args.cases is None:
        raise course.CourseEvalError("a held-out run needs --config and --cases (the held-out "
                                     "file) for the release gate")  # fmt: skip
    if course_data is None:
        from course_assistant.ingest import CorpusStore
        from course_assistant.schemas import load_app_config
        from course_assistant.schemas.config import load_assessments

        config = load_app_config(args.config)
        store = CorpusStore(args.store or args.config.parent / "corpus_store", config.cohort)
        assessments = load_assessments(args.assessments or args.config.parent / "assessments.yaml")
        course_data = (config, store, assessments)
    config, store, assessments = course_data
    files = course.CourseFiles.beside(
        args.config,
        args.cases,
        public=args.public,
        tuning_log=args.tuning_log,
        decisions=args.decisions,
    )
    calibration = course.evalkit_run_dir(args.calibration) if args.calibration else None
    return course.course_check(run_dir, config=config, store=store, assessments=assessments,
                               files=files, calibration_run=calibration,
                               baseline_path=getattr(args, "baseline", None), env=env)  # fmt: skip


def _gate_report(folder: Path, args: argparse.Namespace, env: Mapping[str, str] | None = None,
                 course_data: tuple[Any, Any, Any] | None = None) -> tuple[int, Any]:  # fmt: skip
    """(exit code, verdict or None). Writes gate.json next to the run; prints fixed lines."""
    from course_assistant.eval import course, gate
    from course_assistant.eval.dataset import DatasetError
    from course_assistant.eval.release import ReleaseError
    from course_assistant.schemas import ConfigError

    env = os.environ if env is None else env
    try:
        verdict = _verdict(folder, args, env, course_data)
    except (course.CourseEvalError, ReleaseError, DatasetError, ConfigError,
            FileNotFoundError) as exc:  # fmt: skip
        print(f"{args.command}: {exc}", file=sys.stderr)  # our errors name ids, never text
        return EXIT_CONFIG, None
    except Exception as exc:  # anything else may quote input: its class only
        print(f"{args.command}: the gate could not run ({type(exc).__name__})", file=sys.stderr)
        return EXIT_CONFIG, None
    out = gate.live_folder(course.evalkit_run_dir(folder)) / "gate.json"
    out.write_text(verdict.to_json(), encoding="utf-8")
    as_json = getattr(args, "json", False)
    print(verdict.to_json(), end="") if as_json else print(course.render(verdict))
    return int(verdict.exit_code), verdict


RANK = {2: 4, 3: 3, 4: 2, 1: 1, 0: 0}  # exit-code precedence, as the release gate: 2 > 3 > 4 > 1


def _eval(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> int:
    import datetime as dt

    try:
        from course_assistant.eval import course, panel
        from course_assistant.eval.dataset import DatasetError
        from course_assistant.eval.judges import DirSolutionBook
    except ImportError as exc:
        print(f"eval: {exc.name} is not installed: pip install 'course-assistant[eval]'",
              file=sys.stderr)  # fmt: skip
        return EXIT_CONFIG
    from course_assistant.ingest import CorpusStore, IngestError
    from course_assistant.schemas import ConfigError, load_app_config, load_manifest
    from course_assistant.schemas.config import load_assessments

    env = os.environ if env is None else env
    base = args.config.parent
    assessments_path = args.assessments or base / "assessments.yaml"
    try:
        config = load_app_config(args.config)
        assessments = load_assessments(assessments_path)
        manifest = load_manifest(args.manifest or base / "manifest.yaml")
        cases, split = course.load_cases(args.cases)
        store = CorpusStore(args.store or base / "corpus_store", config.cohort)
        corpus = store.current()
        if corpus is None:
            raise ConfigError(f"no corpus; run course-assistant ingest --config {args.config} "
                              "first")  # fmt: skip
        if manifest.cohort != config.cohort:
            raise ConfigError("the manifest cohort is not the config cohort")
    except (ConfigError, IngestError, course.CourseEvalError) as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if split == "heldout" and not args.dry_run and not args.no_approve:
        # as the repo's gate: a candidate that only removes units goes live at once (§2.3)
        from course_assistant.eval import gate
        from course_assistant.eval.release import apply_removals

        settings = gate.gated_settings(config, assessments_path.read_bytes())
        if apply_removals(store, now=dt.datetime.now(dt.UTC), settings=settings):
            print("eval: the candidate only removes units: live at once, no gate (§2.3)")
            return EXIT_OK
    graded = any(c.rule == "B3" for c in cases)
    if graded and args.solutions is not None and not args.solutions.is_dir():
        print(f"eval: --solutions {args.solutions.name} is not a folder", file=sys.stderr)
        return EXIT_CONFIG
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = args.out / f"course-ta-{split}-{stamp}"
    try:
        task = course.prepare(folder, cases, corpus, manifest, args.root or base)
    except DatasetError as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    cost = course.estimate(cases)
    print(f"course-ta: {len(cases)} {split} cases ({course.probes(cases)} probes) -> {folder}")
    print(f"estimate: ${cost:.2f} (cap ${args.max_cost:.2f})")
    if args.dry_run:
        return EXIT_OK
    if not args.yes:
        print("eval: this run makes paid API calls; pass --yes to confirm", file=sys.stderr)
        return EXIT_CONFIG
    missing = sorted(k for k in panel.needed_keys() if not env.get(k, "").strip())
    if missing:
        print(f"eval: {', '.join(missing)} not set", file=sys.stderr)
        return EXIT_CONFIG
    if graded and args.solutions is None:
        print("eval: graded-work (B3) cases need --solutions DIR (the model solutions the "
              "integrity judge reads; keep that folder private)", file=sys.stderr)  # fmt: skip
        return EXIT_CONFIG
    files = course.CourseFiles.beside(
        args.config,
        args.cases,
        public=args.public,
        tuning_log=args.tuning_log,
        decisions=args.decisions,
    )
    heldout = split == "heldout"
    if heldout:
        problem = course.heldout_problem(files, config.grounding_bar)
        if problem is not None:
            print(f"eval: {problem}", file=sys.stderr)
            return EXIT_CONFIG
    try:
        budget = course.plan_budget(cases, args.max_cost)
    except ValueError as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return EXIT_COST_CAP
    runtime, judges, recorder = _eval_runtime(config, corpus, assessments, folder, args.ledger,
                                              budget)  # fmt: skip
    solutions = DirSolutionBook(args.solutions) if args.solutions else course.NoSolutions()
    try:
        code = course.run(folder, task, n=course.live_cases(task), budget=budget,
                          runtime=runtime, panel=judges, solutions=solutions,
                          assessments=assessments, live_judges=args.live_judges)  # fmt: skip
    finally:
        rows = [r.model_dump_json() for r in getattr(recorder, "recordings", [])]
        (folder / "recordings.jsonl").write_text("".join(x + "\n" for x in rows),
                                                 encoding="utf-8")  # fmt: skip
        if heldout:  # every run that scores the held-out split is logged (as eval_live)
            from course_assistant.answer.ground import GroundStep
            from course_assistant.eval.tuning import log_heldout_scored

            log_heldout_scored(files.tuning_log, run=folder.name, bar=config.grounding_bar,
                               ground_version=GroundStep.version())  # fmt: skip
    print(f"evalkit exit {code}")
    if not list((folder / "evalkit").glob("*/run.json")):
        return code if code in (EXIT_CONFIG, EXIT_COST_CAP) else EXIT_CONFIG
    result, verdict = _gate_report(folder, args, env, course_data=(config, store, assessments))
    if code and RANK.get(code, 4) > RANK.get(result, 4):
        print(f"eval: EvalKit exited {code}, which outranks the verdict", file=sys.stderr)
        return code if code in RANK else EXIT_CONFIG
    if heldout and result == EXIT_OK and code == EXIT_OK and not args.no_approve:
        from course_assistant.eval import gate

        state = gate.approve(store, verdict, config=config,
                             assessments_bytes=assessments_path.read_bytes(),
                             now=dt.datetime.now(dt.UTC))  # fmt: skip
        print(f"approved: the live course version is now {state['corpus_version'][:12]}")
    return result


def _date(text: str) -> object:
    import datetime as dt

    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("a date as YYYY-MM-DD") from None


def _purge(args: argparse.Namespace, now: Any = None) -> int:
    """Retry open trace deletions on the app's JSONL trace file, then apply retention with
    `Store.purge`. Exit 1 when a deletion request is still incomplete after 7 days."""
    import datetime as dt

    from course_assistant.llm import JsonlSink
    from course_assistant.privacy import retry_trace_deletion
    from course_assistant.store import RetentionPolicy, Store

    path = args.app_dir / "app.sqlite"
    if not path.is_file():
        print(f"purge: no app store at {path} (nothing to purge; check --app-dir)",
              file=sys.stderr)  # fmt: skip
        return EXIT_CONFIG
    if not args.yes:
        print("purge: this deletes stored conversations, consent and access records past "
              "their retention period; pass --yes to confirm", file=sys.stderr)  # fmt: skip
        return EXIT_CONFIG
    when = now or dt.datetime.now(dt.UTC)
    store = Store(path)
    try:
        done = retry_trace_deletion(store, [JsonlSink(args.app_dir / "trace.jsonl")], at=when)
        report = store.purge(when, RetentionPolicy(pilot_end=args.pilot_end))
    finally:
        store.close()
    print(f"traces: completed {done} deletion request(s)")
    print(f"purge: {report.text_rows_deleted} text rows deleted, {report.consent_rows_deleted} "
          f"consent rows, {report.audit_rows_deleted} access-log rows")  # fmt: skip
    if not report.wal_cleared:
        print("purge: another reader kept deleted text in the write-ahead log; run it again "
              "when the app is stopped", file=sys.stderr)  # fmt: skip
    if report.overdue_deletions:
        print(f"purge: {report.overdue_deletions} deletion request(s) overdue (incomplete "
              "after 7 days): see docs/trust/incident-runbook.md", file=sys.stderr)  # fmt: skip
        return EXIT_GATE
    return EXIT_OK


def _passkey_hash(read: Callable[[str], str] | None = None) -> int:
    from course_assistant.demo.passkey import SECRET_NAME, hash_passcode, valid_code

    if read is None:
        import getpass

        read = getpass.getpass
    try:
        first = read("Demo passcode (6-12 digits): ")
        second = read("Again: ")
    except (EOFError, KeyboardInterrupt):
        print("passkey-hash: no passcode read", file=sys.stderr)
        return EXIT_CONFIG
    if first != second:
        print("passkey-hash: the two entries differ", file=sys.stderr)
        return EXIT_CONFIG
    if not valid_code(first):
        print("passkey-hash: a passcode is 6 to 12 digits", file=sys.stderr)
        return EXIT_CONFIG
    print(hash_passcode(first))
    print(f"Store this line as {SECRET_NAME} in the app's secret store.", file=sys.stderr)
    return EXIT_OK


def _eval_replay(args: argparse.Namespace) -> int:
    import json

    try:
        from course_assistant.answer import golden
        from course_assistant.answer.replay import ReplayError
    except ImportError as exc:
        print(f"eval-replay: {exc.name} is not installed: pip install "
              "'course-assistant[openai]'", file=sys.stderr)  # fmt: skip
        return EXIT_CONFIG
    try:
        results = golden.run(args.fixture or golden.FIXTURE)
    except (OSError, ValueError, ReplayError) as exc:
        print(f"eval-replay: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return EXIT_CONFIG
    failed = [cid for cid, ok, _ in results if not ok]
    if args.json:
        print(json.dumps({"requests": len(results), "matched": len(results) - len(failed),
                          "mismatched": failed}))  # fmt: skip
    else:
        print(f"golden replay: {len(results) - len(failed)}/{len(results)} requests match")
        for cid in failed:
            print(f"  mismatch: {cid}")
    return EXIT_GATE if failed or not results else EXIT_OK


def _vector_store_api() -> VectorStoreAPI:
    """The real OpenAI client, built only when a run will write (never for --dry-run)."""
    import openai

    from course_assistant.ingest import OpenAIVectorStore

    return OpenAIVectorStore(openai.OpenAI(max_retries=2, timeout=60.0))


def _ingest(args: argparse.Namespace) -> int:
    from course_assistant.ingest import CorpusStore, IngestError, apply, plan
    from course_assistant.ingest.serving import live_corpus
    from course_assistant.schemas import ConfigError, load_app_config, load_manifest

    base = args.config.parent
    try:
        config = load_app_config(args.config)
        manifest = load_manifest(args.manifest or base / "manifest.yaml")
        if manifest.cohort != config.cohort:
            raise ConfigError(f"manifest cohort {manifest.cohort} is not config cohort "
                              f"{config.cohort}")  # fmt: skip
        store = CorpusStore(args.store or base / "corpus_store", config.cohort)
        p = plan(manifest, store.ledger(), root=args.root or base, live=live_corpus(store))
        print(p.render(dry_run=args.dry_run))
        if args.dry_run:
            return EXIT_OK
        if not os.environ.get("OPENAI_API_KEY"):
            raise ConfigError("OPENAI_API_KEY is not set (use --dry-run to see the plan)")
        try:
            api = _vector_store_api()
        except ImportError:
            raise ConfigError(
                "openai is not installed: pip install 'course-assistant[openai]'"
            ) from None
        corpus = apply(p, api, vector_store_id=config.vector_store_id, store=store)
    except (ConfigError, IngestError) as exc:
        print(f"ingest: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"reconciled: vector store {config.vector_store_id} holds {len(corpus.units)} units, "
          f"corpus {corpus.corpus_version[:12]}")  # fmt: skip
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ingest":
        return _ingest(args)
    if args.command == "eval-replay":
        return _eval_replay(args)
    if args.command == "passkey-hash":
        return _passkey_hash()
    if args.command == "init":
        return _init(args.dir)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "ask":
        return _ask(args)
    if args.command == "eval":
        return _eval(args)
    if args.command == "eval-check":
        return _gate_report(args.run, args)[0]
    if args.command == "purge":
        return _purge(args)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
