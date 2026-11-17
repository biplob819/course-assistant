"""`course-assistant doctor` (R3.1 OSS-03): offline checks with fixed messages and exit codes.

Secrets are reported by name only; a sentinel key value must never reach stdout or stderr.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml

from course_assistant import cli

SENTINEL = "SENTINEL-secret-value-do-not-print-0123456789"
LINE = re.compile(r"^(ok|warn|FAIL) +[a-z_]+: \S.*$")


def _course(tmp_path: Path) -> Path:
    """An `init` course made valid: a real store id, one corpus file with its sha256."""
    assert cli.main(["init", str(tmp_path)]) == cli.EXIT_OK
    cfg_path = tmp_path / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["vector_store_id"] = "vs_0123456789abcdefABCDEF"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    man_path = tmp_path / "manifest.yaml"
    man = yaml.safe_load(man_path.read_text(encoding="utf-8"))
    for entry in man["entries"]:
        f = tmp_path / entry["path"]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"slides for " + entry["manifest_id"].encode())
        entry["sha256"] = hashlib.sha256(f.read_bytes()).hexdigest()
    man_path.write_text(yaml.safe_dump(man, sort_keys=False), encoding="utf-8")
    return cfg_path


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    return monkeypatch


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()  # drop the setup's `init` output
    code = cli.main(["doctor", *argv])
    cap = capsys.readouterr()
    assert SENTINEL not in cap.out and SENTINEL not in cap.err, "a secret value was printed"
    return code, cap.out, cap.err


def _lines(out: str) -> dict[str, str]:
    rows = {}
    for ln in out.splitlines():
        if not ln.strip() or ln.startswith("doctor:"):
            continue
        assert LINE.match(ln), f"not a fixed check line: {ln!r}"
        level, rest = ln.split(None, 1)
        rows[rest.split(":", 1)[0]] = level
    return rows


def test_a_ready_course_passes(tmp_path, env, capsys):
    code, out, _ = _run(capsys, "--config", str(_course(tmp_path)))
    rows = _lines(out)
    assert code == cli.EXIT_OK, out
    for check in ("python", "extras", "config", "secrets", "vector_store", "corpus"):
        assert rows.get(check) in ("ok", "warn"), (check, out)
    assert rows["python"] == "ok" and rows["config"] == "ok" and rows["corpus"] == "ok"


def test_missing_key_fails_by_name_only(tmp_path, env, capsys):
    cfg = _course(tmp_path)
    env.delenv("OPENAI_API_KEY")
    code, out, _ = _run(capsys, "--config", str(cfg))
    assert code == cli.EXIT_GATE
    assert _lines(out)["secrets"] == "FAIL"
    assert "OPENAI_API_KEY" in out


def test_blank_key_fails(tmp_path, env, capsys):
    cfg = _course(tmp_path)
    env.setenv("OPENAI_API_KEY", "   ")
    code, out, _ = _run(capsys, "--config", str(cfg))
    assert code == cli.EXIT_GATE and _lines(out)["secrets"] == "FAIL"


def test_key_only_in_dotenv_is_named_but_never_printed(tmp_path, env, capsys):
    cfg = _course(tmp_path)
    env.delenv("OPENAI_API_KEY")
    (tmp_path / ".env").write_text(f"OPENAI_API_KEY={SENTINEL}\n", encoding="utf-8")
    code, out, _ = _run(capsys, "--config", str(cfg))
    assert code == cli.EXIT_GATE and _lines(out)["secrets"] == "FAIL"
    assert ".env" in out and "export" in out


def test_placeholder_store_id_from_init_fails(tmp_path, env, capsys):
    cfg = _course(tmp_path)
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    for bad in ("vs_REPLACE_WITH_YOUR_STORE_ID", "vs-abc", "abc123", "vs_"):
        data["vector_store_id"] = bad
        cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        code, out, _ = _run(capsys, "--config", str(cfg))
        assert code == cli.EXIT_GATE and _lines(out)["vector_store"] == "FAIL", bad
        assert bad == "vs_" or bad not in out  # fixed message, the id is not echoed


def test_fresh_init_course_fails_until_filled_in(tmp_path, env, capsys):
    assert cli.main(["init", str(tmp_path)]) == cli.EXIT_OK
    capsys.readouterr()
    code, out, _ = _run(capsys, "--config", str(tmp_path / "config.yaml"))
    rows = _lines(out)
    assert code == cli.EXIT_GATE
    assert rows["config"] == "ok" and rows["vector_store"] == "FAIL" and rows["corpus"] == "FAIL"


def test_missing_config_fails(tmp_path, env, capsys):
    code, out, _ = _run(capsys, "--config", str(tmp_path / "nope.yaml"))
    rows = _lines(out)
    assert code == cli.EXIT_GATE and rows["config"] == "FAIL"
    assert rows["python"] == "ok"  # the checks that do not need a config still run


def test_invalid_config_fails_without_quoting_content(tmp_path, env, capsys):
    cfg = _course(tmp_path)
    cfg.write_text("schema_version: '1.0'\ncohort: Private Name\n", encoding="utf-8")
    code, out, _ = _run(capsys, "--config", str(cfg))
    assert code == cli.EXIT_GATE and _lines(out)["config"] == "FAIL"
    assert "Private Name" not in out


def test_missing_corpus_file_fails(tmp_path, env, capsys):
    cfg = _course(tmp_path)
    for f in (tmp_path / "materials").iterdir():
        f.unlink()
    code, out, _ = _run(capsys, "--config", str(cfg))
    assert code == cli.EXIT_GATE and _lines(out)["corpus"] == "FAIL"
    assert "missing" in out


def test_changed_corpus_file_fails(tmp_path, env, capsys):
    cfg = _course(tmp_path)
    next((tmp_path / "materials").iterdir()).write_bytes(b"changed")
    code, out, _ = _run(capsys, "--config", str(cfg))
    assert code == cli.EXIT_GATE and _lines(out)["corpus"] == "FAIL"
    assert "sha256" in out


def test_cohort_mismatch_fails(tmp_path, env, capsys):
    cfg = _course(tmp_path)
    man = tmp_path / "manifest.yaml"
    data = yaml.safe_load(man.read_text(encoding="utf-8"))
    data["cohort"] = "another-cohort"
    man.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    code, out, _ = _run(capsys, "--config", str(cfg))
    assert code == cli.EXIT_GATE and _lines(out)["corpus"] == "FAIL"


def test_old_python_fails(tmp_path, env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_python_version", lambda: (3, 10, 4))
    code, out, _ = _run(capsys, "--config", str(_course(tmp_path)))
    assert code == cli.EXIT_GATE and _lines(out)["python"] == "FAIL"


def test_missing_openai_extra_fails_and_others_warn(tmp_path, env, capsys, monkeypatch):
    real = cli._has_module
    monkeypatch.setattr(cli, "_has_module", lambda m: m not in ("openai", "streamlit") and real(m))
    code, out, _ = _run(capsys, "--config", str(_course(tmp_path)))
    assert code == cli.EXIT_GATE
    assert "course-assistant[openai]" in out and "course-assistant[app]" in out
    assert re.search(r"^FAIL +extras: .*openai", out, re.M)
    assert re.search(r"^warn +extras: .*app", out, re.M)


def test_bad_arguments_exit_2(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor", "--no-such-flag"])
    assert exc.value.code == cli.EXIT_CONFIG


def test_doctor_makes_no_network_call(tmp_path, env, capsys, monkeypatch):
    import socket

    def refuse(*a, **k):
        raise AssertionError("network call from doctor")

    cfg = _course(tmp_path)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    code, _, _ = _run(capsys, "--config", str(cfg))
    assert code == cli.EXIT_OK


def test_sample_course_manifest_passes_corpus_check(env, capsys):
    """The packaged sample course is a working example: its manifest's files exist and match."""
    from harness_lib import ROOT

    report = cli.doctor_report(
        config=None,
        manifest=ROOT / "sample_course" / "manifest.yaml",
        root=ROOT,
        env={"OPENAI_API_KEY": SENTINEL},
    )
    corpus = [c for c in report if c.check == "corpus"]
    assert corpus and corpus[0].level == "ok", corpus
