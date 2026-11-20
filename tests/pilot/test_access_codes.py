"""Personal access codes for pilot learners (OWNER D-98): one random code per learner, only
its keyed hash kept (which is also the learner's id), a daily question cap, live revocation.
No name, email or platform id ever reaches the app."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from course_assistant import cli
from course_assistant.demo._errors import GuardRefused
from course_assistant.demo.passkey import CLOSED, NEEDED, WRONG
from course_assistant.pilot import access as A
from course_assistant.privacy.pseudonym import learner_pid

SECRET = "a-long-random-pilot-secret-0123456789"


def made(tmp_path: Path, n: int = 3) -> tuple[Path, list[tuple[str, str]]]:
    reg = tmp_path / "access-codes.json"
    rows = A.new_codes(reg, n, SECRET)
    return reg, rows


def gate(reg: Path, **kw) -> A.AccessCodeGate:
    return A.AccessCodeGate(reg, lambda: SECRET, **kw)


# ---------- generating codes ----------


def test_new_codes_are_unique_well_formed_and_the_registry_holds_no_code(tmp_path):
    reg, rows = made(tmp_path, 25)
    labels, codes = zip(*rows, strict=True)
    assert list(labels) == [f"L{i:03d}" for i in range(1, 26)]
    assert len(set(codes)) == 25 and all(A.CODE_RE.fullmatch(c) for c in codes)
    text = reg.read_text(encoding="utf-8")
    assert not any(c.replace("-", "") in text.replace("-", "") for c in codes)
    data = json.loads(text)
    assert set(data["learners"]) == {learner_pid(A.normalize(c), SECRET) for c in codes}


def test_more_codes_continue_the_labels_and_keep_the_earlier_ones(tmp_path):
    reg, _first = made(tmp_path, 2)
    more = A.new_codes(reg, 2, SECRET)
    assert [lab for lab, _ in more] == ["L003", "L004"]
    assert len(json.loads(reg.read_text(encoding="utf-8"))["learners"]) == 4


def test_a_short_secret_makes_no_codes(tmp_path):
    with pytest.raises(A.AccessError):
        A.new_codes(tmp_path / "r.json", 2, "short")


# ---------- the gate ----------


def test_a_right_code_unlocks_and_the_session_gets_that_learners_stable_id(tmp_path):
    reg, rows = made(tmp_path)
    g = gate(reg)
    g.unlock(rows[0][1], session="s1")
    g.require("s1")
    assert g.identity("s1") == learner_pid(A.normalize(rows[0][1]), SECRET)
    g.unlock(rows[0][1].lower().replace("-", " "), session="s2")  # case and separators
    assert g.identity("s2") == g.identity("s1")
    g.unlock(rows[1][1], session="s3")
    assert g.identity("s3") != g.identity("s1")


def test_a_wrong_code_is_refused_and_repeated_guesses_lock_the_session(tmp_path):
    reg, _ = made(tmp_path)
    g = gate(reg, max_failures=3)
    for _ in range(3):
        with pytest.raises(GuardRefused) as e:
            g.unlock("AAAA-BBBB-CCCC", session="s1")
        assert e.value.reason == g.WRONG_TEXT != WRONG
    assert g.locked("s1")


def test_revoking_a_code_closes_its_open_sessions_at_the_next_check(tmp_path):
    reg, rows = made(tmp_path)
    g = gate(reg)
    g.unlock(rows[1][1], session="s1")
    assert A.revoke(reg, "L002") is True
    with pytest.raises(GuardRefused) as e:
        g.require("s1")
    assert e.value.reason == g.NEEDED_TEXT != NEEDED
    with pytest.raises(GuardRefused):
        g.unlock(rows[1][1], session="s2")
    g.unlock(rows[0][1], session="s3")  # the others still work
    assert A.revoke(reg, "L999") is False


def test_no_registry_or_no_secret_closes_the_page(tmp_path):
    with pytest.raises(GuardRefused) as e:
        gate(tmp_path / "missing.json").unlock("AAAA-BBBB-CCCC", session="s1")
    assert e.value.reason == CLOSED
    reg, rows = made(tmp_path)
    with pytest.raises(GuardRefused) as e:
        A.AccessCodeGate(reg, lambda: None).unlock(rows[0][1], session="s1")
    assert e.value.reason == CLOSED


def test_a_passkey_gate_has_no_learner_identity():
    from course_assistant.demo.passkey import PasskeyGate

    assert PasskeyGate(lambda: None).identity("s1") is None


# ---------- the learner chat ----------


def test_the_chat_takes_the_codes_learner_id_and_caps_questions_per_day(tmp_path):
    from tests.app.rig import ChatRig, queues

    reg, rows = made(tmp_path)
    g = gate(reg)
    rig = ChatRig(tmp_path, owner=queues("answered", "answered", "answered"), gate=g,
                  daily_questions=2)  # fmt: skip
    st = rig.state
    rig.chat.unlock(st, rows[0][1])
    assert st.pid == learner_pid(A.normalize(rows[0][1]), SECRET)
    rig.chat.accept_notice(st)
    rig.chat.ask(st, "What is a stop rule?")
    rig.chat.ask(st, "And a guardrail metric?")
    with pytest.raises(GuardRefused) as e:
        rig.chat.ask(st, "One more?")
    assert "2 questions" in e.value.reason
    rig.chat.sign_out(st)
    assert st.pid != learner_pid(A.normalize(rows[0][1]), SECRET)


# ---------- the command ----------


def _run(*argv: str) -> int:
    return cli.main(["access-codes", *argv])


def test_cli_makes_codes_into_a_private_file_and_refuses_to_overwrite(tmp_path, monkeypatch,
                                                                       capsys):  # fmt: skip
    monkeypatch.setenv("PID_SECRET", SECRET)
    reg, out = tmp_path / "access-codes.json", tmp_path / "codes.csv"
    assert _run("new", "--count", "3", "--registry", str(reg), "--out", str(out)) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "label,code" and len(lines) == 4
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    printed = capsys.readouterr().out
    assert "3 code(s)" in printed and lines[1].split(",")[1] not in printed  # never echoed
    assert _run("new", "--count", "1", "--registry", str(reg), "--out", str(out)) == 2


def test_cli_revoke_and_refusals(tmp_path, monkeypatch):
    monkeypatch.setenv("PID_SECRET", SECRET)
    reg = tmp_path / "access-codes.json"
    _run("new", "--count", "2", "--registry", str(reg), "--out", str(tmp_path / "c.csv"))
    assert _run("revoke", "--registry", str(reg), "--label", "L001") == 0
    assert _run("revoke", "--registry", str(reg), "--label", "L404") == 2
    monkeypatch.delenv("PID_SECRET")
    assert (
        _run("new", "--count", "1", "--registry", str(reg), "--out", str(tmp_path / "d.csv")) == 2
    )


# ---------- wiring ----------


def test_startup_in_code_mode_needs_the_registry_and_pid_secret_not_the_demo_secrets(tmp_path):
    from course_assistant.app import deploy
    from tests.app.team_rig import STAFF_HASH as HASH_B

    reg, _ = made(tmp_path)
    env = {"CA_ACCESS_CODES": str(reg), "OPENAI_API_KEY": "sk-x", "STAFF_PASSKEY_HASH": HASH_B,
           "PID_SECRET": SECRET}  # fmt: skip
    assert deploy.startup_problem(env, {}) is None
    assert "PID_SECRET" in (deploy.startup_problem({**env, "PID_SECRET": ""}, {}) or "")
    bad = {**env, "CA_ACCESS_CODES": str(tmp_path / "nope.json")}
    assert "access codes" in (deploy.startup_problem(bad, {}) or "")


def test_the_page_asks_for_an_access_code_not_a_passkey(tmp_path):
    from course_assistant.demo.passkey import PasskeyGate

    reg, _ = made(tmp_path)
    assert gate(reg).PROMPT[0] == "Access code" and PasskeyGate.PROMPT[0] == "Passkey"


# ---------- security review fixes (D-98) ----------


def test_deleting_history_does_not_reset_the_daily_cap(tmp_path):
    from tests.app.rig import ChatRig, queues

    reg, rows = made(tmp_path)
    rig = ChatRig(tmp_path, owner=queues("answered", "answered", "answered"), gate=gate(reg),
                  daily_questions=2)  # fmt: skip
    st = rig.state
    rig.chat.unlock(st, rows[0][1])
    rig.chat.accept_notice(st)
    rig.chat.ask(st, "What is a stop rule?")
    rig.chat.ask(st, "And a guardrail metric?")
    rig.chat.delete(st)
    rig.chat.accept_notice(st)
    with pytest.raises(GuardRefused) as e:
        rig.chat.ask(st, "One more?")
    assert "2 questions" in e.value.reason


def test_the_daily_count_holds_no_text_and_keeps_only_today(tmp_path):
    import datetime as dt

    c = A.DailyCount(tmp_path / "daily.json")
    d1, d2 = dt.date(2026, 12, 1), dt.date(2026, 12, 2)
    assert c.take("a" * 16, d1, 2) and c.take("a" * 16, d1, 2)
    assert not c.take("a" * 16, d1, 2)
    assert c.take("a" * 16, d2, 2)  # a new day starts again
    data = json.loads((tmp_path / "daily.json").read_text(encoding="utf-8"))
    assert data == {"day": "2026-12-02", "counts": {"a" * 16: 1}}


def test_a_short_secret_closes_the_page_instead_of_refusing_every_code(tmp_path):
    from course_assistant.app import deploy
    from tests.app.team_rig import STAFF_HASH

    reg, rows = made(tmp_path)
    with pytest.raises(GuardRefused) as e:
        A.AccessCodeGate(reg, lambda: "short").unlock(rows[0][1], session="s1")
    assert e.value.reason == CLOSED
    env = {"CA_ACCESS_CODES": str(reg), "OPENAI_API_KEY": "sk-x",
           "STAFF_PASSKEY_HASH": STAFF_HASH, "PID_SECRET": "short"}  # fmt: skip
    assert "PID_SECRET" in (deploy.startup_problem(env, {}) or "")


@pytest.mark.parametrize("entry", ["L001", {"label": 7, "revoked": False},
                                   {"label": "X1", "revoked": False},
                                   {"label": "L001", "revoked": "no"}])  # fmt: skip
def test_a_malformed_registry_fails_closed(tmp_path, entry):
    reg = tmp_path / "r.json"
    reg.write_text(json.dumps({"version": 1, "learners": {"a" * 16: entry}}), encoding="utf-8")
    with pytest.raises(A.AccessError):
        A.load(reg)
    with pytest.raises(GuardRefused) as e:
        gate(reg).unlock("ABCD-EFGH-JKMN", session="s1")
    assert e.value.reason == CLOSED


def test_saves_use_a_private_unique_temp_file_under_a_lock(tmp_path, monkeypatch):
    reg, _ = made(tmp_path)
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((Path(src).name, stat.S_IMODE(os.stat(src).st_mode)))
        return real(src, dst)

    monkeypatch.setattr(os, "replace", spy)  # access.py uses this same os module
    A.new_codes(reg, 1, SECRET)
    A.revoke(reg, "L001")
    assert len({n for n, _ in seen}) == 2 and all(m == 0o600 for _, m in seen)
    assert (tmp_path / "access-codes.json.lock").exists()


def test_cli_checks_the_out_file_before_making_any_code(tmp_path, monkeypatch):
    monkeypatch.setenv("PID_SECRET", SECRET)
    reg = tmp_path / "access-codes.json"
    assert _run("new", "--count", "2", "--registry", str(reg),
                "--out", str(tmp_path / "no-such-dir" / "c.csv")) == 2  # fmt: skip
    assert not reg.exists()


def test_code_mode_refusals_speak_of_access_codes(tmp_path):
    reg, _ = made(tmp_path)
    g = gate(reg)
    with pytest.raises(GuardRefused) as e:
        g.unlock("AAAA-BBBB-CCCC", session="s1")
    assert e.value.reason == "Wrong access code."
    with pytest.raises(GuardRefused) as e:
        g.require("s9")
    assert e.value.reason == "Enter your access code to continue."
