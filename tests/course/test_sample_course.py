"""N1.3: the public sample course, its manifest, assessments and absent topics."""

import re

import pytest
import yaml

import build_sample_course as bsc
from course_assistant.schemas import load_assessments, load_manifest
from harness_lib import ROOT

COURSE = ROOT / "sample_course"


def _all_text() -> str:
    parts = []
    for s in bsc.load_sessions():
        parts.append(s["title"] + "\n" + s["agenda"])
        for sl in s["slides"]:
            parts.append(sl["title"] + "\n" + sl["body"] + "\n" + sl.get("notes", ""))
        for p in s["pre_read"]["pages"]:
            parts.append(p["heading"] + "\n" + p["text"])
    return "\n".join(parts).lower()


# ---------- CRS-01 ----------


def test_three_sessions_build_slides_pre_reads_and_agendas():
    files = bsc.build()
    assert sorted(files) == sorted(
        f"s0{i}-{k}" for i in (1, 2, 3) for k in ("slides.pptx", "pre-read.pdf", "agenda.pdf")
    )


def test_slides_and_pages_match_their_source():
    from pptx import Presentation
    from pypdf import PdfReader

    for s in bsc.load_sessions():
        sid = s["session"]
        prs = Presentation(str(COURSE / "build" / f"{sid}-slides.pptx"))
        titles = [sl.shapes.title.text if sl.shapes.title else "" for sl in prs.slides]
        assert titles == [x["title"] for x in s["slides"]]
        for slide, src in zip(prs.slides, s["slides"], strict=True):
            if src.get("notes"):
                notes = slide.notes_slide.notes_text_frame
                assert notes is not None and notes.text == src["notes"].strip()
        pdf = PdfReader(COURSE / "build" / f"{sid}-pre-read.pdf")
        pages = s["pre_read"]["pages"]
        assert len(pdf.pages) == len(pages)
        for page, src in zip(pdf.pages, pages, strict=True):
            text = " ".join(page.extract_text().split())
            assert src["heading"] in text
            assert " ".join(src["text"].split()[:6]) in text


def test_prd_examples_sit_where_the_prd_cites_them():
    s2, s3 = bsc.load_sessions()[1], bsc.load_sessions()[2]
    assert "offline" in s2["slides"][8]["title"].lower()  # Session 2 slides, slide 9
    assert "offline" in s2["pre_read"]["pages"][2]["heading"].lower()  # Session 2 pre-read, p. 3
    assert all("Layer" in s3["slides"][i]["title"] for i in range(3, 7))  # slides 4 to 7


@pytest.mark.contract
def test_build_is_byte_identical_and_committed_files_are_current():
    assert bsc.build() == bsc.build()
    assert bsc.main(["--check"]) == 0


# ---------- CRS-02 ----------


def test_sample_material_is_cc_by_4():
    text = (COURSE / "LICENSE").read_text(encoding="utf-8")
    assert "Creative Commons Attribution 4.0" in text
    assert all(e.licence == "CC-BY-4.0" for e in load_manifest(COURSE / "manifest.yaml").entries)


# ---------- CRS-03 ----------


def test_manifest_loads_and_hashes_match_the_built_files():
    import hashlib

    m = load_manifest(COURSE / "manifest.yaml")
    assert len(m.entries) == 9
    for e in m.entries:
        assert hashlib.sha256((ROOT / e.path).read_bytes()).hexdigest() == e.sha256
        assert e.approved_by == "owner-1" and not e.include_speaker_notes


@pytest.mark.safety
def test_a_changed_source_without_a_rebuild_is_detected(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    for f in (COURSE / "src").glob("*.yaml"):
        (src / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    edited = src / "s01.yaml"
    edited.write_text(edited.read_text(encoding="utf-8").replace("Session 1", "Session One", 1),
                      encoding="utf-8")  # fmt: skip
    monkeypatch.setattr(bsc, "SRC", src)
    assert bsc.main(["--check"]) == 1


# ---------- CRS-04 ----------


def test_assessments_are_briefs_only_three_per_session():
    a = load_assessments(COURSE / "assessments.yaml")
    for sid in ("s01", "s02", "s03"):
        assert sum(sid in x.sessions for x in a.assessments) >= 3


@pytest.mark.safety
def test_assessments_are_never_indexed_and_solutions_stay_private():
    m = load_manifest(COURSE / "manifest.yaml")
    assert not any("assessment" in e.path or "solution" in e.path for e in m.entries)
    r = __import__("subprocess").run(
        ["git", "check-ignore", "-q", "golden/heldout/solutions/s01-a1.md"], cwd=ROOT, check=False
    )
    assert r.returncode == 0


# ---------- CRS-05 ----------


def test_absent_topics_are_absent_from_every_unit():
    topics = yaml.safe_load((COURSE / "absent_topics.yaml").read_text(encoding="utf-8"))["topics"]
    assert len(topics) >= 12
    text = _all_text()
    for t in topics:
        for k in t["keywords"]:
            assert not re.search(r"(?<![a-z])" + re.escape(k.lower()), text), k
