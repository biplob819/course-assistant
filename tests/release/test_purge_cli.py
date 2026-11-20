"""`course-assistant purge` and `make purge` (R3.1, OWNER): the tested retention purge
(`Store.purge`, N1.5) and the trace-deletion retry (`privacy.retry_trace_deletion`, B2.2) from
the command line. It deletes, so it refuses without --yes, and it never creates a store.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from course_assistant import cli
from course_assistant.privacy import delete
from course_assistant.store import Store
from harness_lib import ROOT
from tests.privacy.test_privacy import SECRET, T0, A, B, at, fill, read, trace_lines, traced

PILOT_END = "2026-11-30"


class NoDelete:
    def emit(self, record: dict) -> None:
        return None


def _app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    app.mkdir(parents=True)
    store = Store(app / "app.sqlite")
    fill(store)
    store.close()
    return app


def _purge(*argv: str, now: dt.datetime) -> int:
    return cli._purge(cli.build_parser().parse_args(["purge", *argv]), now=now)


def _text_rows(app: Path) -> int:
    store = Store(app / "app.sqlite")
    try:
        return len(store.conversations_for(A)) + len(store.conversations_for(B))
    finally:
        store.close()


def test_purge_refuses_without_yes_and_changes_nothing(tmp_path, capsys):
    app = _app(tmp_path)
    late = T0 + dt.timedelta(days=400)
    assert _purge("--app-dir", str(app), "--pilot-end", PILOT_END, now=late) == cli.EXIT_CONFIG
    assert "--yes" in capsys.readouterr().err
    assert _text_rows(app) == 4


def test_purge_never_creates_a_store(tmp_path, capsys):
    app = tmp_path / "empty"
    code = cli.main(["purge", "--app-dir", str(app), "--pilot-end", PILOT_END, "--yes"])
    assert code == cli.EXIT_CONFIG
    assert not app.exists() and "app.sqlite" in capsys.readouterr().err


def test_purge_keeps_text_until_pilot_end_plus_30_days_then_deletes_it(tmp_path, capsys):
    app = _app(tmp_path)
    end = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)  # the close of the last pilot day
    args = ("--app-dir", str(app), "--pilot-end", PILOT_END, "--yes")
    assert _purge(*args, now=end + dt.timedelta(days=29)) == cli.EXIT_OK
    assert _text_rows(app) == 4
    assert _purge(*args, now=end + dt.timedelta(days=31)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert _text_rows(app) == 0
    assert "text rows deleted" in out and SECRET not in out


def test_purge_retries_trace_deletion_then_fails_on_an_overdue_request(tmp_path, capsys):
    app = _app(tmp_path)
    store = Store(app / "app.sqlite")
    sink = traced(app)  # app/trace.jsonl, as the pages write it
    delete(A, store=store, traces=[NoDelete()], at=at(40))  # the trace step could not run
    store.close()
    args = ("--app-dir", str(app), "--pilot-end", "2027-03-31", "--yes")
    assert _purge(*args, now=at(40) + dt.timedelta(days=1)) == cli.EXIT_OK
    assert read(sink) == trace_lines(B, "b"), "the retry removed the learner's trace lines"
    assert "1 deletion request" in capsys.readouterr().out

    app2 = _app(tmp_path / "second")
    store = Store(app2 / "app.sqlite")
    delete(A, store=store, traces=[NoDelete()], at=at(40))
    store.close()
    blocked = app2 / "trace.jsonl.tmp"  # a non-empty leftover: every trace rewrite fails loudly
    blocked.mkdir()
    (blocked / "x").write_text("x", encoding="utf-8")
    args = ("--app-dir", str(app2), "--pilot-end", "2027-03-31", "--yes")
    assert _purge(*args, now=at(40) + dt.timedelta(days=6)) == cli.EXIT_OK
    code = _purge(*args, now=at(40) + dt.timedelta(days=8))
    assert code == cli.EXIT_GATE
    assert "overdue" in capsys.readouterr().err


def test_purge_rejects_a_bad_date(tmp_path):
    app = _app(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(["purge", "--app-dir", str(app), "--pilot-end", "31/11/2026", "--yes"])
    assert exc.value.code == 2


def test_make_purge_calls_the_cli_and_the_caller_confirms():
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "purge:" in text
    recipe = text.split("\npurge:")[1].split("\n\n")[0]
    assert "course-assistant purge" in recipe
    assert " --yes" not in recipe, "the confirmation is the caller's (YES=--yes)"
    assert "$(YES)" in recipe
