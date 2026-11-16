"""Command line entry point. Make targets call these subcommands (Tech Spec §4).

Exit codes: 0 passed, 1 a gate failed (a replay mismatch), 2 config or runtime error
(including a failed reconcile).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from course_assistant import __version__

if TYPE_CHECKING:
    from course_assistant.ingest import VectorStoreAPI

EXIT_OK = 0
EXIT_GATE = 1
EXIT_CONFIG = 2


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
    sub.add_parser(
        "passkey-hash",
        help="turn the demo passcode into the salted hash for the secret store",
        description="Read a 6-12 digit passcode twice without echo and print only its salted "
        "PBKDF2 hash, to paste into the app's secret store as DEMO_PASSKEY_HASH. Writes no "
        "file and makes no network call; changing the hash revokes every unlocked session.",
    )
    return ap


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
    from course_assistant.schemas import ConfigError, load_app_config, load_manifest

    base = args.config.parent
    try:
        config = load_app_config(args.config)
        manifest = load_manifest(args.manifest or base / "manifest.yaml")
        if manifest.cohort != config.cohort:
            raise ConfigError(f"manifest cohort {manifest.cohort} is not config cohort "
                              f"{config.cohort}")  # fmt: skip
        store = CorpusStore(args.store or base / "corpus_store", config.cohort)
        p = plan(manifest, store.ledger(), root=args.root or base)
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
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
