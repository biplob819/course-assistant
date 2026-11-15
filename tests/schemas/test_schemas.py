import datetime as dt
import re
from typing import Any

import pytest
from pydantic import ValidationError

from course_assistant import schemas as S
from course_assistant.schemas.config import ConfigError, load_app_config, load_manifest
from harness_lib import ROOT

UTC = dt.UTC
T0 = dt.datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
PID = "0123456789abcdef"
QID = "q-0123456789ab"
CID = "c-0123456789ab"
SHA = "a" * 64


def cite(i=1):
    return S.Citation(
        file_id="file-1", unit_index=i, label="Session 2 slides, slide 9", session="s02"
    )


def grounding(passed=True):
    return S.GroundingResult(
        citations_resolve=True,
        top_score=0.8 if passed else 0.1,
        bar=0.5,
        sentence_support=(True, True),
        passed=passed,
        ground_version="g1",
    )


def answer(**kw):
    base: dict[str, Any] = dict(
        question_id=QID,
        status="answered",
        text="Offline evals score a fixed set before release.",
        citations=(cite(),),
        grounding=grounding(),
        integrity_passed=True,
        model_snapshot="gpt-6.1-sol-2026-10-01",
        rules_hash="abcdef123456",
        corpus_version=SHA,
        top_search_score=0.8,
        cost_usd=0.02,
        latency_ms=4200,
        created_at=T0,
    )
    base.update(kw)
    return S.Answer(**base)


def entry(**kw):
    base: dict[str, Any] = dict(
        manifest_id="m-s02-slides",
        path="sample_course/build/s02-slides.pptx",
        session="s02",
        kind="slides",
        sha256=SHA,
        licence="CC-BY-4.0",
        approved_by="owner-1",
        approved_at=dt.date(2026, 11, 15),
    )
    base.update(kw)
    return S.ManifestEntry(**base)


def golden(**kw):
    base: dict[str, Any] = dict(
        schema_version="1.0",
        id="ta-b1-001",
        rule="B1",
        question="Why do we need both offline and online evals?",
        expected_status="answered",
        gold_units=("s02:slides:9",),
        split="tuning",
        corpus_version=SHA,
    )
    base.update(kw)
    return S.GoldenCase(**base)


# ---------- SCH-01 corpus ----------


def test_manifest_requires_unique_ids_and_paths():
    S.Manifest(schema_version="1.0", cohort="sample", entries=(entry(),))
    with pytest.raises(ValidationError, match="unique"):
        S.Manifest(schema_version="1.0", cohort="sample", entries=(entry(), entry()))
    with pytest.raises(ValidationError, match="unique"):
        S.Manifest(
            schema_version="1.0",
            cohort="sample",
            entries=(entry(), entry(manifest_id="m-other")),
        )


@pytest.mark.parametrize("path", ["/etc/x.pdf", "../x.pdf", "a/../../x.pdf", "C:\\x.pdf"])
def test_manifest_paths_are_relative(path):
    with pytest.raises(ValidationError):
        entry(path=path)


def test_unit_index_is_one_based_and_kind_is_closed():
    unit: dict[str, Any] = dict(
        unit_index=1, file_id="f", vs_file_id="v", manifest_id="m", session="s01", kind="slides",
        locator="slide 1", label="Session 1 slides, slide 1", text="x", version=SHA,
        corpus_version=SHA,
    )  # fmt: skip
    S.CorpusUnit(**unit)
    with pytest.raises(ValidationError):
        S.CorpusUnit(**{**unit, "unit_index": 0})
    with pytest.raises(ValidationError):
        S.CorpusUnit(**{**unit, "kind": "video"})


@pytest.mark.parametrize("field", ["solution", "model_answer", "answer_key", "rubric_answer"])
def test_assessment_refuses_any_solution_field(field):
    with pytest.raises(ValidationError):
        S.Assessment(
            assessment_id="s01-a1", title="A1", brief="Write a brief.", sessions=("s01",),
            **{field: "the answer"},
        )  # fmt: skip


# ---------- SCH-02 request ----------


def test_answered_needs_citation_grounding_and_integrity():
    answer()
    with pytest.raises(ValidationError, match="citation"):
        answer(citations=())
    with pytest.raises(ValidationError, match="grounding"):
        answer(grounding=None)
    with pytest.raises(ValidationError, match="integrity"):
        answer(integrity_passed=None)


@pytest.mark.parametrize("status", ["not_covered", "handed_over", "error"])
def test_non_answers_carry_no_citations(status):
    answer(status=status, citations=(), grounding=None, integrity_passed=None)
    with pytest.raises(ValidationError, match="no citations"):
        answer(status=status, grounding=None, integrity_passed=None)


def test_grounding_passed_must_match_its_parts():
    with pytest.raises(ValidationError):
        S.GroundingResult(
            citations_resolve=True, top_score=0.1, bar=0.5, sentence_support=(True,),
            passed=True, ground_version="g1",
        )  # fmt: skip
    with pytest.raises(ValidationError):
        S.GroundingResult(
            citations_resolve=True, top_score=0.9, bar=0.5, sentence_support=(),
            passed=True, ground_version="g1",
        )  # fmt: skip


def test_route_assessment_only_for_course_or_graded_work():
    base: dict[str, Any] = dict(
        question_id=QID, confidence=0.9, standalone_question="q?", route_version="r1"
    )
    S.Route(category="graded_work", assessment_id="s01-a1", **base)
    with pytest.raises(ValidationError):
        S.Route(category="admin", assessment_id="s01-a1", **base)


def test_turn_index_is_one_based_and_pid_is_not_an_email():
    q: dict[str, Any] = dict(
        question_id=QID, conversation_id=CID, turn_index=1, learner_pid=PID,
        session_scope=None, text="hi", asked_at=T0,
    )  # fmt: skip
    S.Question(**q)
    with pytest.raises(ValidationError):
        S.Question(**{**q, "turn_index": 0})
    for bad in ("someone@example.com", "Jane Doe", "0123456789ABCDEF", "0123"):
        with pytest.raises(ValidationError):
            S.Question(**{**q, "learner_pid": bad})


# ---------- SCH-03 people ----------


def test_escalation_reply_rules():
    base: dict[str, Any] = dict(question_id=QID, reason="not_covered", created_at=T0)
    S.Escalation(status="open", **base)
    with pytest.raises(ValidationError):
        S.Escalation(status="replied", **base)
    with pytest.raises(ValidationError):
        S.Escalation(status="open", reply_text="x", **base)
    with pytest.raises(ValidationError, match="before"):
        S.Escalation(
            status="replied", reply_text="See slide 9.", replied_by="instructor-1",
            replied_at=T0 - dt.timedelta(minutes=1), **base,
        )  # fmt: skip
    with pytest.raises(ValidationError):
        S.Escalation(status="replied", reply_text="x", replied_by="Jane Doe", replied_at=T0, **base)


def test_consent_and_deletion_timestamps():
    S.Consent(learner_pid=PID, notice_version="n1", purposes=("answer_questions",), given_at=T0)
    with pytest.raises(ValidationError):
        S.Consent(learner_pid=PID, notice_version="n1", purposes=(), given_at=T0)
    with pytest.raises(ValidationError):
        S.Consent(
            learner_pid=PID, notice_version="n1",
            purposes=("answer_questions", "answer_questions"), given_at=T0,
        )  # fmt: skip
    with pytest.raises(ValidationError):
        S.Consent(
            learner_pid=PID, notice_version="n1", purposes=("answer_questions",), given_at=T0,
            withdrawn_at=T0 - dt.timedelta(seconds=1),
        )  # fmt: skip
    d = S.DeletionRequest(learner_pid=PID, requested_at=T0)
    assert not d.complete
    with pytest.raises(ValidationError):
        S.DeletionRequest(learner_pid=PID, requested_at=T0, app_deleted_at=T0 - dt.timedelta(1))


def test_audit_event_cannot_hold_free_text():
    S.AuditEvent(at=T0, actor="instructor-1", action="reply", target=QID)
    with pytest.raises(ValidationError):
        S.AuditEvent(at=T0, actor="instructor-1", action="I asked about my extension")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        S.AuditEvent(at=T0, actor="instructor-1", action="reply", target="my father is ill")
    with pytest.raises(ValidationError):
        S.AuditEvent(at=T0, actor="Jane Doe", action="reply")


def test_pause_scope_and_digest_week():
    S.PauseState(scope="all", paused=True, set_by="instructor-1", set_at=T0)
    S.PauseState(scope="s02", paused=True, set_by="instructor-1", set_at=T0)
    with pytest.raises(ValidationError):
        S.PauseState(scope="session 2", paused=True, set_by="instructor-1", set_at=T0)
    S.DigestRow(week="2026-W48", session="s02", topic="judge calibration", count=5)
    with pytest.raises(ValidationError):
        S.DigestRow(week="2026-W60", session="s02", topic="t", count=5)
    with pytest.raises(ValidationError):
        S.DigestRow(week="2026-W48", session="s02", topic="t", count=0)


# ---------- SCH-04 config and golden cases ----------


@pytest.mark.parametrize(
    ("kw", "msg"),
    [
        ({"rule": "B1", "gold_units": ()}, "gold_units"),
        ({"rule": "B3", "expected_status": "hint_only", "gold_units": ()}, "assessment_id"),
        ({"rule": "B8", "expected_status": "not_covered", "gold_units": ()}, "canary"),
        ({"rule": "B10", "gold_units": ()}, "canary"),
        ({"rule": "B2", "expected_status": "answered"}, "cannot expect"),
        ({"rule": "B4", "expected_status": "not_covered"}, "cannot expect"),
    ],
)
def test_golden_case_rule_invariants(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        golden(**kw)


def test_golden_prior_turns_are_pairs_of_non_blank_text():
    golden(prior_turns=(("What is a golden set?", "A fixed set of test cases [s02 slide 3]."),))
    with pytest.raises(ValidationError):
        golden(prior_turns=(("q", " "),))


def test_app_config_requires_explicit_effort(tmp_path):
    good = tmp_path / "config.yaml"
    good.write_text(
        "schema_version: '1.0'\ncohort: sample\nvector_store_id: vs_1\n"
        "answer_model: gpt-6.1-sol\ncheck_model: gpt-6-luna\n"
        "reasoning_effort: {gpt-6.1-sol: low, gpt-6-luna: low}\n"
        "grounding_bar: 0.4\nmonthly_budget_usd: 100\n",
        encoding="utf-8",
    )
    assert load_app_config(good).digest_k == 5
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        good.read_text(encoding="utf-8").replace(", gpt-6-luna: low", ""), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match=r"bad\.yaml.*reasoning_effort"):
        load_app_config(bad)


@pytest.mark.parametrize(
    ("content", "msg"),
    [
        (b"schema_version: '1.0'\ncohort: a\ncohort: b\nentries: []\n", "duplicate key"),
        (b"\xff\xfe\x00bad", "not UTF-8"),
        (b"- a\n- b\n", "mapping"),
        (b"schema_version: '1.0'\ncohort: a\nentries: []\n", "field entries"),
        (b"schema_version: '2.0'\ncohort: a\nentries: []\n", "schema_version"),
    ],
)
def test_loaders_name_the_file_and_the_problem(tmp_path, content, msg):
    f = tmp_path / "manifest.yaml"
    f.write_bytes(content)
    with pytest.raises(ConfigError, match=msg) as exc:
        load_manifest(f)
    assert "manifest.yaml" in str(exc.value)


def test_loader_refuses_a_directory(tmp_path):
    with pytest.raises(ConfigError, match="directory"):
        load_manifest(tmp_path)


# ---------- SCH-05 immutability, strictness and exports ----------


def test_models_are_deeply_immutable_and_hashable():
    a = answer()
    with pytest.raises(ValidationError):
        a.status = "error"  # type: ignore[misc]
    assert isinstance(a.citations, tuple)
    hash(a)
    cfg = S.AppConfig(
        schema_version="1.0", cohort="c", vector_store_id="v", answer_model="m1",
        check_model="m2", reasoning_effort={"m1": "low", "m2": "low"}, grounding_bar=0.4,
        monthly_budget_usd=100,
    )  # fmt: skip
    with pytest.raises(TypeError):
        cfg.reasoning_effort["m1"] = "high"  # type: ignore[index]


@pytest.mark.parametrize(
    "kw",
    [
        {"cost_usd": float("nan")},
        {"cost_usd": float("inf")},
        {"latency_ms": True},
        {"latency_ms": "12"},
        {"created_at": dt.datetime(2026, 11, 15)},
        {"surprise": 1},
    ],
)
def test_strict_values_are_enforced(kw):
    with pytest.raises(ValidationError):
        answer(**kw)


def test_persisted_files_require_schema_version():
    with pytest.raises(ValidationError):
        S.Manifest(cohort="c", entries=(entry(),))  # type: ignore[call-arg]


@pytest.mark.contract
def test_every_tech_spec_schema_is_exported():
    """The package exports exactly the Tech Spec §3 models: none missing, none extra (L-4)."""
    from pydantic import BaseModel

    spec = (ROOT / "AI Teaching Assistant Tech Spec.md").read_text(encoding="utf-8")
    section = spec.split("## 3. Schemas")[1].split("## 4. Interfaces")[0]
    names = set(re.findall(r"^class (\w+)\(BaseModel\):", section, re.M))
    assert len(names) >= 21
    exported = {
        n
        for n in S.__all__
        if isinstance(getattr(S, n), type) and issubclass(getattr(S, n), BaseModel)
    }
    assert exported == names


@pytest.mark.contract
def test_models_round_trip_through_json_byte_for_byte():
    samples = [
        answer(),
        entry(),
        golden(),
        S.Escalation(
            question_id=QID, reason="admin", status="replied", reply_text="Done.",
            replied_by="instructor-1", created_at=T0, replied_at=T0,
            draft=S.Draft(text="d", citations=(cite(),)),
        ),
        S.Consent(
            learner_pid=PID, notice_version="n1", purposes=("answer_questions",), given_at=T0
        ),
        S.AuditEvent(at=T0, actor=PID, action="delete_history"),
    ]  # fmt: skip
    for model in samples:
        dumped = model.model_dump_json()
        again = type(model).model_validate_json(dumped)
        assert again == model
        assert again.model_dump_json() == dumped


@pytest.mark.parametrize(
    "kw",
    [
        {"cost_usd": True},
        {"top_search_score": "1e3"},
        {"integrity_passed": "yes"},
        {"integrity_passed": 1},
    ],
)
def test_no_coercion_of_bools_or_numeric_strings(kw):
    with pytest.raises(ValidationError):
        answer(**kw)


def test_dates_and_bools_are_not_coerced_elsewhere():
    with pytest.raises(ValidationError):
        entry(approved_at=0)
    with pytest.raises(ValidationError):
        S.Route(question_id=QID, category="course", confidence=True, standalone_question="q",
                route_version="r")  # fmt: skip


@pytest.mark.parametrize("who", ["jane.doe@example.ac.in", "Jane Doe", "owner-biplob"])
def test_staff_are_role_ids_never_names(who):
    with pytest.raises(ValidationError):
        entry(approved_by=who)


def test_audit_target_cannot_hold_a_slug_sentence():
    with pytest.raises(ValidationError):
        S.AuditEvent(
            at=T0, actor="ta-1", action="reply",
            target="my-father-is-ill-so-i-need-an-extension-on-a2",
        )  # fmt: skip


def test_consent_withdrawal_must_be_after_giving():
    with pytest.raises(ValidationError):
        S.Consent(
            learner_pid=PID, notice_version="n1", purposes=("answer_questions",),
            given_at=T0, withdrawn_at=T0,
        )  # fmt: skip


def test_allowed_status_cannot_be_changed():
    with pytest.raises(TypeError):
        S.ALLOWED_STATUS["B2"] = ("answered",)  # type: ignore[index]


def test_model_copy_with_update_is_validated():
    with pytest.raises(ValidationError):
        answer().model_copy(update={"citations": ()})
    assert answer().model_copy(update={"latency_ms": 10}).latency_ms == 10


def test_manifest_paths_are_compared_normalised_and_refuse_nul_and_tilde():
    with pytest.raises(ValidationError, match="unique"):
        S.Manifest(
            schema_version="1.0", cohort="c",
            entries=(entry(path="a/b.pdf"), entry(manifest_id="m-2", path="a//b.pdf")),
        )  # fmt: skip
    for bad in ("a\x00b.pdf", "~/x.pdf"):
        with pytest.raises(ValidationError):
            entry(path=bad)


def test_yaml_errors_never_echo_file_content(tmp_path):
    f = tmp_path / "manifest.yaml"
    f.write_text('cohort: a\nnote: "jane.doe@example.com private\n  : [', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_manifest(f)
    assert "jane" not in str(exc.value) and "line" in str(exc.value)


def test_frozen_maps_copy_and_pickle():
    import copy
    import pickle

    cfg = S.AppConfig(
        schema_version="1.0", cohort="c", vector_store_id="v", answer_model="m1",
        check_model="m2", reasoning_effort={"m2": "low", "m1": "low"}, grounding_bar=0.4,
        monthly_budget_usd=100,
    )  # fmt: skip
    assert copy.deepcopy(cfg) == cfg
    assert cfg.model_copy(deep=True) == cfg
    assert pickle.loads(pickle.dumps(cfg)) == cfg
    assert list(cfg.reasoning_effort) == ["m1", "m2"]  # keys sorted: equal maps, equal JSON


def test_equal_instants_serialise_identically():
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    a = answer(created_at=T0)
    b = answer(created_at=T0.astimezone(ist))
    assert a == b and a.model_dump_json() == b.model_dump_json()


@pytest.mark.parametrize("value", ["on", "yes", "1:30", "0x10"])
def test_yaml_11_scalars_are_not_coerced(tmp_path, value):
    f = tmp_path / "config.yaml"
    f.write_text(
        "schema_version: '1.0'\ncohort: c\nvector_store_id: v\nanswer_model: m1\n"
        "check_model: m2\nreasoning_effort: {m1: low, m2: low}\ngrounding_bar: 0.4\n"
        f"monthly_budget_usd: 100\ndigest_k: {value}\n",
        encoding="utf-8",
    )
    if value == "0x10":
        assert load_app_config(f).digest_k == 16  # YAML 1.2 core hex is an int
    else:
        with pytest.raises(ConfigError):
            load_app_config(f)


def test_unknown_yaml_keys_are_not_echoed(tmp_path):
    f = tmp_path / "manifest.yaml"
    f.write_text(
        "schema_version: '1.0'\ncohort: c\nentries: []\n'Ravi Kumar ravi@x.com': 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_manifest(f)
    assert "ravi" not in str(exc.value).lower()


@pytest.mark.parametrize("bad", ["ravi.kumar.gmail.com", "q1", "priya_sharma:9876543210"])
def test_names_cannot_be_ids(bad):
    with pytest.raises(ValidationError):
        S.Escalation(question_id=bad, reason="admin", status="open", created_at=T0)


def test_closed_escalation_cannot_have_partial_reply():
    with pytest.raises(ValidationError):
        S.Escalation(question_id=QID, reason="admin", status="closed", created_at=T0,
                     replied_by="instructor-1")  # fmt: skip
