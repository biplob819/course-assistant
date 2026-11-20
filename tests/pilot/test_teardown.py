"""P4.3 teardown: the cohort's vector store, its files and the app store are deleted, the store
is checked gone, and a proof is written. Fake client; no network."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from course_assistant import cli
from course_assistant.pilot import teardown as T

VS = "vs_abc123"
NOW = dt.datetime(2026, 12, 31, 9, 0, tzinfo=dt.UTC)


class FakeAPI:
    def __init__(self, n=3, keep_store=False, fail_file=None):
        self.files = {f"file-{i}" for i in range(n)}
        self.store = True
        self.keep_store = keep_store
        self.fail_file = fail_file
        self.calls = []

    def list_files(self, vector_store_id):
        self.calls.append(("list", vector_store_id))
        return [SimpleNamespace(id=f) for f in sorted(self.files)]

    def delete_file(self, file_id):
        self.calls.append(("delete_file", file_id))
        if file_id == self.fail_file:
            raise RuntimeError("boom with secret text")
        self.files.discard(file_id)

    def delete_store(self, vector_store_id):
        self.calls.append(("delete_store", vector_store_id))
        if not self.keep_store:
            self.store = False

    def store_exists(self, vector_store_id):
        return self.store


def app_dir(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    app.mkdir()
    for name in (
        "app.sqlite",
        "app.sqlite-wal",
        "app.sqlite-shm",
        "trace.jsonl",
        "trace.jsonl.lock",
    ):
        (app / name).write_text("x", encoding="utf-8")
    (app / "keep-me.txt").write_text("not ours", encoding="utf-8")
    return app


def test_teardown_deletes_every_file_then_the_store_and_the_app_data(tmp_path):
    api, app = FakeAPI(), app_dir(tmp_path)
    report = T.teardown(api, VS, app, now=NOW)
    assert api.files == set() and api.store is False
    assert [c[0] for c in api.calls][-1] == "delete_store"
    assert report.files_deleted == 3 and report.store_gone and report.complete
    assert not any((app / n).exists() for n in T.APP_FILES)
    assert (app / "keep-me.txt").exists()  # only the app's own files


def test_the_proof_names_what_was_deleted_and_holds_no_learner_data(tmp_path):
    api, app = FakeAPI(n=2), app_dir(tmp_path)
    proof = tmp_path / "proof.json"
    report = T.teardown(api, VS, app, now=NOW)
    T.write_proof(report, proof)
    data = json.loads(proof.read_text(encoding="utf-8"))
    assert data["vector_store_id"] == VS and data["store_gone"] is True
    assert data["files_deleted"] == 2 and data["file_ids"] == ["file-0", "file-1"]
    assert data["app_files_removed"] and data["at"] == "2026-12-31T09:00:00+00:00"
    assert data["complete"] is True


def test_a_store_still_there_or_a_failed_file_is_incomplete_and_names_no_error_text(tmp_path):
    api = FakeAPI(keep_store=True, fail_file="file-1")
    report = T.teardown(api, VS, app_dir(tmp_path), now=NOW)
    assert not report.complete and not report.store_gone
    assert report.files_failed == ["file-1"]
    assert "secret text" not in json.dumps(T.proof_dict(report))


def _run(*argv: str, api=None) -> int:
    args = cli.build_parser().parse_args(["teardown", *argv])
    return cli._teardown(args, api=api, now=NOW)


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "schema_version: '1.0'\ncohort: pilot-2027\nvector_store_id: vs_abc123\n"
        "answer_model: gpt-6.1-terra\ncheck_model: gpt-6-luna\n"
        "reasoning_effort:\n  gpt-6.1-terra: low\n  gpt-6-luna: low\n"
        "max_num_results: 8\ngrounding_bar: 0.4\nmonthly_budget_usd: 50.0\n",
        encoding="utf-8",
    )
    return cfg


def test_cli_refuses_without_yes_and_makes_no_call(tmp_path, capsys):
    api = FakeAPI()
    code = _run("--config", str(_config(tmp_path)), "--app-dir", str(app_dir(tmp_path)),
                "--proof", str(tmp_path / "p.json"), api=api)  # fmt: skip
    assert code == cli.EXIT_CONFIG and api.calls == []
    assert "--yes" in capsys.readouterr().err


def test_cli_tears_down_writes_the_proof_and_exits_0(tmp_path, capsys):
    api, proof = FakeAPI(), tmp_path / "p.json"
    code = _run("--config", str(_config(tmp_path)), "--app-dir", str(app_dir(tmp_path)),
                "--proof", str(proof), "--yes", api=api)  # fmt: skip
    assert code == cli.EXIT_OK and proof.is_file()
    out = capsys.readouterr().out
    assert "3 file(s) deleted" in out and "vs_abc123" in out


def test_cli_exits_1_when_anything_is_left(tmp_path):
    left = FakeAPI(keep_store=True)
    code = _run("--config", str(_config(tmp_path)), "--app-dir", str(app_dir(tmp_path)),
                "--proof", str(tmp_path / "p.json"), "--yes", api=left)  # fmt: skip
    assert code == cli.EXIT_GATE


def test_cli_refuses_a_proof_inside_the_app_dir(tmp_path):
    app = app_dir(tmp_path)
    code = _run("--config", str(_config(tmp_path)), "--app-dir", str(app),
                "--proof", str(app / "p.json"), "--yes", api=FakeAPI())  # fmt: skip
    assert code == cli.EXIT_CONFIG


def test_make_teardown_calls_the_cli_and_the_caller_confirms():
    make = (Path(cli.__file__).resolve().parents[2] / "Makefile").read_text(encoding="utf-8")
    assert "teardown:" in make and "course-assistant teardown" in make and "$(YES)" in make


@pytest.mark.parametrize("missing", ["config", "app"])
def test_cli_missing_config_or_app_dir_exits_2(tmp_path, missing):
    cfg = _config(tmp_path) if missing != "config" else tmp_path / "nope.yaml"
    app = app_dir(tmp_path) if missing != "app" else tmp_path / "no-app"
    code = _run("--config", str(cfg), "--app-dir", str(app), "--proof", str(tmp_path / "p.json"),
                "--yes", api=FakeAPI())  # fmt: skip
    assert code == cli.EXIT_CONFIG


def test_the_openai_adapter_deletes_the_store_and_reads_not_found_as_gone():
    openai = pytest.importorskip("openai")

    from course_assistant.ingest import OpenAIVectorStore

    gone = openai.NotFoundError.__new__(openai.NotFoundError)  # the SDK's 404, no HTTP stack

    class Stores:
        def __init__(self):
            self.deleted, self.exists = [], True

        def delete(self, vs):
            self.deleted.append(vs)
            self.exists = False

        def retrieve(self, vs):
            if not self.exists:
                raise gone
            return SimpleNamespace(id=vs)

    stores = Stores()
    api = OpenAIVectorStore(SimpleNamespace(vector_stores=stores))
    assert api.store_exists(VS) is True
    api.delete_store(VS)
    assert stores.deleted == [VS] and api.store_exists(VS) is False


def test_any_other_error_while_checking_is_not_read_as_gone(tmp_path):
    class Broken(FakeAPI):
        def store_exists(self, vector_store_id):
            raise RuntimeError("network down")

    report = T.teardown(Broken(), VS, app_dir(tmp_path), now=NOW)
    assert report.store_gone is False and not report.complete
