#!/usr/bin/env python3
"""Build the sample course binaries from their YAML source, byte for byte the same every time.

    python scripts/build_sample_course.py            # write sample_course/build/ and manifest
    python scripts/build_sample_course.py --check    # exit 1 if build/ or manifest is stale

Source: sample_course/src/sNN.yaml (slides, pre-read pages, agenda). Output per session:
sNN-slides.pptx (one slide per source slide, speaker notes where given), sNN-pre-read.pdf
(one page per source page), sNN-agenda.pdf (one page). The manifest's sha256 values are
computed from these files (CRS-03). PDFs are written by a small built-in writer (standard
Helvetica, WinAnsi text), so building needs no PDF library and the bytes never depend on a
library version.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import sys
import textwrap
import zipfile
import zlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "sample_course" / "src"
BUILD = ROOT / "sample_course" / "build"
MANIFEST = ROOT / "sample_course" / "manifest.yaml"
FIXED_TIME = dt.datetime(2026, 11, 15, 0, 0, 0)
ZIP_TIME = (2026, 11, 15, 0, 0, 0)
APPROVER = "owner-1"
APPROVED_AT = "2026-11-15"
LICENCE = "CC-BY-4.0"


# ---------- PDF ----------


def _pdf_escape(text: str) -> bytes:
    data = text.encode("cp1252")  # WinAnsiEncoding; the source is checked to be ASCII-safe
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _page_stream(heading: str, body: str) -> bytes:
    lines: list[tuple[int, str]] = [(16, heading), (11, "")]
    for para in body.strip().splitlines():
        for wrapped in textwrap.wrap(para, 88) or [""]:
            lines.append((11, wrapped))
    out = [b"BT", b"/F1 16 Tf", b"56 780 Td"]
    first = True
    for size, line in lines:
        if not first:
            out.append(f"0 -{int(size * 1.5)} Td".encode())
        out.append(f"/F1 {size} Tf".encode())
        out.append(b"(" + _pdf_escape(line) + b") Tj")
        first = False
    out.append(b"ET")
    return b"\n".join(out)


def pdf(pages: list[tuple[str, str]], title: str) -> bytes:
    """A minimal PDF: catalog, page tree, one Helvetica font, one Flate stream per page."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in below
    tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    kids = []
    for heading, body in pages:
        stream = zlib.compress(_page_stream(heading, body), 9)
        content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream
                      + b"\nendstream")  # fmt: skip
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] "
                        b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                        % (tree, font, content)))  # fmt: skip
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % tree
    objects[tree - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                         + b"] /Count %d >>" % len(kids))  # fmt: skip
    info = add(b"<< /Title (" + _pdf_escape(title) + b") /Producer (course-assistant sample build)"
               b" /CreationDate (D:20261115000000Z) >>")  # fmt: skip
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = buf.tell()
    buf.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        buf.write(b"%010d 00000 n \n" % off)
    buf.write(b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objects) + 1, catalog, info, xref))  # fmt: skip
    return buf.getvalue()


# ---------- PPTX ----------


def pptx(session: dict) -> bytes:
    from pptx import Presentation
    from pptx.util import Pt

    prs = Presentation()
    layout = prs.slide_layouts[1]  # title and content
    for s in session["slides"]:
        slide = prs.slides.add_slide(layout)
        title = slide.shapes.title
        assert title is not None, "layout 1 has a title placeholder"
        title.text = s["title"]
        body = slide.placeholders[1].text_frame  # type: ignore[attr-defined]
        lines = [ln for ln in s["body"].strip().splitlines() if ln.strip()]
        body.text = lines[0] if lines else ""
        for ln in lines[1:]:
            p = body.add_paragraph()
            p.text = ln
        for p in body.paragraphs:
            for r in p.runs:
                r.font.size = Pt(20)
        if s.get("notes"):
            notes = slide.notes_slide.notes_text_frame
            assert notes is not None
            notes.text = s["notes"].strip()
    cp = prs.core_properties
    cp.title = session["title"]
    cp.author = "course-assistant sample course"
    cp.last_modified_by = "course-assistant sample build"
    cp.created = cp.modified = FIXED_TIME
    cp.revision = 1
    raw = io.BytesIO()
    prs.save(raw)
    return _restamp_zip(raw.getvalue())


def _restamp_zip(data: bytes) -> bytes:
    """Rewrite a zip with fixed timestamps and attributes so the bytes are reproducible."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            fixed = zipfile.ZipInfo(info.filename, date_time=ZIP_TIME)
            fixed.compress_type = zipfile.ZIP_DEFLATED
            fixed.external_attr = 0o644 << 16
            dst.writestr(fixed, src.read(info.filename), compresslevel=9)
    return out.getvalue()


# ---------- build ----------


def load_sessions() -> list[dict]:
    sessions = []
    for f in sorted(SRC.glob("s[0-9][0-9].yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        text = yaml.safe_dump(data)
        try:
            text.encode("cp1252")
        except UnicodeEncodeError as exc:
            raise SystemExit(
                f"{f.name}: text must be WinAnsi-encodable for the PDF writer ({exc})"
            ) from None
        sessions.append(data)
    if not sessions:
        raise SystemExit(f"no session sources in {SRC}")
    return sessions


def build() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for s in load_sessions():
        sid = s["session"]
        files[f"{sid}-slides.pptx"] = pptx(s)
        pages = [(p["heading"], p["text"]) for p in s["pre_read"]["pages"]]
        files[f"{sid}-pre-read.pdf"] = pdf(pages, s["pre_read"]["title"])
        files[f"{sid}-agenda.pdf"] = pdf([(s["title"] + " - agenda", s["agenda"])], s["title"])
    return files


def manifest_for(files: dict[str, bytes]) -> str:
    kinds = {"slides.pptx": "slides", "pre-read.pdf": "pre_read", "agenda.pdf": "agenda"}
    entries = []
    for name, data in files.items():
        sid, rest = name.split("-", 1)
        kind = kinds[rest]
        entries.append({
            "manifest_id": f"m-{sid}-{kind.replace('_', '-')}",
            "path": f"sample_course/build/{name}",
            "session": sid,
            "kind": kind,
            "sha256": hashlib.sha256(data).hexdigest(),
            "licence": LICENCE,
            "approved_by": APPROVER,
            "approved_at": APPROVED_AT,
            "include_speaker_notes": False,
        })  # fmt: skip
    doc = {"schema_version": "1.0", "cohort": "sample", "entries": entries}
    header = "# Generated by scripts/build_sample_course.py; do not edit by hand.\n"
    return header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    files = build()
    manifest = manifest_for(files)
    if args.check:
        stale = [
            n
            for n, d in files.items()
            if not (BUILD / n).is_file() or (BUILD / n).read_bytes() != d
        ]
        if not MANIFEST.is_file() or MANIFEST.read_text(encoding="utf-8") != manifest:
            stale.append("manifest.yaml")
        if stale:
            print("sample course is stale: " + ", ".join(stale))
            return 1
        print(f"sample course is up to date ({len(files)} files)")
        return 0
    BUILD.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (BUILD / name).write_bytes(data)
    MANIFEST.write_text(manifest, encoding="utf-8")
    print(f"built {len(files)} files and the manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
