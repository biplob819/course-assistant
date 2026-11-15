import node_converge as nc
from harness_lib import eligibility_problems, parse_catalog, parse_graph

HDR = "cap_id\tnode\tstatus\tproof\tnote\n"
GRAPH = parse_graph(
    "| **W0.1** | Harness | — | node_converge + verifier |\n"
    "| **G0.1** | Sign-off | W0.1 | owner attest |\n"
)


def _proofs(node: str, proof: str, tmp_path) -> nc.Result:
    caps = parse_catalog(HDR + f"X-1\t{node}\tgreen\t{proof}\t\n")
    return nc.gate_proofs(nc.Ctx(node, tmp_path, GRAPH, caps, tmp_path / "specs"))


def _decisions(root):
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "decisions.md").write_text(
        "| OWNER D-21 | d | Attests G0.1: signed | y | z |\n"
        "| OWNER D-30 | d | not G0.1 | y | z |\n",
        encoding="utf-8",
    )


def test_external_node_accepts_owner_attest(tmp_path):
    assert GRAPH["G0.1"].external and not GRAPH["W0.1"].external
    _decisions(tmp_path)
    assert _proofs("G0.1", "attest=OWNER D-21", tmp_path).status == "PASS"


def test_external_node_rejects_anything_else(tmp_path):
    for proof in ("attest=me", "attest=D-21", 'logical=tests/t.py›"test_a"', ""):
        assert _proofs("G0.1", proof, tmp_path).status == "FAIL", proof


def test_code_node_never_accepts_attest(tmp_path):
    r = _proofs("W0.1", "attest=OWNER D-21", tmp_path)
    assert r.status == "FAIL" and "only for external nodes" in r.detail


def test_attest_must_name_a_recorded_ruling_in_ascii(tmp_path):
    _decisions(tmp_path)
    assert _proofs("G0.1", "attest=OWNER D-22", tmp_path).status == "FAIL"
    assert _proofs("G0.1", "attest=OWNER D-\u0662\u0661", tmp_path).status == "FAIL"


def test_prose_mentioning_attest_does_not_make_a_node_external():
    g = parse_graph("| **N1.1** | S | — | converge + attestation-free tests |\n")
    assert not g["N1.1"].external


def test_external_node_counts_green_only_with_attest_proofs():
    g = parse_graph(
        "| **W0.1** | H | — | node_converge + verifier |\n"
        "| **G0.1** | Sign-off | W0.1 | owner attest |\n"
        "| **P4.1** | Pilot | G0.1 | owner attest |\n"
    )
    caps = parse_catalog(HDR + "H\tW0.1\tgreen\tx\t\nS\tG0.1\tgreen\t\t\nP\tP4.1\tbuilding\t\t\n")
    problems = eligibility_problems(g, caps)
    assert any("G0.1" in p and "without a valid attest" in p for p in problems)


def test_attest_ruling_must_name_its_node(tmp_path):
    _decisions(tmp_path)
    r = _proofs("G0.1", "attest=OWNER D-30", tmp_path)
    assert r.status == "FAIL" and "Attests G0.1" in r.detail


def test_eligibility_refuses_a_green_external_node_with_an_unrecorded_ruling():
    g = parse_graph(
        "| **W0.1** | H | — | node_converge + verifier |\n"
        "| **G0.1** | Sign-off | W0.1 | owner attest |\n"
    )
    caps = parse_catalog(HDR + "H\tW0.1\tgreen\tx\t\nS\tG0.1\tgreen\tattest=OWNER D-999\t\n")
    assert any("D-999" in p for p in eligibility_problems(g, caps, decisions_text=""))
    ok = "| OWNER D-999 | d | Attests G0.1: signed scope note | y | z |\n"
    assert eligibility_problems(g, caps, decisions_text=ok) == []


def test_attest_ruling_ignores_fenced_rows_and_accepts_marked_d_rows():
    from harness_lib import attest_problem

    cap = parse_catalog(HDR + "S\tG0.1\tgreen\tattest=OWNER D-7\t\n")[0]
    fenced = "```\n| OWNER D-7 | d | attests G0.1 | y | z |\n```\n"
    assert attest_problem(cap, fenced) is not None
    marked = "| D-7 | d | **OWNER D-7 (16 Nov):** Attests G0.1: signed | y | z |\n"
    assert attest_problem(cap, marked) is None
    negated = "| OWNER D-7 | d | does not sign off G0.1 | y | z |\n"
    assert attest_problem(cap, negated) is not None


def test_attest_ruling_must_open_with_the_marker():
    from harness_lib import attest_problem

    cap = parse_catalog(HDR + "S\tG0.1\tgreen\tattest=OWNER D-7\t\n")[0]
    for cell in (
        "the owner never attests G0.1",
        "it does not say 'Attests G0.1'",
        "Attests G0.12 only",
    ):
        assert attest_problem(cap, f"| OWNER D-7 | d | {cell} | y | z |\n") is not None, cell
    commented = "<!--\n| OWNER D-7 | d | Attests G0.1 | y | z |\n-->\n"
    assert attest_problem(cap, commented) is not None
    tilde = "~~~\n| OWNER D-7 | d | Attests G0.1 | y | z |\n~~~\n"
    assert attest_problem(cap, tilde) is not None
    other_number = "| D-7 | d | **OWNER D-71:** Attests G0.1 | y | z |\n"
    assert attest_problem(cap, other_number) is not None
