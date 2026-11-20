"""Pilot learner ids from platform ids (D-13, PIL-03): HMAC-SHA256(platform_user_id, PID_SECRET)
cut to 16 hex characters. Same person, same id; no secret, no id; never the platform id."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from course_assistant.privacy import pseudonym
from course_assistant.privacy._errors import check_pid

SECRET = "a-long-random-pilot-secret-0123456789"


def test_the_id_is_the_keyed_hash_cut_to_16_hex_and_a_valid_pid():
    pid = pseudonym.learner_pid("user-42@platform", SECRET)
    want = hmac.new(SECRET.encode(), b"user-42@platform", hashlib.sha256).hexdigest()[:16]
    assert pid == want and check_pid(pid) == pid


def test_same_person_same_id_other_person_or_key_other_id():
    a = pseudonym.learner_pid("u1", SECRET)
    assert a == pseudonym.learner_pid("u1", SECRET)
    assert a != pseudonym.learner_pid("u2", SECRET)
    assert a != pseudonym.learner_pid("u1", SECRET + "x")


def test_surrounding_space_is_not_a_different_person():
    assert pseudonym.learner_pid("  u1 ", SECRET) == pseudonym.learner_pid("u1", SECRET)


@pytest.mark.parametrize("secret", ["", "   ", "short"])
def test_a_missing_or_short_secret_is_refused(secret):
    with pytest.raises(pseudonym.PseudonymError):
        pseudonym.learner_pid("u1", secret)


@pytest.mark.parametrize("uid", ["", "   ", None, 42])
def test_a_missing_platform_id_is_refused(uid):
    with pytest.raises(pseudonym.PseudonymError):
        pseudonym.learner_pid(uid, SECRET)  # type: ignore[arg-type]


def test_errors_never_carry_the_platform_id_or_the_secret():
    try:
        pseudonym.learner_pid("someone@example.org", "tiny-key")
    except pseudonym.PseudonymError as exc:
        assert "someone" not in str(exc) and "tiny-key" not in str(exc)
    else:
        raise AssertionError("expected a refusal")
    assert "someone" not in repr(pseudonym.learner_pid("someone@example.org", SECRET))


def test_a_pilot_host_keeps_one_persistent_store(tmp_path):
    # CA_DAILY_RESET=0 survives the deploy defaults and gives one store file, never removed
    from course_assistant.app import deploy, wiring

    env = {"CA_DAILY_RESET": "0"}
    deploy.apply_defaults(env, tmp_path)
    assert env["CA_DAILY_RESET"] == "0"
    stores = wiring.app_stores(env, tmp_path / "app")
    assert not isinstance(stores, deploy.DailyStores)
    stores()
    assert (tmp_path / "app" / "app.sqlite").is_file()
