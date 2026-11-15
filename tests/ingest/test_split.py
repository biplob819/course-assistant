"""ING-01: one unit per slide or page, title first, notes only when allowed, empty units
dropped and reported, and a file whose hash differs from the manifest refused."""

from __future__ import annotations

import io

import pytest
import yaml

from build_sample_course import pdf, pptx
from course_assistant.ingest import IngestError, units_from_manifest
from harness_lib import ROOT
from tests.ingest.conftest import write_course

SRC = ROOT / "sample_course" / "src"


def source(session: str) -> dict:
    return yaml.safe_load((SRC / f"{session}.yaml").read_text(encoding="utf-8"))


def by_key(split) -> dict:
    return {u.key: u for u in split.units}


def test_each_slide_is_one_unit_with_its_title_first(sample_split):
    units = by_key(sample_split)
    for sid in ("s01", "s02", "s03"):
        slides = source(sid)["slides"]
        for n, s in enumerate(slides, start=1):
            key = f"{sid}:slides:{n}"
            if key in {d.key for d in sample_split.dropped}:
                continue
            u = units[key]
            assert u.text.startswith(s["title"] + "\n"), key
            assert s["body"].strip().splitlines()[0] in u.text
            assert u.locator == f"slide {n}" and u.number == n and u.kind == "slides"
            assert u.label == f"Session {int(sid[1:])} slides, slide {n}"
            assert u.manifest_id == f"m-{sid}-slides"
        assert sum(u.key.startswith(f"{sid}:slides:") for u in sample_split.units) + sum(
            d.key.startswith(f"{sid}:slides:") for d in sample_split.dropped
        ) == len(slides)


def test_speaker_notes_only_when_the_entry_allows(sample_split, tmp_path):
    assert not any("Instructor note" in u.text for u in sample_split.units)
    deck = (ROOT / "sample_course" / "build" / "s01-slides.pptx").read_bytes()
    allowed = write_course(tmp_path, [("deck.pptx", "s01", "slides", deck)], notes=True)
    units = by_key(units_from_manifest(allowed, tmp_path))
    assert "Instructor note: ask two learners" in units["s01:slides:3"].text
    assert units["s01:slides:3"].text.startswith("Start with the problem, not the model\n")
    assert "Instructor note" not in units["s01:slides:4"].text  # slide 4 has no notes


def test_each_pdf_page_is_one_unit(sample_split):
    units = by_key(sample_split)
    for sid in ("s01", "s02", "s03"):
        pages = source(sid)["pre_read"]["pages"]
        got = [u for u in sample_split.units if u.key.startswith(f"{sid}:pre_read:")]
        assert len(got) == len(pages)
        for n, page in enumerate(pages, start=1):
            u = units[f"{sid}:pre_read:{n}"]
            assert u.text.startswith(page["heading"])
            assert u.locator == f"p. {n}"
            assert u.label == f"Session {int(sid[1:])} pre-read, p. {n}"


def test_agenda_is_one_unit(sample_split):
    for sid in ("s01", "s02", "s03"):
        got = [u for u in sample_split.units if u.kind == "agenda" and u.session == sid]
        assert [u.key for u in got] == [f"{sid}:agenda:1"]
        assert got[0].locator == "agenda" and "agenda" in got[0].text


def test_near_empty_canvas_slide_is_dropped_and_reported(sample_split):
    assert [(d.key, d.manifest_id, d.words) for d in sample_split.dropped] == [
        ("s01:slides:20", "m-s01-slides", 1)
    ]
    keys = {u.key for u in sample_split.units}
    assert "s01:slides:20" not in keys
    assert "s01:slides:21" in keys  # later slides keep their deck number (D-30)


def test_a_slide_under_three_words_after_its_title_is_dropped(tmp_path):
    session = {"title": "T", "slides": [
        {"title": "A long title with many words in it", "body": "Two words."},
        {"title": "Short", "body": "Exactly three words."},
    ]}  # fmt: skip
    m = write_course(tmp_path, [("d.pptx", "s01", "slides", pptx(session))])
    split = units_from_manifest(m, tmp_path)
    assert [d.key for d in split.dropped] == ["s01:slides:1"]
    assert [u.key for u in split.units] == ["s01:slides:2"]


def _table_deck() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title only
    title = slide.shapes.title
    assert title is not None
    title.text = "Metrics table"
    shapes = slide.shapes
    table = shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1)).table
    for (r, c), text in {(0, 0): "metric", (0, 1): "target",
                         (1, 0): "groundedness", (1, 1): "0.9"}.items():  # fmt: skip
        table.cell(r, c).text = text
    box = shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    box.text_frame.text = "Body text placed after the table in shape order."
    group = shapes.add_group_shape()
    inner = group.shapes.add_textbox(Inches(1), Inches(3), Inches(2), Inches(1))
    inner.text_frame.text = "Grouped note inside a group"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_tables_follow_body_text_and_groups_are_read(tmp_path):
    m = write_course(tmp_path, [("t.pptx", "s02", "slides", _table_deck())])
    (u,) = units_from_manifest(m, tmp_path).units
    lines = u.text.splitlines()
    assert lines[0] == "Metrics table"
    body = lines.index("Body text placed after the table in shape order.")
    group = lines.index("Grouped note inside a group")
    table = lines.index("metric | target")
    assert body < group < table and lines[table + 1] == "groundedness | 0.9"


def test_a_file_whose_hash_differs_from_the_manifest_is_refused(tmp_path):
    data = pdf([("Heading", "Some page text with enough words.")], "x")
    m = write_course(tmp_path, [("p.pdf", "s01", "pre_read", data)])
    (tmp_path / "p.pdf").write_bytes(data.replace(b"/Title (x)", b"/Title (y)"))
    with pytest.raises(IngestError) as exc:
        units_from_manifest(m, tmp_path)
    assert exc.value.kind == "config"
    assert "m-p-pdf" in str(exc.value) and "hash" in str(exc.value)


def test_a_path_that_leaves_the_corpus_root_is_refused(tmp_path):
    outside = tmp_path / "outside.pdf"
    data = pdf([("Heading", "Some page text with enough words.")], "x")
    outside.write_bytes(data)
    root = tmp_path / "root"
    root.mkdir()
    m = write_course(root, [("p.pdf", "s01", "pre_read", data)])
    (root / "p.pdf").unlink()
    (root / "p.pdf").symlink_to(outside)
    with pytest.raises(IngestError) as exc:
        units_from_manifest(m, root)
    assert exc.value.kind == "config" and "corpus root" in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def test_missing_unsupported_and_unreadable_files_are_config_errors(tmp_path):
    m = write_course(tmp_path, [("notes.txt", "s01", "faq", b"plain text faq entry here")])
    with pytest.raises(IngestError, match="unsupported file type"):
        units_from_manifest(m, tmp_path)
    m = write_course(tmp_path, [("bad.pdf", "s01", "pre_read", b"%PDF-1.4 not really")])
    with pytest.raises(IngestError) as exc:
        units_from_manifest(m, tmp_path)
    assert exc.value.kind == "config" and "m-bad-pdf" in str(exc.value)
    assert exc.value.__context__ is None and exc.value.__cause__ is None
    (tmp_path / "bad.pdf").unlink()
    with pytest.raises(IngestError, match="cannot read"):
        units_from_manifest(m, tmp_path)


def test_two_entries_of_one_kind_number_on_in_manifest_order(tmp_path):
    a = pdf([("A one", "First file page one text."), ("A two", "First file page two text.")],
            "a")  # fmt: skip
    b = pdf([("B one", "Second file page one text.")], "b")
    m = write_course(tmp_path, [("a.pdf", "s01", "pre_read", a), ("b.pdf", "s01", "pre_read", b)])
    units = by_key(units_from_manifest(m, tmp_path))
    assert units["s01:pre_read:3"].text.startswith("B one")
    assert units["s01:pre_read:3"].manifest_id == "m-b-pdf"
