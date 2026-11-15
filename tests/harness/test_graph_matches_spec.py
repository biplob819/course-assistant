"""The build graph and catalog match the Tech Spec tables (HAR-03, HAR-06).

Parsed independently of harness_lib, so a parser bug cannot hide a drift (L-10).
"""

import re

from harness_lib import ROOT, load_catalog, load_graph

SPEC = (ROOT / "AI Teaching Assistant Tech Spec.md").read_text(encoding="utf-8")
EXTERNAL = {"G0.1", "P4.1", "P4.2", "P4.3"}


def _spec_rows() -> dict[str, tuple[str, list[str], str, str]]:
    """node -> (title, deps, deliverable, stories) from the Tech Spec §11.2 table."""
    section = SPEC.split("### 11.2")[1].split("### 11.3")[0]
    rows = {}
    for line in section.splitlines():
        m = re.match(r"^\| \*\*([A-Z]\d\.\d)\*\* \|(.*)\|$", line)
        if m:
            title, deps, deliverable, stories = (c.strip() for c in m[2].split(" | "))
            dep_list = [] if deps == "—" else [d.strip() for d in deps.split(",")]
            rows[m[1]] = (title, dep_list, deliverable, stories)
    return rows


def _spec_nodes() -> dict[str, list[str]]:
    return {n: r[1] for n, r in _spec_rows().items()}


def _spec_cap_rows() -> dict[str, tuple[str, str]]:
    """cap -> (node, note) from the Tech Spec §11.4 tables; node from the table's heading."""
    section = SPEC.split("### 11.4")[1].split("### 11.5")[0]
    caps, node = {}, ""
    for line in section.splitlines():
        h = re.match(r"^\*\*([A-Z]\d\.\d)", line)
        if h:
            node = h[1]
        m = re.match(r"^\| ([A-Z]+-\d\d) \| (.*) \| (.*?) \|$", line)
        if m:
            note = m[2] if m[3].strip() in ("—", "") else f"{m[2]} [{m[3]}]"
            caps[m[1]] = (node, note)
    return caps


def _spec_caps() -> set[str]:
    return set(_spec_cap_rows())


def test_graph_nodes_and_dependencies_match_the_tech_spec():
    spec = _spec_nodes()
    graph = load_graph()
    assert len(spec) == 31
    assert set(graph) == set(spec)
    for node_id, deps in spec.items():
        assert graph[node_id].depends_on == deps, node_id


def test_catalog_rows_match_the_tech_spec():
    assert {c.cap_id for c in load_catalog()} == _spec_caps()


def test_titles_deliverables_and_stories_match_the_tech_spec():
    graph = load_graph()
    for node_id, (title, _, deliverable, _stories) in _spec_rows().items():
        assert graph[node_id].title == title, node_id
        assert graph[node_id].deliverable == deliverable, node_id


def test_every_capability_note_and_node_match_the_tech_spec():
    spec = _spec_cap_rows()
    pilot = {"PIL": "P4.1", "OPS": "P4.2", "END": "P4.3"}  # one table heading covers P4.1-P4.3
    for c in load_catalog():
        node, note = spec[c.cap_id]
        assert c.note == note, c.cap_id
        assert c.node == pilot.get(c.cap_id.split("-")[0], node), c.cap_id


def test_external_nodes_are_exactly_sign_off_and_pilot():
    assert {n.id for n in load_graph().values() if n.external} == EXTERNAL


def test_dependency_levels_match_the_tech_spec_table():
    graph = load_graph()
    level: dict[str, int] = {}

    def lvl(n: str) -> int:
        if n not in level:
            level[n] = 1 + max((lvl(d) for d in graph[n].depends_on), default=-1)
        return level[n]

    section = SPEC.split("**Dependency levels.**")[1].split("**Critical path")[0]
    for m in re.finditer(r"^\| (\d+) \| (.+) \|$", section, re.M):
        for node in re.findall(r"[A-Z]\d\.\d", m[2]):
            assert lvl(node) == int(m[1]), node
