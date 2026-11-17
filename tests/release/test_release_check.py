"""`scripts/release_check.py` (R3.1 OSS-01): tag matches version; built artefacts are sound."""

from __future__ import annotations

import gzip
import io
import tarfile
import zipfile
from pathlib import Path

import release_check

V = "1.2.3"


def _sdist(dist: Path, members: dict[str, bytes], version: str = V) -> None:
    top = f"course_assistant-{version}"
    with (
        (dist / f"{top}.tar.gz").open("wb") as fh,
        gzip.GzipFile(fileobj=fh, mode="wb") as gz,
        tarfile.TarFile(fileobj=gz, mode="w") as tar,
    ):
        for name, data in members.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _wheel(dist: Path, members: dict[str, bytes], version: str = V) -> None:
    with zipfile.ZipFile(dist / f"course_assistant-{version}-py3-none-any.whl", "w") as whl:
        for name, data in members.items():
            whl.writestr(name, data)


META = (
    f"Metadata-Version: 2.4\nName: course-assistant\nVersion: {V}\n"
    "Description-Content-Type: text/markdown\n\n# readme\n"
).encode()


def _good(dist: Path) -> None:
    _sdist(dist, {"README.md": b"# r", "LICENSE": b"MIT", "pyproject.toml": b"",
                  "src/course_assistant/__init__.py": b""})  # fmt: skip
    _wheel(dist, {"course_assistant/__init__.py": b"",
                  "course_assistant/templates/config.yaml": b"",
                  f"course_assistant-{V}.dist-info/METADATA": META})  # fmt: skip


def test_tag_must_equal_v_plus_version():
    assert release_check.tag_problems("v1.2.3", "1.2.3") == []
    for tag in ("1.2.3", "v1.2.4", "v1.2.3-rc1", "refs/tags/v1.2.3", ""):
        assert release_check.tag_problems(tag, "1.2.3"), tag


def test_good_dist_passes(tmp_path):
    _good(tmp_path)
    assert release_check.dist_problems(tmp_path, V, ("runs", ".env")) == []


def test_private_path_in_either_artefact_fails(tmp_path):
    _good(tmp_path)
    _wheel(tmp_path, {"course_assistant/__init__.py": b"", "runs/x/record.json": b"{}",
                      "course_assistant/templates/config.yaml": b"",
                      f"course_assistant-{V}.dist-info/METADATA": META})  # fmt: skip
    probs = release_check.dist_problems(tmp_path, V, ("runs",))
    assert any("runs/x/record.json" in p for p in probs)


def test_private_path_in_sdist_fails(tmp_path):
    _good(tmp_path)
    (tmp_path / f"course_assistant-{V}.tar.gz").unlink()
    _sdist(tmp_path, {"README.md": b"", "LICENSE": b"", "pyproject.toml": b"",
                      "src/course_assistant/__init__.py": b"", ".env": b"X="})  # fmt: skip
    assert any(".env" in p for p in release_check.dist_problems(tmp_path, V, (".env",)))


def test_wrong_version_or_count_fails(tmp_path):
    _good(tmp_path)
    _wheel(tmp_path, {"x": b""}, version="9.9.9")
    probs = release_check.dist_problems(tmp_path, V, ())
    assert any("one wheel" in p for p in probs)


def test_readme_must_be_markdown_and_templates_shipped(tmp_path):
    _sdist(tmp_path, {"README.md": b"", "LICENSE": b"", "pyproject.toml": b"",
                      "src/course_assistant/__init__.py": b""})  # fmt: skip
    meta = b"Name: course-assistant\n"
    _wheel(tmp_path, {"course_assistant/__init__.py": b"",
                      f"course_assistant-{V}.dist-info/METADATA": meta})  # fmt: skip
    probs = release_check.dist_problems(tmp_path, V, ())
    assert any("text/markdown" in p for p in probs)
    assert any("templates" in p for p in probs)


def test_excludes_come_from_pyproject():
    ex = release_check.sdist_excludes()
    for p in ("golden/heldout", "runs", ".evalkit", ".env", ".private-terms", "Portfolio notes.md"):
        assert p in ex


def test_main_exit_codes(tmp_path, capsys):
    _good(tmp_path)
    assert release_check.main(["--tag", f"v{V}", "--dist", str(tmp_path), "--version", V]) == 0
    assert release_check.main(["--tag", "v0.0.0", "--dist", str(tmp_path), "--version", V]) == 1
    assert release_check.main(["--tag", f"v{V}", "--dist", str(tmp_path / "none"),
                               "--version", V]) == 1  # fmt: skip
    capsys.readouterr()
