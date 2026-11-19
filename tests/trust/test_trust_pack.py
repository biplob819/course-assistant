"""The trust pack holds only claims that resolve (T3.1, OWNER D-72; L-8, L-16).

Every test named in `docs/trust/` exists, every repo path it cites exists, every decision it
cites is in `docs/decisions.md`, and the behavioural spec lists exactly the public golden case
ids of each rule. Offline: reads tracked files only, never `golden/heldout/` content.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from functools import cache
from pathlib import Path

import pytest

from harness_lib import CATALOG_TSV, ROOT, parse_catalog

TRUST = ROOT / "docs" / "trust"
DOCS = sorted(TRUST.glob("*.md"))
SPEC = TRUST / "behavioral-spec.md"
DPDP = TRUST / "dpdp-mapping.md"
RUNBOOK = TRUST / "incident-runbook.md"
PUBLIC = ROOT / "golden" / "public" / "cases.jsonl"
RULES = [f"B{n}" for n in range(1, 11)]

SPAN = re.compile(r"`([^`\n]+)`")
TEST_REF = re.compile(r"^(?:(?P<path>[\w./-]*test_\w+\.py))?::(?P<name>\w+)$")
TEST_FILE = re.compile(r"^[\w./-]*test_\w+\.py$")
PATH_REF = re.compile(
    r"^[\w.-]+(?:/[\w.-]+)*(?:\.(?:py|md|json|jsonl|toml|yaml|yml|svg|gif|txt|cfg)|/)$"
)
MD_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
DECISION = re.compile(r"\bD-(\d+)\b")
CASE_ID = re.compile(r"\bta-b\d+-\d+\b")
# Git-ignored by design (PRIVACY.md, On your own machine); cited, never committed.
IGNORED = ("runs/", ".evalkit/", "corpus_store/", "golden/heldout/src/", "golden/heldout/retired/")


def _resolve(path: str) -> Path:
    for base in (ROOT, ROOT / "tests", TRUST):
        if (base / path).exists():
            return base / path
    return ROOT / path


@cache
def _functions(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return frozenset(
        n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    )


def _test_refs(text: str) -> list[tuple[str, str]]:
    """(path, name) for every `path.py::name` span; a bare `::name` takes the line's last file."""
    refs = []
    for line in text.splitlines():
        last = None
        for span in SPAN.findall(line):
            if TEST_FILE.match(span.strip()):
                last = span.strip()
                continue
            m = TEST_REF.match(span.strip())
            if not m:
                continue
            last = m.group("path") or last
            assert last, f"`{span}` has no test file before it on its line"
            refs.append((last, m.group("name")))
    return refs


def _public_ids_by_rule() -> dict[str, list[str]]:
    ids: dict[str, list[str]] = {r: [] for r in RULES}
    for line in PUBLIC.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        ids[case["rule"]].append(case["id"])
    return ids


def test_the_pack_exists():
    names = {p.name for p in DOCS}
    assert {"behavioral-spec.md", "dpdp-mapping.md", "incident-runbook.md"} <= names


def test_ref_parser_reuses_the_lines_last_path():
    line = (
        "`answer/test_ask.py::test_a`, `::test_b`; `tests/x/test_y.py::test_c`; "
        "`x/test_z.py` (`::test_d`)"
    )
    assert _test_refs(line) == [
        ("answer/test_ask.py", "test_a"),
        ("answer/test_ask.py", "test_b"),
        ("tests/x/test_y.py", "test_c"),
        ("x/test_z.py", "test_d"),
    ]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_named_test_exists(doc: Path):
    missing = []
    for path, name in _test_refs(doc.read_text(encoding="utf-8")):
        file = _resolve(path)
        if not file.is_file() or name not in _functions(file):
            missing.append(f"{path}::{name}")
    assert not missing, missing


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_cited_path_exists(doc: Path):
    text = doc.read_text(encoding="utf-8")
    cited = [s.split("::")[0] for s in SPAN.findall(text) if PATH_REF.match(s.split("::")[0])]
    cited += [u for u in MD_LINK.findall(text) if not u.startswith(("http://", "https://"))]
    missing = []
    for path in cited:
        if path.startswith(IGNORED):
            continue
        if not (_resolve(path).exists() or (doc.parent / path).resolve().exists()):
            missing.append(path)
    assert not missing, missing


@pytest.mark.parametrize(
    "doc", [*DOCS, ROOT / "DATA_CARD.md", ROOT / "PRIVACY.md"], ids=lambda p: p.name
)
def test_every_cited_decision_is_logged(doc: Path):
    logged = set(
        re.findall(
            r"^\|\s*(?:OWNER )?D-(\d+)\s*\|",
            (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8"),
            re.M,
        )
    )
    cited = set(DECISION.findall(doc.read_text(encoding="utf-8")))
    assert cited - logged == set(), sorted(cited - logged, key=int)


def test_behavioral_spec_lists_exactly_the_public_cases_of_each_rule():
    """TRU-01: each rule's line under 'Public cases' names every public id of that rule only."""
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("## Public cases", 1)[1].split("\n## ", 1)[0]
    listed: dict[str, list[str]] = {}
    for line in section.splitlines():
        m = re.match(r"^- \*\*(B\d+)\*\* \((\d+)\)", line)
        if m:
            ids = CASE_ID.findall(line)
            assert int(m.group(2)) == len(ids), (
                f"{m.group(1)}: count {m.group(2)} vs {len(ids)} ids"
            )
            listed[m.group(1)] = ids
    public = _public_ids_by_rule()
    for rule in RULES:
        assert rule in listed, f"{rule} has no line under Public cases"
        assert listed[rule] == public[rule], rule


def test_behavioral_spec_gives_every_rule_a_result_row_with_a_held_out_slot():
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("## Rules, cases and results", 1)[1].split("\n## ", 1)[0]
    rows = [r for r in section.splitlines() if re.match(r"^\| B\d+ ", r)]
    assert [r.split("|")[1].strip() for r in rows] == RULES
    for row in rows:
        held_out = row.split("|")[-2]
        assert "PENDING (heldout-4)" in held_out or re.search(r"\d", held_out), row


def test_data_card_public_counts_match_the_public_cases():
    """TRU-02: the public column of DATA_CARD.md is the count in golden/public/cases.jsonl."""
    counts = Counter(
        json.loads(line)["rule"] for line in PUBLIC.read_text(encoding="utf-8").splitlines()
    )
    card = (ROOT / "DATA_CARD.md").read_text(encoding="utf-8")
    for rule, n in counts.items():
        row = next(r for r in card.splitlines() if re.match(rf"^\| {rule} ", r))
        assert row.split("|")[4].strip() == str(n), (rule, row)
    total = next(r for r in card.splitlines() if r.startswith("| **Total**"))
    assert total.split("|")[4].strip() == f"**{sum(counts.values())}**"


def test_the_pack_links_the_data_card_and_the_privacy_notice():
    text = "\n".join(p.read_text(encoding="utf-8") for p in DOCS)
    assert "DATA_CARD.md" in text and "PRIVACY.md" in text


def test_dpdp_mapping_has_a_uk_gdpr_column_and_every_required_duty():
    text = DPDP.read_text(encoding="utf-8")
    header = next(r for r in text.splitlines() if r.startswith("| Duty"))
    assert "UK GDPR" in header and "Proof" in header
    rows = [
        r for r in text.splitlines() if r.startswith("| ") and not r.startswith(("| Duty", "| ---"))
    ]
    duties = " ".join(r.split("|")[1].lower() for r in rows)
    for duty in ("notice", "consent", "withdrawal", "erasure", "safeguards", "breach", "processor"):
        assert duty in duties, duty


def test_every_dpdp_control_names_a_test_or_is_marked_as_no_code():
    """TRU-03: a row's proof is a named test, or the row says plainly it has no code."""
    text = DPDP.read_text(encoding="utf-8")
    cols = [
        c.strip() for c in next(r for r in text.splitlines() if r.startswith("| Duty")).split("|")
    ]
    proof, status = cols.index("Proof"), cols.index("Status")
    for row in text.splitlines():
        if not row.startswith("| ") or row.startswith(("| Duty", "| ---")):
            continue
        cells = row.split("|")
        has_test = bool(_test_refs(cells[proof]))
        no_code = re.search(r"no code|gap", cells[status], re.I)
        assert has_test or no_code, row


def test_runbook_covers_detect_pause_notify_and_the_72_hour_report():
    text = RUNBOOK.read_text(encoding="utf-8").lower()
    for step in ("## detect", "pause", "notify the data fiduciary", "72 hours"):
        assert step in text, step


# B9 (D-85): the fixed texts are shown with no model in between, so no judge reads them at run
# time. None may claim a person or staff identity, or promise an outcome for the teaching team.
IDENTITY_CLAIM = re.compile(
    r"\b(?:i am|i'm)\s+(?:a |an |the |your )?(?:human|person|real|member of staff|staff|tutor|"
    r"instructor|professor|teacher|lecturer|faculty)\b"
    r"|\b(?:will|shall)\s+(?:reply|respond|get back|grant|approve|extend|give you)\b"
    r"|\bon (?:their|our|its) behalf\b|\bwe (?:will|promise|guarantee|approve)\b",
    re.I,
)


def test_identity_claim_pattern_catches_the_old_handover_text_and_plain_claims():
    for bad in ("They will reply here.", "I'm a tutor on this course.", "We will approve it."):
        assert IDENTITY_CLAIM.search(bad), bad


def test_no_fixed_text_claims_a_person_or_promises_for_the_teaching_team():
    from course_assistant.answer.ask import Assistant

    texts = Assistant.fixed_texts()
    assert "handed_over" in texts
    bad = {k: v for k, v in texts.items() if IDENTITY_CLAIM.search(v)}
    assert not bad, bad


# Owner, 19 Nov: no PENDING marker of any kind remains in docs/trust/ at green. Drafts carry
# `PENDING (heldout-4)` and `PENDING (D3.2 owner attest)`, so the check bites only once T3.1 is
# green; the verifier can call `pending_markers()` at any time.
PENDING = re.compile(r"\bPENDING\b")


def pending_markers(docs: list[Path] = DOCS) -> list[str]:
    """`file:line` of every PENDING marker in the trust pack."""
    return [
        f"{p.name}:{n}"
        for p in docs
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if PENDING.search(line)
    ]


def node_is_green(node: str, catalog: str) -> bool:
    caps = [c for c in parse_catalog(catalog) if c.node == node]
    return bool(caps) and all(c.status == "green" for c in caps)


def test_pending_helpers_find_markers_and_read_node_status(tmp_path):
    doc = tmp_path / "x.md"
    doc.write_text(
        "ok\nvalue `PENDING (heldout-4)`\npending in lower case is prose\n", encoding="utf-8"
    )
    assert pending_markers([doc]) == ["x.md:2"]
    hdr = "cap_id\tnode\tstatus\tproof\tnote\n"
    assert node_is_green("T3.1", hdr + "TRU-01\tT3.1\tgreen\tp\tn\n")
    assert not node_is_green(
        "T3.1", hdr + "TRU-01\tT3.1\tgreen\tp\tn\nTRU-02\tT3.1\tbuilding\t\tn\n"
    )
    assert not node_is_green("T3.1", hdr)


def test_no_pending_marker_remains_in_the_pack_at_green():
    if not node_is_green("T3.1", CATALOG_TSV.read_text(encoding="utf-8")):
        pytest.skip("T3.1 is not green; drafts may carry PENDING markers (OWNER D-72)")
    assert not pending_markers(), pending_markers()
