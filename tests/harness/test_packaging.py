"""The published package never ships private data (D-21, D-72, NFR1).

The sdist and wheel must hold no held-out golden case, run folder, EvalKit cache, `.env`,
private-terms list or personal notes. The first test reads the build config; the second
builds both artefacts offline with uv and inspects every member.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]
NEVER = ("golden/heldout", "runs", ".evalkit", ".env", ".private-terms", "Portfolio notes.md")
REQUIRED_IN_SDIST = ("README.md", "LICENSE", "pyproject.toml", "src/course_assistant/__init__.py")


def _forbidden(rel: str) -> str | None:
    """The NEVER entry `rel` (a path inside the artefact) falls under, if any."""
    parts = PurePosixPath(rel).parts
    for bad in NEVER:
        bad_parts = PurePosixPath(bad).parts
        for i in range(len(parts) - len(bad_parts) + 1):
            if parts[i : i + len(bad_parts)] == bad_parts:
                return bad
    return None


def test_forbidden_matcher_catches_each_entry():
    for bad in NEVER:
        assert _forbidden(f"{bad}/x.json") == bad or _forbidden(bad) == bad
    assert _forbidden("src/course_assistant/runs.py") is None
    assert _forbidden("src/course_assistant/answer/replay/golden.json") is None


def test_sdist_config_excludes_private_paths():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sdist = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]
    excluded = set(sdist.get("exclude", []))
    missing = [p for p in NEVER if p not in excluded]
    assert not missing, f"sdist exclude must list {missing}"
    for inc in sdist["include"]:
        assert _forbidden(inc) is None, f"sdist include {inc!r} covers a private path"


def test_project_metadata_is_complete():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for key in ("description", "readme", "license", "keywords", "classifiers", "urls"):
        assert project.get(key), f"[project] needs {key}"
    assert project["name"] == "course-assistant"
    assert {"Homepage", "Repository", "Issues"} <= set(project["urls"])


@pytest.mark.safety
def test_built_artefacts_hold_no_private_paths(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    out = tmp_path / "dist"
    proc = subprocess.run(
        [uv, "build", "--offline", "--quiet", "-o", str(out), str(ROOT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    if proc.returncode != 0 and "offline" in proc.stderr.lower():
        pytest.skip("build backend not in the local uv cache (offline build only)")
    assert proc.returncode == 0, proc.stderr

    (sdist,) = out.glob("*.tar.gz")
    with (
        sdist.open("rb") as fh,
        gzip.GzipFile(fileobj=fh) as gz,
        tarfile.TarFile(fileobj=gz) as tar,
    ):
        # members are "<name>-<version>/<path>"; drop the top folder
        names = [m.name.split("/", 1)[1] for m in tar.getmembers() if "/" in m.name]
    bad = sorted({n for n in names if _forbidden(n)})
    assert not bad, f"sdist ships private paths: {bad}"
    for req in REQUIRED_IN_SDIST:
        assert req in names, f"sdist is missing {req}"

    (wheel,) = out.glob("*.whl")
    with zipfile.ZipFile(wheel) as whl:
        wheel_names = whl.namelist()
    bad = sorted({n for n in wheel_names if _forbidden(n)})
    assert not bad, f"wheel ships private paths: {bad}"
    assert "course_assistant/__init__.py" in wheel_names
