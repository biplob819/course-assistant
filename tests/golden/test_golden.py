"""E2.1: the golden set. Public checks always run; held-out checks run where the private
file exists (the owner's machine), and the committed manifest pins it everywhere."""

from __future__ import annotations

import json
from collections import Counter

import pytest
import yaml

import golden_tool as gt
from course_assistant.schemas import GoldenCase
from harness_lib import ROOT

PUBLIC = gt.read(gt.PUBLIC)
# the seeded split's public half; the public supplement (D-84, ids from 701) comes after it
SEEDED = [c for c in PUBLIC if int(c.id.rsplit("-", 1)[1]) <= gt.EXTRA_START]
SUPPLEMENT = PUBLIC[len(SEEDED) :]
HELD = gt.read(gt.HELDOUT)
MANIFEST = json.loads(gt.MANIFEST.read_text(encoding="utf-8"))
CORE = {"B1": 150, "B2": 30, "B7": 10, "B3": 30, "B4": 20, "B8": 10, "B9": 10, "B10": 20}


def _all_counts() -> Counter:
    counts = Counter(c.rule for c in SEEDED)
    counts.update(MANIFEST["counts_by_rule"])
    return counts


# ---------- GLD-01 ----------


def test_composition_matches_the_tech_spec_plus_extension():
    counts = _all_counts()
    expected = dict(CORE)
    expected["B3"] += 90  # three paraphrases of each B3 probe (D-31)
    expected["B10"] += 60  # three paraphrases of each B10 probe (D-31)
    expected["B1"] -= len(gt.RETIRED)  # held-out cases whose text became public (D-54)
    assert dict(counts) == expected
    assert sum(counts.values()) == 430 - len(gt.RETIRED)


def test_public_cases_are_valid_with_known_locators_and_assessments():
    assert gt.problems(PUBLIC) == []
    assert all(c.gold_units for c in PUBLIC if c.rule == "B1")
    assert all(c.canary for c in PUBLIC if c.rule in ("B8", "B10"))
    assert all(c.split == "tuning" for c in PUBLIC)


@pytest.mark.contract
def test_every_line_is_a_versioned_golden_case():
    for line in gt.PUBLIC.read_text(encoding="utf-8").splitlines():
        case = GoldenCase.model_validate_json(line)
        assert case.schema_version == "1.0"
        assert case.corpus_version == gt.corpus_version()


# ---------- GLD-02 ----------


def test_split_is_roughly_half_and_half_by_rule():
    """Split by merged topic and assessment groups (D-34): halves are approximate, bounded."""
    held = MANIFEST["counts_by_rule"]
    pub = Counter(c.rule for c in SEEDED)
    for rule in CORE:
        total = pub[rule] + held[rule]
        assert pub[rule] and held[rule], rule
        assert abs(pub[rule] - held[rule]) <= max(2, 0.25 * total), rule


@pytest.mark.safety
def test_heldout_cases_are_private_and_pinned_by_the_manifest():
    import subprocess

    for path in ("golden/heldout/cases.jsonl", "golden/heldout/src/cases_b1.py"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0
    assert MANIFEST["count"] == len(MANIFEST["cases"]) == 430 - len(SEEDED) - len(gt.RETIRED)
    assert not set(MANIFEST["cases"]) & gt.RETIRED
    assert not set(MANIFEST["cases"]) & {c.id for c in PUBLIC}
    if HELD:  # owner's machine: the private file must match the committed manifest
        assert gt.main(["verify"]) == 0


def test_build_is_deterministic():
    if not gt.SRC.is_dir():
        assert gt.PUBLIC.is_file()  # contributors see only the committed public split
        return
    a, b = gt.build_cases(), gt.build_cases()
    gt.split(a)
    gt.split(b)
    assert [gt.to_line(c) for c in a] == [gt.to_line(c) for c in b]
    assert [gt.to_line(c) for c in a if c["split"] == "tuning"] + gt.extra_lines() == [
        line for line in gt.PUBLIC.read_text(encoding="utf-8").splitlines()
    ]


def test_the_supplement_is_public_b9_after_the_seeded_split():
    assert PUBLIC == SEEDED + SUPPLEMENT
    assert SUPPLEMENT and all(c.rule == "B9" and c.split == "tuning" for c in SUPPLEMENT)
    assert not {c.id for c in SUPPLEMENT} & set(MANIFEST["cases"])


# ---------- GLD-04 ----------


def test_coverage_rule_holds():
    cases = PUBLIC + HELD
    if HELD:
        assert gt.coverage(cases) == []
    b1 = Counter(c.gold_units[0].split(":")[0] for c in PUBLIC if c.rule == "B1")
    assert all(b1[s] >= 7 for s in ("s01", "s02", "s03"))  # half of 15 on the public side


def test_absent_topic_questions_name_an_absent_topic():
    topics = yaml.safe_load(
        (ROOT / "sample_course" / "absent_topics.yaml").read_text(encoding="utf-8")
    )
    keys = [k for t in topics["topics"] for k in t["keywords"]] + ["rlhf", "nvidia", "lora"]
    for c in PUBLIC:
        if c.rule == "B2":
            assert any(k in c.question.lower() for k in keys), c.id


# ---------- GLD-05 / GLD-06 ----------


def test_paraphrases_keep_their_source_status_assessment_and_canary():
    by_q: dict[tuple, list[GoldenCase]] = {}
    for c in PUBLIC + HELD:
        if c.rule in ("B3", "B10"):
            by_q.setdefault((c.rule, c.assessment_id, c.canary, c.expected_status,
                             tuple(map(tuple, c.prior_turns))), []).append(c)  # fmt: skip
    sizes = Counter(len(v) for v in by_q.values())
    assert set(sizes) <= {4, 8, 12, 16, 20}  # each probe with its three paraphrases


def test_multi_turn_cases_are_well_formed():
    multi = [c for c in PUBLIC if c.prior_turns]
    assert multi
    for c in multi:
        assert all(q.strip() and a.strip() for q, a in c.prior_turns)


@pytest.mark.safety
def test_no_held_out_text_or_topic_reaches_the_public_split():
    if HELD:  # owner's machine: the full check runs against the private file
        assert gt.leaks(PUBLIC, HELD) == []
    public_ids = {c.id for c in PUBLIC}
    assert public_ids.isdisjoint(MANIFEST["cases"])


@pytest.mark.privacy
def test_no_heldout_question_appears_in_any_tracked_file():
    """D-54: a held-out question that shows up in a public file is no longer held out. Runs on
    the owner's machine, where the private file exists; reports case ids only."""
    import subprocess

    if not HELD:
        pytest.skip("held-out file is private; checked on the owner's machine and in E2.4")
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                           check=True).stdout.decode().split("\0")  # fmt: skip
    text = []
    for f in files:
        path = ROOT / f
        if f and path.is_file() and path.suffix in {".py", ".md", ".json", ".jsonl", ".yaml",
                                                     ".yml", ".txt", ".toml", ".csv"}:  # fmt: skip
            text.append(path.read_text(encoding="utf-8", errors="ignore"))
    corpus = "\n".join(text)
    exposed = [c.id for c in HELD if len(c.question) >= 25 and c.question in corpus]
    assert exposed == [], f"held-out cases exposed in tracked files: {exposed}"
