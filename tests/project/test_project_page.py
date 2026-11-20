"""T3.2 project page: links resolve, no placeholder is left, and the held-out headline matches
the committed gate summary with its stamps (PAG-01, PAG-02)."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "project" / "project-page.md"
GATES = ROOT / "docs" / "measurements" / "e2.5-heldout4-gates.json"
AREAS = 9


def text() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_every_relative_link_and_image_resolves():
    for target in re.findall(r"\]\(([^)\s]+)\)", text()):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path = (PAGE.parent / target.split("#")[0]).resolve()
        assert path.exists(), target


def test_no_placeholder_is_left():
    assert not re.search(r"PENDING|TODO|DEMO_URL|TBD", text())


def test_the_held_out_headline_matches_the_gate_summary_and_carries_its_stamps():
    gates = {g["name"]: g for g in json.loads(GATES.read_text(encoding="utf-8"))["gates"]}
    t = text()
    g = gates["grounded"]
    assert f"{g['value']:.3f} ({round(g['value'] * g['n'])}/{g['n']})" in t
    assert f"lower bound {g['lower']:.2f}" in t
    for name in ("integrity", "injection", "leak", "identity"):
        assert f"{gates[name]['n']}/{gates[name]['n']}" in t, name
    assert "single judge · certified (model panel) · course-ta-heldout-20261119T181359Z" in t
    assert "OWNER D-90" in t


def test_the_page_follows_the_nine_architecture_areas_and_links_the_diagrams_and_gif():
    t = text()
    numbered = re.findall(r"^## (\d+)\. ", t, re.M)
    assert [int(n) for n in numbered] == list(range(1, AREAS + 1))
    for asset in ("architecture.svg", "dataflow.svg", "walkthrough.gif"):
        assert (ROOT / "docs" / "assets" / asset).is_file() and asset in t, asset
    assert (ROOT / "docs" / "walkthrough.md").is_file()


def test_the_case_study_pointer_carries_no_numbers():
    pointer = (ROOT / "docs" / "case-study.md").read_text(encoding="utf-8")
    assert "project/project-page.md" in pointer
    assert not re.search(r"\b0\.\d{3}\b", pointer)
