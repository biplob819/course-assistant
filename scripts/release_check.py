"""Release checks run by `.github/workflows/release.yml` before anything is published (R3.1).

1. The pushed tag is exactly `v` + the pyproject version.
2. `dist/` holds exactly one sdist and one wheel of that version; the wheel's metadata
   declares the README as Markdown (so PyPI renders it) and ships the `init` templates; no
   member of either artefact falls under a path the sdist config excludes (the same list
   tests/harness/test_packaging.py requires to cover every private path).

Usage:
    python scripts/release_check.py --tag "$GITHUB_REF_NAME" --dist dist

Offline. Exit 0 all checks pass, 1 a check failed.
"""

from __future__ import annotations

import argparse
import gzip
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DIST_NAME = "course_assistant"
REQUIRED_IN_SDIST = ("README.md", "LICENSE", "pyproject.toml", "src/course_assistant/__init__.py")
TEMPLATE = "course_assistant/templates/config.yaml"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def project_version() -> str:
    return str(_pyproject()["project"]["version"])


def sdist_excludes() -> tuple[str, ...]:
    return tuple(_pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"].get("exclude", []))


def tag_problems(tag: str, version: str) -> list[str]:
    if tag != f"v{version}":
        return [f"tag {tag!r} is not 'v{version}' (the pyproject version)"]
    return []


def _under(rel: str, excludes: Iterable[str]) -> str | None:
    parts = PurePosixPath(rel).parts
    for bad in excludes:
        bad_parts = PurePosixPath(bad).parts
        for i in range(len(parts) - len(bad_parts) + 1):
            if parts[i : i + len(bad_parts)] == bad_parts:
                return bad
    return None


def dist_problems(dist: Path, version: str, excludes: Iterable[str]) -> list[str]:
    excludes = tuple(excludes)
    if not dist.is_dir():
        return [f"{dist} is not a folder"]
    problems: list[str] = []
    sdists = sorted(dist.glob("*.tar.gz"))
    wheels = sorted(dist.glob("*.whl"))
    want_sdist = f"{DIST_NAME}-{version}.tar.gz"
    if [p.name for p in sdists] != [want_sdist]:
        problems.append(f"want exactly one sdist {want_sdist}, found {[p.name for p in sdists]}")
    good_wheels = [p for p in wheels if p.name.startswith(f"{DIST_NAME}-{version}-")]
    if len(wheels) != 1 or len(good_wheels) != 1:
        problems.append(f"want exactly one wheel of {version}, found {[p.name for p in wheels]}")

    if (dist / want_sdist).is_file():
        with (
            (dist / want_sdist).open("rb") as fh,
            gzip.GzipFile(fileobj=fh) as gz,
            tarfile.TarFile(fileobj=gz) as tar,
        ):
            names = [m.name.split("/", 1)[1] for m in tar.getmembers() if "/" in m.name]
        for n in sorted(n for n in names if _under(n, excludes)):
            problems.append(f"sdist ships an excluded path: {n}")
        for req in REQUIRED_IN_SDIST:
            if req not in names:
                problems.append(f"sdist is missing {req}")

    for whl_path in good_wheels[:1]:
        with zipfile.ZipFile(whl_path) as whl:
            wnames = whl.namelist()
            meta_name = f"{DIST_NAME}-{version}.dist-info/METADATA"
            meta = whl.read(meta_name).decode("utf-8") if meta_name in wnames else ""
        for n in sorted(n for n in wnames if _under(n, excludes)):
            problems.append(f"wheel ships an excluded path: {n}")
        if "Description-Content-Type: text/markdown" not in meta:
            problems.append("wheel METADATA does not declare the README as text/markdown")
        if TEMPLATE not in wnames:
            problems.append(f"wheel does not ship the init templates ({TEMPLATE})")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Release checks before publishing (R3.1).")
    ap.add_argument("--tag", required=True, help="the pushed tag, e.g. v0.1.0")
    ap.add_argument("--dist", required=True, type=Path, help="folder holding the built files")
    ap.add_argument("--version", help="default: the version in pyproject.toml")
    args = ap.parse_args(argv)
    version = args.version or project_version()
    problems = tag_problems(args.tag, version) + dist_problems(args.dist, version,
                                                               sdist_excludes())  # fmt: skip
    for p in problems:
        print(f"release-check FAIL: {p}")
    if problems:
        return 1
    print(f"release-check: v{version} sdist and wheel pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
