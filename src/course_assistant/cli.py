"""Command line entry point. Make targets call these subcommands (Tech Spec §4)."""

from __future__ import annotations

import argparse
import sys

from course_assistant import __version__


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="course-assistant", description=__doc__)
    ap.add_argument("--version", action="version", version=f"course-assistant {__version__}")
    return ap


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
