"""Shared helpers for the course-assistant build harness (graph, catalog, specs).

Ported from the EvalKit harness (Tech Spec §11, D-18). Adds external nodes (D-17).

The build graph lives in docs/graph/graph.md as a markdown table. The capability
ledger lives in docs/graph/catalog.tsv. These helpers parse both and derive node status.
Nothing here touches the network.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH_MD = ROOT / "docs" / "graph" / "graph.md"
CATALOG_TSV = ROOT / "docs" / "graph" / "catalog.tsv"
SPECS_DIR = ROOT / "specs"

STATUSES = ("not-started", "spec", "building", "green")
NODE_RE = re.compile(r"^[WGXNABEDTRP]\d+\.\d+$")
NODE_LIKE_RE = re.compile(r"^[A-Za-z]\d+\.\d+$")  # looks like a node id but may be malformed
CATALOG_FIELDS = ("cap_id", "node", "status", "proof", "note")
PROOF_DIMENSIONS = ("logical", "contract", "golden", "repro", "safety")
ATTEST_RE = re.compile(
    r"^attest=OWNER D-([0-9]+)$", re.ASCII
)  # the only proof an external node takes
EXTERNAL_MARK = "owner attest"  # the exact Verified-by cell of an external node


@dataclass
class Node:
    id: str
    title: str
    depends_on: list[str] = field(default_factory=list)
    verified_by: str = ""
    deliverable: str = ""

    @property
    def external(self) -> bool:
        """External nodes (sign-off, pilot work outside the repo) are proven by owner attest.

        Only the exact cell "owner attest" marks one, so prose mentioning attest cannot.
        """
        return self.verified_by.strip().lower() == EXTERNAL_MARK


@dataclass
class Cap:
    cap_id: str
    node: str
    status: str
    proof: str
    note: str


class HarnessError(Exception):
    """Raised when a harness input file is malformed."""


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_graph(text: str) -> dict[str, Node]:
    """Parse every markdown table row whose first cell is a node id."""
    nodes: dict[str, Node] = {}
    in_fence = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not raw.lstrip().startswith("|"):
            continue
        cells = _cells(raw)
        node_id = cells[0].strip("*` ")
        if not NODE_RE.match(node_id):
            if NODE_LIKE_RE.match(node_id):
                raise HarnessError(f"graph line {lineno}: malformed node id {node_id!r}")
            continue
        if len(cells) < 3:
            raise HarnessError(f"graph line {lineno}: node {node_id} row needs a depends_on cell")
        if node_id in nodes:
            raise HarnessError(f"duplicate node {node_id} in graph")
        deps_cell = cells[2]
        deps: list[str] = []
        if deps_cell not in ("", "—", "-"):
            for part in deps_cell.split(","):
                dep = part.strip().strip("*` ")
                if not NODE_RE.match(dep):
                    raise HarnessError(f"{node_id}: unparseable depends_on entry {part.strip()!r}")
                deps.append(dep)
        nodes[node_id] = Node(
            id=node_id,
            title=cells[1],
            depends_on=deps,
            verified_by=cells[3] if len(cells) > 3 else "",
            deliverable=cells[4] if len(cells) > 4 else "",
        )
    if not nodes:
        raise HarnessError("graph has no node rows")
    return nodes


def load_graph(path: Path = GRAPH_MD) -> dict[str, Node]:
    return parse_graph(path.read_text(encoding="utf-8"))


def parse_catalog(text: str) -> list[Cap]:
    rows = list(csv.reader(text.splitlines(), delimiter="\t"))
    if not rows or tuple(c.strip() for c in rows[0]) != CATALOG_FIELDS:
        raise HarnessError("catalog header must be exactly: " + " | ".join(CATALOG_FIELDS))
    caps: list[Cap] = []
    seen: set[str] = set()
    for i, row in enumerate(rows[1:], start=2):
        if not row or all(not c.strip() for c in row):
            continue
        if len(row) != len(CATALOG_FIELDS):
            raise HarnessError(f"catalog line {i}: expected 5 columns, got {len(row)}")
        cap_id, node, status, proof, note = (c.strip() for c in row)
        if not cap_id or not node:
            raise HarnessError(f"catalog line {i}: cap_id and node are required")
        if status not in STATUSES:
            raise HarnessError(f"catalog line {i}: unknown status {status!r}")
        if cap_id in seen:
            raise HarnessError(f"catalog line {i}: duplicate cap {cap_id}")
        seen.add(cap_id)
        caps.append(Cap(cap_id=cap_id, node=node, status=status, proof=proof, note=note))
    return caps


def load_catalog(path: Path = CATALOG_TSV) -> list[Cap]:
    return parse_catalog(path.read_text(encoding="utf-8"))


def write_catalog(caps: list[Cap], path: Path = CATALOG_TSV) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(CATALOG_FIELDS)
        for c in caps:
            writer.writerow([c.cap_id, c.node, c.status, c.proof, c.note])


def node_status(node_id: str, caps: list[Cap]) -> str:
    """green if every row is green; building if any row is building or green.

    Otherwise spec if any row is spec, else not-started.
    """
    rows = [c for c in caps if c.node == node_id]
    if not rows:
        return "not-started"
    statuses = {c.status for c in rows}
    if statuses == {"green"}:
        return "green"
    if statuses & {"building", "green"}:
        return "building"
    if "spec" in statuses:
        return "spec"
    return "not-started"


def find_cycle(nodes: dict[str, Node]) -> list[str] | None:
    """Return one dependency cycle as a list of node ids, or None."""
    white, grey, black = 0, 1, 2
    color = dict.fromkeys(nodes, white)
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = grey
        stack.append(n)
        for d in nodes[n].depends_on:
            if d not in nodes:
                continue
            if color[d] == grey:
                return [*stack[stack.index(d) :], d]
            if color[d] == white:
                found = visit(d)
                if found:
                    return found
        stack.pop()
        color[n] = black
        return None

    for n in nodes:
        if color[n] == white:
            found = visit(n)
            if found:
                return found
    return None


def eligibility_problems(
    nodes: dict[str, Node],
    caps: list[Cap],
    simulate_start: str | None = None,
    decisions_text: str | None = None,
) -> list[str]:
    """All reasons the graph is not eligible. Empty list means eligible."""
    if decisions_text is None:
        decisions_text = DECISIONS_MD.read_text(encoding="utf-8") if DECISIONS_MD.is_file() else ""
    problems: list[str] = []
    for n in nodes.values():
        for d in n.depends_on:
            if d not in nodes:
                problems.append(f"{n.id} depends on unknown node {d}")
    cycle = find_cycle(nodes)
    if cycle:
        problems.append("dependency cycle: " + " -> ".join(cycle))
    for c in caps:
        if c.node not in nodes:
            problems.append(f"catalog row {c.cap_id} names unknown node {c.node}")
    for n in nodes.values():
        if not n.external:
            continue
        for c in caps:
            if c.node == n.id and c.status == "green":
                why = attest_problem(c, decisions_text)
                if why:
                    problems.append(f"external node {n.id} is green without a valid attest: {why}")
    for n in nodes.values():
        status = node_status(n.id, caps)
        active = status in ("building", "green") or n.id == simulate_start
        if not active:
            continue
        for d in n.depends_on:
            if d in nodes and node_status(d, caps) != "green":
                problems.append(
                    f"{n.id} is {'starting' if n.id == simulate_start else status} "
                    f"but dependency {d} is {node_status(d, caps)}"
                )
    return problems


DECISIONS_MD = ROOT / "docs" / "decisions.md"


def _ruling_rows(decisions_text: str, n: str) -> list[str]:
    """Decision cells (third column) of live table rows recording owner ruling D-n: either
    `| OWNER D-n |` or `| D-n |` whose decision is marked `**OWNER D-n` (exact number).
    Code fences (``` or ~~~) and HTML comments are not live text."""
    text = re.sub(r"<!--.*?(?:-->|\Z)", "", decisions_text, flags=re.S)  # unclosed: to the end
    marked = re.compile(rf"\*\*OWNER D-{n}(?!\d)")
    cells: list[str] = []
    fence: str | None = None  # a fence closes only with the same marker it opened with
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3] if stripped.startswith(("```", "~~~")) else None
        if marker:
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None or not line.startswith("|"):
            continue
        parts = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(parts) < 3:
            continue
        if parts[0] == f"OWNER D-{n}" or (parts[0] == f"D-{n}" and marked.search(parts[2])):
            cells.append(parts[2])
    return cells


def attest_problem(cap: Cap, decisions_text: str) -> str | None:
    """None when the row's proof is attest=OWNER D-n and that ruling is recorded in
    decisions.md and says, in so many words, that it `attests <node>` (D-22): naming the node
    elsewhere ("does not sign off G0.1") proves nothing."""
    m = ATTEST_RE.match(cap.proof.strip())
    if not m:
        return f"{cap.cap_id}: an external node's proof must be attest=OWNER D-n"
    cells = _ruling_rows(decisions_text, m[1])
    if not cells:
        return f"{cap.cap_id}: OWNER D-{m[1]} is not recorded in docs/decisions.md"
    # the decision itself must open with "Attests <node>" (after an optional bold label),
    # so a sentence that merely mentions or negates it does not count
    opening = re.compile(
        rf"^(?:\*\*[^*]*\*\*\s*)?Attests {re.escape(cap.node)}(?![\w.])", re.IGNORECASE
    )
    if not any(opening.match(c) for c in cells):
        return f"{cap.cap_id}: OWNER D-{m[1]} does not open with 'Attests {cap.node}'"
    return None


def criteria_missing(text: str, cap_ids: list[str]) -> list[str]:
    """Capabilities with no criterion line. A criterion is a line starting `**CAP-ID**`,
    so a longer id (SCH-10 for SCH-1) or a passing mention does not count."""
    present = set(re.findall(r"^\*\*([A-Z]+-\d+)\*\*", text, re.M))
    return [c for c in cap_ids if c not in present]


def spec_dir(node_id: str, specs: Path = SPECS_DIR) -> Path | None:
    matches = sorted(specs.glob(f"{node_id}-*"))
    return matches[0] if matches else None
