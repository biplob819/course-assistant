"""Owner-attest rows inside a code node (D-60, D3.2's DEM-04).

A row whose note ends `(owner attest)` (before its trace bracket) is pending until the owner
attests it: converge passes it while it is not green, and it can be green only with the proof
`attest=OWNER D-n` whose ruling opens `Attests <node>`. Other rows are unchanged."""

import node_converge as nc
from harness_lib import eligibility_problems, parse_catalog, parse_graph

HDR = "cap_id\tnode\tstatus\tproof\tnote\n"
GRAPH = parse_graph(
    "| **W0.1** | Harness | — | node_converge + verifier |\n"
    "| **D3.2** | Deploy | W0.1 | node_converge + verifier |\n"
)
NOTE = "Live deploy; smoke check passes (owner attest) [US10]"
GOOD = (
    'logical=tests/t.py›"test_a"; contract=n/a:no contract here at all; golden=n/a:no golden '
    "here at all; repro=n/a:no repro here at all; safety=n/a:no safety here at all"
)


def _setup(root):
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "decisions.md").write_text(
        "| OWNER D-61 | d | Attests D3.2: the live app passed the smoke check | y | z |\n"
        "| OWNER D-62 | d | Attests G0.1: something else | y | z |\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "t.py").write_text("def test_a():\n    pass\n", encoding="utf-8")


def _gate(rows: str, root) -> nc.Result:
    caps = parse_catalog(HDR + "H\tW0.1\tgreen\tx\t\n" + rows)
    return nc.gate_proofs(nc.Ctx("D3.2", root, GRAPH, caps, root / "specs"))


def test_the_mark_is_exact_and_only_at_the_end_of_the_note():
    caps = parse_catalog(
        HDR + f"A\tD3.2\tbuilding\t\t{NOTE}\n"
        "B\tD3.2\tbuilding\t\tNo trace (owner attest)\n"
        "C\tD3.2\tbuilding\t\tthe owner attest step is later [US10]\n"
        "D\tD3.2\tbuilding\t\t(owner attest) then more words [US10]\n"
        "E\tD3.2\tbuilding\t\t(Owner Attest) [US10]\n"
    )
    assert [c.owner_attest for c in caps] == [True, True, False, False, False]


def test_a_pending_owner_attest_row_passes_converge_with_its_siblings(tmp_path):
    _setup(tmp_path)
    r = _gate(f"D-1\tD3.2\tbuilding\t{GOOD}\tcode row [US10]\nD-4\tD3.2\tbuilding\t\t{NOTE}\n",
              tmp_path)  # fmt: skip
    assert r.status == "PASS" and "owner attest pending" in r.detail
    # its code siblings still need their five proofs
    r = _gate(f"D-1\tD3.2\tbuilding\t\tcode row [US10]\nD-4\tD3.2\tbuilding\t\t{NOTE}\n",
              tmp_path)  # fmt: skip
    assert r.status == "FAIL"


def test_a_green_owner_attest_row_needs_an_attest_naming_the_node(tmp_path):
    _setup(tmp_path)
    assert _gate(f"D-4\tD3.2\tgreen\tattest=OWNER D-61\t{NOTE}\n", tmp_path).status == "PASS"
    for proof in ("", "attest=OWNER D-62", "attest=OWNER D-99", "attest=me", GOOD):
        r = _gate(f"D-4\tD3.2\tgreen\t{proof}\t{NOTE}\n", tmp_path)
        assert r.status == "FAIL", proof


def test_a_pending_row_with_a_malformed_attest_fails(tmp_path):
    _setup(tmp_path)
    assert _gate(f"D-4\tD3.2\tbuilding\tattest=me\t{NOTE}\n", tmp_path).status == "FAIL"
    assert _gate(f"D-4\tD3.2\tbuilding\t{GOOD}\t{NOTE}\n", tmp_path).status == "FAIL"


def test_code_rows_without_the_mark_still_refuse_attest(tmp_path):
    _setup(tmp_path)
    r = _gate("D-1\tD3.2\tgreen\tattest=OWNER D-61\tcode row [US10]\n", tmp_path)
    assert r.status == "FAIL" and "only for external nodes" in r.detail


def test_eligibility_refuses_a_green_owner_attest_row_without_a_valid_attest(tmp_path):
    decisions = "| OWNER D-61 | d | Attests D3.2: live | y | z |\n"
    green_bad = parse_catalog(HDR + f"H\tW0.1\tgreen\tx\t\nD\tD3.2\tgreen\t{GOOD}\t{NOTE}\n")
    problems = eligibility_problems(GRAPH, green_bad, decisions_text=decisions)
    assert any("DEM" in p or "owner attest" in p for p in problems)
    green_ok = parse_catalog(
        HDR + f"H\tW0.1\tgreen\tx\t\nD\tD3.2\tgreen\tattest=OWNER D-61\t{NOTE}\n"
    )
    assert eligibility_problems(GRAPH, green_ok, decisions_text=decisions) == []
    pending = parse_catalog(HDR + f"H\tW0.1\tgreen\tx\t\nD\tD3.2\tbuilding\t\t{NOTE}\n")
    assert eligibility_problems(GRAPH, pending, decisions_text=decisions) == []
