"""ING-06: `course-assistant ingest --config … [--dry-run]`: the plan with no key and no calls,
a second run with zero write calls, and exit 2 for config and reconcile errors."""

from __future__ import annotations

import socket

import pytest

from course_assistant import cli
from harness_lib import ROOT
from tests.ingest.conftest import SAMPLE_MANIFEST, write_config
from tests.ingest.fakes import FakeVectorStore


def args(cfg, tmp_path, *extra: str) -> list[str]:
    return ["ingest", "--config", str(cfg), "--manifest", str(SAMPLE_MANIFEST),
            "--root", str(ROOT), "--store", str(tmp_path / "corpus_store"), *extra]  # fmt: skip


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def fake(monkeypatch):
    f = FakeVectorStore()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "_vector_store_api", lambda: f)
    monkeypatch.setattr("course_assistant.ingest.sync.time.sleep", lambda s: None)
    return f


def test_dry_run_prints_the_plan_and_makes_no_api_calls_without_a_key(
    tmp_path, monkeypatch, capsys, no_network
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def never():
        raise AssertionError("dry run built an API client")

    monkeypatch.setattr(cli, "_vector_store_api", never)
    cfg = write_config(tmp_path)
    assert cli.main(args(cfg, tmp_path, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "upload s01:slides:1" in out and "upload s03:agenda:1" in out
    assert "dropped s01:slides:20 (m-s01-slides): 1 word" in out
    assert "0 delete" in out
    assert not (tmp_path / "corpus_store").exists()  # a check-only run writes nothing (L-3)


def test_a_second_run_with_no_change_makes_zero_write_calls(tmp_path, capsys, fake):
    cfg = write_config(tmp_path)
    assert cli.main(args(cfg, tmp_path)) == 0
    first = len(fake.writes())
    assert first > 0
    fake.calls.clear()
    assert cli.main(args(cfg, tmp_path)) == 0
    assert fake.writes() == []
    assert [c[0] for c in fake.calls] == ["list_files"]  # the reconcile read only
    out = capsys.readouterr().out
    assert "0 upload, 0 relabel, 0 delete" in out and "reconciled" in out
    fake.calls.clear()
    assert cli.main(args(cfg, tmp_path, "--dry-run")) == 0
    assert fake.calls == []
    assert "nothing to do" in capsys.readouterr().out


def test_a_reconcile_mismatch_exits_2_with_the_differing_ids(tmp_path, capsys, fake):
    fake.foreign = ["file-stray-9"]
    cfg = write_config(tmp_path)
    assert cli.main(args(cfg, tmp_path)) == 2
    err = capsys.readouterr().err
    assert "reconcile" in err and "file-stray-9" in err


def test_config_errors_exit_2(tmp_path, capsys, monkeypatch):
    missing = tmp_path / "nope.yaml"
    assert cli.main(args(missing, tmp_path, "--dry-run")) == 2
    assert "nope.yaml" in capsys.readouterr().err
    other = write_config(tmp_path, cohort="other-cohort")
    assert cli.main(args(other, tmp_path, "--dry-run")) == 2
    assert "cohort" in capsys.readouterr().err
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = write_config(tmp_path)
    assert cli.main(args(cfg, tmp_path)) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_defaults_sit_next_to_the_config(tmp_path, capsys, monkeypatch):
    import shutil

    shutil.copy(SAMPLE_MANIFEST, tmp_path / "manifest.yaml")
    (tmp_path / "sample_course").symlink_to(ROOT / "sample_course")
    cfg = write_config(tmp_path)
    monkeypatch.chdir(ROOT)
    assert cli.main(["ingest", "--config", str(cfg), "--dry-run"]) == 2
    assert "corpus root" in capsys.readouterr().err  # the symlinked folder leaves the root
    (tmp_path / "sample_course").unlink()
    shutil.copytree(ROOT / "sample_course" / "build", tmp_path / "sample_course" / "build")
    assert cli.main(["ingest", "--config", str(cfg), "--dry-run"]) == 0
    assert "upload s01:slides:1" in capsys.readouterr().out
