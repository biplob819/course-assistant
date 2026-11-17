"""`course-assistant init` (R3.1 OSS-02): starter files for a new course, never overwriting."""

from __future__ import annotations

from pathlib import Path

import pytest

from course_assistant import cli
from course_assistant.schemas import load_app_config, load_manifest
from course_assistant.schemas.config import load_assessments

FILES = ("config.yaml", "manifest.yaml", "assessments.yaml", ".env.example")


def test_init_writes_every_starter_file(tmp_path, capsys):
    target = tmp_path / "my-course"
    assert cli.main(["init", str(target)]) == cli.EXIT_OK
    for name in FILES:
        assert (target / name).is_file(), name
    out = capsys.readouterr().out
    for name in FILES:
        assert f"wrote {name}" in out


def test_written_files_load_with_the_project_loaders(tmp_path):
    assert cli.main(["init", str(tmp_path)]) == cli.EXIT_OK
    config = load_app_config(tmp_path / "config.yaml")
    manifest = load_manifest(tmp_path / "manifest.yaml")
    load_assessments(tmp_path / "assessments.yaml")
    assert manifest.cohort == config.cohort
    # the store id is a placeholder that doctor reports as not yet set
    assert not cli.VECTOR_STORE_ID.fullmatch(config.vector_store_id)


def test_env_example_holds_names_only(tmp_path):
    assert cli.main(["init", str(tmp_path)]) == cli.EXIT_OK
    lines = [
        ln
        for ln in (tmp_path / ".env.example").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert lines, ".env.example lists no variable"
    names = []
    for ln in lines:
        name, sep, value = ln.partition("=")
        assert sep == "=" and value == "", f"value present for {name}"
        names.append(name)
    assert "OPENAI_API_KEY" in names


def test_init_never_overwrites(tmp_path, capsys):
    mine = b"schema_version: '1.0'\n# my own config, hand edited\n"
    (tmp_path / "config.yaml").write_bytes(mine)
    (tmp_path / ".env.example").write_bytes(b"")
    assert cli.main(["init", str(tmp_path)]) == cli.EXIT_OK
    assert (tmp_path / "config.yaml").read_bytes() == mine
    assert (tmp_path / ".env.example").read_bytes() == b""
    out = capsys.readouterr().out
    assert "kept config.yaml (exists)" in out
    assert "kept .env.example (exists)" in out
    assert "wrote manifest.yaml" in out and "wrote assessments.yaml" in out


def test_second_init_changes_nothing(tmp_path):
    assert cli.main(["init", str(tmp_path)]) == cli.EXIT_OK
    before = {n: (tmp_path / n).read_bytes() for n in FILES}
    (tmp_path / "manifest.yaml").write_text("edited\n", encoding="utf-8")
    before["manifest.yaml"] = b"edited\n"
    assert cli.main(["init", str(tmp_path)]) == cli.EXIT_OK
    assert {n: (tmp_path / n).read_bytes() for n in FILES} == before


def test_init_default_target_is_the_current_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == cli.EXIT_OK
    assert (tmp_path / "config.yaml").is_file()


def test_init_refuses_a_file_as_target(tmp_path, capsys):
    target = tmp_path / "a-file"
    target.write_text("x", encoding="utf-8")
    assert cli.main(["init", str(target)]) == cli.EXIT_CONFIG
    assert "not a folder" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "x"


def test_init_does_not_follow_a_symlink_onto_another_file(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    course = tmp_path / "course"
    course.mkdir()
    try:
        (course / "config.yaml").symlink_to(victim)
    except OSError:
        pytest.skip("symlinks not available")
    assert cli.main(["init", str(course)]) == cli.EXIT_OK
    assert victim.read_text(encoding="utf-8") == "keep me"


def test_templates_ship_inside_the_package():
    from importlib.resources import files

    pkg = files("course_assistant") / "templates"
    for name in ("config.yaml", "manifest.yaml", "assessments.yaml", "env.example"):
        assert (pkg / name).is_file(), name


def test_init_makes_no_network_call(tmp_path, monkeypatch):
    import socket

    def refuse(*a, **k):
        raise AssertionError("network call from init")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert cli.main(["init", str(Path(tmp_path))]) == cli.EXIT_OK
