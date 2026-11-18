"""Command line entry point. Make targets call these subcommands (Tech Spec §4).

Exit codes: 0 passed, 1 a gate failed (a replay mismatch, a failed doctor check), 2 config
or runtime error (including a failed reconcile, an unusable `init` folder or bad arguments).
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
from typing import TYPE_CHECKING

from course_assistant import __version__

if TYPE_CHECKING:
    from course_assistant.ingest import VectorStoreAPI

EXIT_OK = 0
EXIT_GATE = 1
EXIT_CONFIG = 2

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
    if _has_module("evalkit") and not env.get("ANTHROPIC_API_KEY", "").strip():
        out.append(Check("warn", "secrets", "ANTHROPIC_API_KEY is not set (needed only for "
                         "evaluation with the second judge)"))  # fmt: skip

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
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
