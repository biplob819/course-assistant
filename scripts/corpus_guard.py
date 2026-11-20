#!/usr/bin/env python3
"""Fail if real course material, held-out eval data or private terms would enter the repo.

The repo is public and must never hold course material or learner data (NFR1, HAR-08).
Checks tracked and untracked (not ignored) files:

  1. Course-type files (.pdf, .pptx, .ppt, .docx, .doc, .key, .odp) only under sample_course/.
  2. Nothing under corpus/ (the private pilot corpus).
  3. Nothing under golden/heldout/ except golden/heldout/MANIFEST.json (D-48 pattern).
  4. No text file contains a term listed in the git-ignored .private-terms file
     (one term per line, case-insensitive; lines starting with # are comments).
     The list itself stays local, so the terms are never published by the guard.

Usage:
    python scripts/corpus_guard.py
    python scripts/corpus_guard.py --self-test
"""

from __future__ import annotations

import argparse
import bz2
import email.header
import gzip
import html
import io
import lzma
import os
import quopri
import re
import sys
import tempfile
import unicodedata
import urllib.parse
import zipfile
import zlib
from pathlib import Path

from harness_lib import ROOT
from secret_scan import candidate_files

COURSE_SUFFIXES = {
    ".pdf", ".pptx", ".ppt", ".docx", ".doc", ".key", ".odp", ".odt", ".rtf", ".epub",
    ".xlsx", ".xls", ".pages", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz",
    ".vtt", ".srt", ".mp4", ".mov", ".m4a", ".mp3", ".wav", ".webm",
}  # fmt: skip
# file contents that mark a document or archive whatever its name says
COURSE_MAGIC = (
    b"%PDF", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"7z\xbc\xaf", b"Rar!", b"\x1f\x8b",
    b"BZh", b"\xfd7zXZ\x00", b"WEBVTT",
)  # fmt: skip
SAMPLE_DIR = "sample_course"  # matched case-sensitively: SAMPLE_COURSE/ is not the sample
HELDOUT_DIR = ("golden", "heldout")
HELDOUT_ALLOWED = "golden/heldout/MANIFEST.json"
PRIVATE_TERMS_FILE = ".private-terms"
WITHHELD = "<path withheld: it contains a private term>"
MIN_COLLAPSED = 8  # terms this long also match inside one word with separators removed
# invisible characters that are not category Cf (combining grapheme joiner, Hangul fillers,
# braille blank); category Mn (combining marks, variation selectors) is dropped too
INVISIBLE = {"\u034f", "\u115f", "\u1160", "\u3164", "\uffa0", "\u2800"}
MARKUP = re.compile(r"<[^<>]{0,200}>")  # HTML or XML tags
MD_LINK = re.compile(r"!?\[([^\]]{0,200})\]\([^)]{0,500}\)")  # [text](url) -> text


# letters that do not decompose into base + accent, and German umlauts written out
TRANSLIT = str.maketrans(
    {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "ł": "l",
     "Ł": "L", "ı": "i", "đ": "d", "Đ": "D", "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D"}
)  # fmt: skip
# German and Scandinavian letters written out ("juergen", "soeren", "aase")
UMLAUT_EXPAND = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ø": "oe", "Ø": "Oe",
     "å": "aa", "Å": "Aa"}
)  # fmt: skip
MD_REF_LINK = re.compile(r"\[([^\]]{0,200})\]\[[^\]]{0,100}\]")  # [text][ref] -> text
JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
CLAUSE_MARKS = ",.;:!?"  # a phrase never matches across these, spaced or not


def fold(text: str) -> str:
    """Undo the ordinary ways a term gets broken up: JSON \\u escapes, HTML entities,
    %-encoding, compatibility forms (fullwidth, NBSP), accents, case, invisible characters.
    Invisible characters are dropped, joining "Insti\u200btute" (never turned into spaces:
    that split "Yin\u00adtang" into a false match, W0.1 pass 10)."""
    text = JSON_ESCAPE.sub(lambda m: chr(int(m[1], 16)), text)
    text = re.sub(r"\\[nrt]", " ", text)  # JSON and code string escapes: "Halvorsen\nPolytechnic"
    for _ in range(3):  # %-encoding and entities, nested either way, until stable
        decoded = urllib.parse.unquote(html.unescape(text))
        if decoded == text:
            break
        text = decoded
    text = text.translate(TRANSLIT)  # after decoding, so S&oslash;ren and S%C3%B8ren map too
    text = unicodedata.normalize("NFKD", text)  # split accents off so "zoë" folds to "zoe"
    text = "".join(
        ch for ch in text if unicodedata.category(ch) not in ("Cf", "Mn") and ch not in INVISIBLE
    )
    return unicodedata.normalize("NFKC", text).casefold()


def mime_decoded(text: str) -> str:
    """Quoted-printable and MIME encoded-words (=?utf-8?Q?S=C3=B8ren_Kjeldby?=), as mail tools
    write them in saved file names and bodies."""
    text = re.sub(
        r"=\?([\w-]+)\?[QqBb]\?([^?]*)\?=",
        lambda m: _mime_word(m[0]),
        text,
    )
    raw = text.encode("utf-8", errors="replace")
    return quopri.decodestring(raw.replace(b"=\r\n", b"").replace(b"=\n", b"")).decode(
        "utf-8", errors="replace"
    )


def _mime_word(word: str) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(word)))
    except (ValueError, LookupError, UnicodeError):
        return word


def views(text: str) -> list[str]:
    """Three views, all checked: as written (terms in URLs and attributes), markup removed
    (a term split mid-word by tags), and markup replaced by a space (words in separate cells:
    <td>Bo</td><td>Yin</td>). Markdown links, inline or reference style, keep their text."""
    linked = MD_REF_LINK.sub(r"\1", MD_LINK.sub(r"\1", text))
    linked = re.sub(r"\.(?=<)", ". ", linked)  # a sentence ending right before a tag still ends
    joined = re.sub(r"[*~`]", "", MARKUP.sub("", linked))  # mid-word emphasis: Zan**zibar**
    # hyphenated line breaks, also behind comment or quote markers ("> Kjel-\n> dby")
    joined = re.sub(r"(\w)-\r?\n[\s#>]*(\w)", r"\1\2", joined)
    spaced = re.sub(r"[*~`]", "", MARKUP.sub(" ", linked))
    out = [fold(text), fold(joined), fold(spaced)]
    mime = mime_decoded(text)
    if mime != text:
        out.append(fold(mime))
    return out


def tokens(text: str) -> list[str]:
    """Letters-and-digits runs; everything else (spaces, newlines, #, >, -, _) separates.
    `text` is already folded."""
    return re.findall(r"[^\W_]+", text)


def words(text: str) -> list[str]:
    """Whitespace-separated words with their non-alphanumerics removed: a slug or camelCase
    name is one word, but two words of prose never run together ("theory and" stays apart)."""
    return ["".join(re.findall(r"[^\W_]+", w)) for w in text.split()]


class Term:
    def __init__(self, raw: str) -> None:
        # spelling variants: plain folding ("jurgen") and German umlaut expansion ("juergen")
        spellings = {fold(raw), fold(raw.translate(UMLAUT_EXPAND))}
        self.variants = []
        for s in spellings:
            toks = tokens(s)
            self.variants.append((" " + " ".join(toks) + " ", "".join(toks)))
            if toks and toks[-1].isalpha():  # the plural too: "...s" and "...es"
                for end in ("s", "es"):
                    plural = [*toks[:-1], toks[-1] + end]
                    self.variants.append((" " + " ".join(plural) + " ", "".join(plural)))
        # split on every clause mark the term does not hold itself: "A. Kumar" keeps its
        # full stop but still never matches across a comma ("plan A, Kumar agreed")
        # A full stop breaks only where it ends a sentence (space or line end after it): a dot
        # inside an identifier (juergen.mueller@..., a domain, a file name) is part of a slug.
        own = set(CLAUSE_MARKS) & set(fold(raw))
        marks = "".join(c for c in CLAUSE_MARKS if c not in own and c != ".")
        alts = ["[" + re.escape(marks) + "]"] if marks else []
        if "." not in own:
            # ends a sentence: before a space, line end, closing quote or bracket, or emphasis
            alts.append(r"\.(?=[\s\"'\u201d\u2019\u00bb)\]*_`]|$)")
        self.breaker = re.compile("|".join(alts), re.M) if alts else None

    def found_in(self, text: str) -> bool:
        for view in views(text):
            clauses = self.breaker.split(view) if self.breaker else [view]
            clause_seqs = [" " + " ".join(tokens(c)) + " " for c in clauses]
            view_words = [w for c in clauses for w in words(c)]  # slugs never cross a break
            for seq, collapsed in self.variants:
                if any(seq in c for c in clause_seqs):
                    return True
                if len(collapsed) >= MIN_COLLAPSED and any(collapsed in w for w in view_words):
                    return True
        return False


def read_terms_file(data: bytes) -> str:
    for bom, enc in (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ):
        if data.startswith(bom):
            return data.decode(enc, errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def load_private_terms(root: Path) -> list[Term]:
    f = root / PRIVATE_TERMS_FILE
    if not f.is_file():
        return []
    terms = []
    for line in read_terms_file(f.read_bytes()).splitlines():
        line = line.strip().lstrip("\ufeff").strip()
        if line and not line.startswith("#") and tokens(fold(line)):
            terms.append(Term(line))
    return terms


def _heldout(parts: tuple[str, ...]) -> bool:
    return any(parts[i : i + 2] == HELDOUT_DIR for i in range(len(parts) - 1))


MAX_MEMBER = 20_000_000
ARCHIVE_MAGIC = (b"PK\x03\x04", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00")
# binary files that are not text and are allowed anywhere (images, fonts); their bytes are
# Binaries are allowed only in these folders, and only when the suffix and the content
# magic agree (D-27). Everything else must be UTF-8 text without NUL bytes (D-26).
BINARY_DIRS = (("sample_course",), ("docs", "assets"))
ASSET_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
}


def _sfnt_header_ok(data: bytes) -> bool:
    """A font's table directory: 1-100 tables and searchRange = 16 * 2**floor(log2(n))."""
    if len(data) < 12:
        return False
    n = int.from_bytes(data[4:6], "big")
    search = int.from_bytes(data[6:8], "big")
    return 0 < n <= 100 and search == 16 * (1 << (n.bit_length() - 1))


def _riff(kind: bytes):
    return lambda d: d[:4] == b"RIFF" and d[8:12] == kind


def _starts(*magic: bytes):
    return lambda d: d.startswith(magic)


_ZIP = _starts(b"PK\x03\x04")
_FTYP = lambda d: d[4:8] == b"ftyp"  # noqa: E731
SUFFIX_MAGIC = {
    ".png": _starts(b"\x89PNG\r\n\x1a\n"),
    ".jpg": _starts(b"\xff\xd8\xff"),
    ".jpeg": _starts(b"\xff\xd8\xff"),
    ".gif": _starts(b"GIF87a", b"GIF89a"),
    ".webp": _riff(b"WEBP"),
    ".ico": lambda d: d[:4] == b"\x00\x00\x01\x00" and d[4:6] != b"\x00\x00",
    ".pdf": _starts(b"%PDF-"),
    ".pptx": _ZIP, ".docx": _ZIP, ".xlsx": _ZIP, ".odp": _ZIP, ".odt": _ZIP, ".zip": _ZIP,
    ".epub": _ZIP,
    ".gz": _starts(b"\x1f\x8b"), ".tgz": _starts(b"\x1f\x8b"),
    ".bz2": _starts(b"BZh"), ".xz": _starts(b"\xfd7zXZ\x00"),
    ".mp4": _FTYP, ".m4a": _FTYP,
    ".mov": lambda d: d[4:8] in (b"ftyp", b"wide", b"moov", b"mdat", b"free", b"skip"),
    ".tif": _starts(b"II*\x00", b"MM\x00*"), ".tiff": _starts(b"II*\x00", b"MM\x00*"),
    ".mp3": _starts(b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
    ".wav": _riff(b"WAVE"),
    ".woff": _starts(b"wOFF"), ".woff2": _starts(b"wOF2"),
    ".ttf": lambda d: d[:4] in (b"\x00\x01\x00\x00", b"true") and _sfnt_header_ok(d),
    ".otf": lambda d: d[:4] == b"OTTO" and _sfnt_header_ok(d),
}  # fmt: skip


def allowed_binary(parts: tuple[str, ...], suffix: str, data: bytes) -> bool:
    """A binary file is allowed only in an allowed folder, with a known suffix whose content
    magic it really has: a suffix alone, or magic alone, is not enough (D-27)."""
    check = SUFFIX_MAGIC.get(suffix.lower())
    if check is None or not check(data):
        return False
    if parts[:1] == ("sample_course",):
        return True
    # docs/assets holds page images and fonts only; course material lives in sample_course/
    return parts[:2] == ("docs", "assets") and suffix.lower() in ASSET_SUFFIXES


def archive_text(data: bytes, depth: int = 0) -> str:
    """Text inside a zip (Office and OpenDocument files are zips of XML) or gzip file, with
    member names, one level of nesting deep, so archives in sample_course/ are checked too."""
    parts: list[str] = []
    try:
        if data.startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    parts.append(info.filename)
                    if info.file_size < MAX_MEMBER:
                        member = z.read(info)
                        parts.append(member.decode("utf-8", errors="replace"))
                        if depth < 1 and member.startswith((b"PK\x03\x04", b"\x1f\x8b")):
                            parts.append(archive_text(member, depth + 1))
        elif data.startswith((b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00")):
            opener = {b"\x1f": gzip.decompress, b"B": bz2.decompress, b"\xfd": lzma.decompress}
            inner = opener[data[:1]](data)[:MAX_MEMBER]
            parts.append(inner.decode("utf-8", errors="replace"))
            if depth < 1 and inner.startswith(ARCHIVE_MAGIC):
                parts.append(archive_text(inner, depth + 1))
        elif data.startswith(b"%PDF"):
            parts.append(pdf_text(data))
            # info dictionaries and other objects outside streams: /Author (Søren ...)
            parts.append(data[:MAX_MEMBER].decode("latin-1"))
    except (
        zipfile.BadZipFile, OSError, RuntimeError, EOFError, ValueError, lzma.LZMAError, zlib.error
    ):  # fmt: skip
        return "\n".join(parts)
    return "\n".join(parts)


def pdf_text(data: bytes) -> str:
    """Decompressed content streams of a PDF (Flate), so text in sample_course PDFs is
    checked. Plain strings only; kerned TJ fragments are outside D-23's scope."""
    parts = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        try:  # a decompress object ignores the end-of-line bytes after the stream
            raw = zlib.decompressobj().decompress(m[1])[:MAX_MEMBER]
            parts.append(raw.decode("utf-8", errors="replace"))  # XMP metadata is UTF-8
            parts.append(raw.decode("latin-1"))  # content-stream strings are byte strings
        except zlib.error:
            parts.append(m[1].decode("latin-1"))
    return "\n".join(parts)


def problems(root: Path, files: list[Path] | None = None) -> list[str]:
    files = candidate_files(root) if files is None else files
    terms = load_private_terms(root)
    root_abs = Path(os.path.abspath(root))
    found: list[str] = []

    def has_term(text: str) -> bool:
        return any(t.found_in(text) for t in terms)

    for f in files:
        f_abs = Path(os.path.abspath(f))  # where the file sits, not where a symlink points
        try:
            rel = f_abs.relative_to(root_abs)
        except ValueError:
            found.append("<path withheld>: outside the repo")
            continue
        posix = rel.as_posix()
        name_hit = bool(terms) and has_term(posix)
        label = WITHHELD if name_hit else posix
        if name_hit:
            found.append(f"{label}: a file name holds a private term (see {PRIVATE_TERMS_FILE})")
        lower = tuple(p.lower() for p in rel.parts)
        in_sample = rel.parts[0] == SAMPLE_DIR
        if PRIVATE_TERMS_FILE in lower:
            found.append(f"{label}: a private terms list must stay git-ignored")
            continue
        if f.suffix.lower() in COURSE_SUFFIXES and not in_sample:
            found.append(f"{label}: course-type file outside {SAMPLE_DIR}/")
        if "corpus" in lower[:-1]:
            found.append(f"{label}: a corpus/ folder must never be committed")
        # the allowance is exact (case-sensitive); detection of held-out folders is not
        if _heldout(lower) and rel.parts != tuple(HELDOUT_ALLOWED.split("/")):
            found.append(f"{label}: held-out eval data must stay git-ignored")
        if os.path.islink(f_abs):
            try:
                link_text = os.readlink(f_abs)  # git commits this text as the blob
            except OSError:
                link_text = ""
            if terms and has_term(link_text):
                found.append(f"{label}: a symlink target holds a private term")
            target = Path(os.path.realpath(f_abs))
            if not target.is_relative_to(Path(os.path.realpath(root_abs))):
                found.append(f"{label}: symlink points outside the repo")
            if target.suffix.lower() in COURSE_SUFFIXES and not in_sample:
                found.append(f"{label}: symlink to a course-type file outside {SAMPLE_DIR}/")
            continue
        try:
            data = f_abs.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue  # deleted in the working tree, or a submodule folder
        except OSError:
            found.append(f"{label}: unreadable, so it cannot be checked")
            continue
        if (
            not in_sample
            and data.startswith(COURSE_MAGIC)
            and f.suffix.lower() not in COURSE_SUFFIXES
        ):
            found.append(f"{label}: document or archive content outside {SAMPLE_DIR}/")
        # NUL-free UTF-8 is text whatever its first bytes; other bytes are an allowed binary
        # (folder + suffix + matching magic, D-27) or a finding (D-26)
        try:
            text = data.decode("utf-8")
            utf8_text = "\x00" not in text
        except UnicodeDecodeError:
            text, utf8_text = "", False
        if not utf8_text:
            text = ""
            if not allowed_binary(rel.parts, f.suffix, data):
                found.append(
                    f"{label}: not UTF-8 text; text must be UTF-8, and binaries belong in "
                    "sample_course/ or docs/assets/ with a suffix matching their content "
                    "(D-26, D-27)"
                )
                continue
        if terms:
            if data.startswith((*ARCHIVE_MAGIC, b"%PDF")):
                text += "\n" + archive_text(data)
            if has_term(text):
                found.append(f"{label}: contains a private term (see {PRIVATE_TERMS_FILE})")
    return found


def self_test() -> int:
    failures = 0

    def check(name: str, ok: bool) -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'WRONG'}] {name}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / SAMPLE_DIR).mkdir()
        (root / SAMPLE_DIR / "s01.pptx").write_bytes(b"PK")
        (root / "README.md").write_text("A public course assistant.\n", encoding="utf-8")
        check("clean tree passes", problems(root) == [])

        (root / "decks").mkdir()
        (root / "decks" / "real.pdf").write_bytes(b"%PDF")
        check(
            "course file outside sample_course fails", any("real.pdf" in p for p in problems(root))
        )
        (root / "decks" / "real.pdf").unlink()

        (root / "corpus").mkdir()
        (root / "corpus" / "s03.txt").write_text("slide text", encoding="utf-8")
        check("anything under corpus/ fails", any("corpus/s03.txt" in p for p in problems(root)))
        (root / "corpus" / "s03.txt").unlink()

        (root / "golden" / "heldout").mkdir(parents=True)
        (root / "golden" / "heldout" / "MANIFEST.json").write_text("{}", encoding="utf-8")
        check("held-out manifest is allowed", problems(root) == [])
        (root / "golden" / "heldout" / "cases.jsonl").write_text("{}", encoding="utf-8")
        check("held-out cases fail", any("cases.jsonl" in p for p in problems(root)))
        (root / "golden" / "heldout" / "cases.jsonl").unlink()

        (root / PRIVATE_TERMS_FILE).write_text("# names\nZanzibar Institute\n", encoding="utf-8")
        (root / "notes.md").write_text(
            "Pilot with the zanzibar institute team.\n", encoding="utf-8"
        )
        found = problems(root, [root / "notes.md"])
        check("private term fails, case-insensitive", any("notes.md" in p for p in found))
        check(
            "the failure never prints the term itself",
            not any("zanzibar" in p.lower() for p in found),
        )
        found = problems(root, [root / PRIVATE_TERMS_FILE])
        check("a tracked private-terms file fails", any(PRIVATE_TERMS_FILE in p for p in found))
        variants = {
            "utf-16": "Zanzibar Institute".encode("utf-16"),
            "split across a line": b"the Zanzibar\n  Institute",
            "nbsp inside": "Zanzibar\u00a0Institute".encode(),
            "fullwidth letter": "\uff3aanzibar Institute".encode(),
        }
        for label, data in variants.items():
            (root / "v.txt").write_bytes(data)
            check(f"private term found: {label}", bool(problems(root, [root / "v.txt"])))
        (root / "v.png").write_bytes(b"logo Zanzibar Institute")
        check("private term found under a binary suffix", bool(problems(root, [root / "v.png"])))
        named = root / "Zanzibar Institute pilot.md"
        named.write_text("nothing here\n", encoding="utf-8")
        found = problems(root, [named])
        check("private term in a file name fails", bool(found))
        check("a file name hit is withheld", not any("zanzibar" in p.lower() for p in found))
        (root / "data" / "corpus").mkdir(parents=True)
        (root / "data" / "corpus" / "u.txt").write_text("x", encoding="utf-8")
        check("nested corpus/ fails", bool(problems(root, [root / "data" / "corpus" / "u.txt"])))
        outside = Path(tmp).parent / "outside-target.pdf"
        link = root / "sample_course" / "link.pdf"
        try:
            link.symlink_to(outside)
            check("symlink leaving the repo fails", bool(problems(root, [link])))
        except OSError:
            check("symlink leaving the repo fails (symlinks unsupported here)", True)
        more = {
            "comment-prefixed line split": b"# Zanzibar\n# Institute",
            "quote-prefixed line split": b"> Zanzibar\n> Institute",
            "hyphen slug": b"see zanzibar-institute-pilot",
            "zero-width space": "Zanzibar Insti\u200btute".encode(),
            "soft hyphen": "Zanzi\u00adbar Institute".encode(),
            "html entity": b"Zanzibar&nbsp;Institute",
            "percent-encoding": b"Zanzibar%20Institute",
            "utf-32": "Zanzibar Institute".encode("utf-32"),
            "bom-less utf-16 mid-file": b"ascii header " + "Zanzibar Institute".encode("utf-16-le"),
        }
        for label, data in more.items():
            (root / "w.txt").write_bytes(data)
            check(f"private term found: {label}", bool(problems(root, [root / "w.txt"])))
        (root / "w.txt").write_bytes(b"An unrelated paragraph about institutes and islands.")
        check("unrelated words do not match", problems(root, [root / "w.txt"]) == [])
        slug = root / "zanzibar_institute.md"
        slug.write_text("x", encoding="utf-8")
        check("underscore slug in a file name fails", bool(problems(root, [slug])))
        (root / "x" / "CORPUS").mkdir(parents=True)
        (root / "x" / "CORPUS" / "a.txt").write_text("x", encoding="utf-8")
        check("CORPUS/ in any case fails", bool(problems(root, [root / "x" / "CORPUS" / "a.txt"])))
        (root / "data" / "golden" / "heldout").mkdir(parents=True)
        nested = root / "data" / "golden" / "heldout" / "c.jsonl"
        nested.write_text("x", encoding="utf-8")
        check("nested golden/heldout fails", bool(problems(root, [nested])))
        try:
            lnk = root / "plain-link"
            lnk.symlink_to("Zanzibar Institute notes.md")
            found = problems(root, [lnk])
            check("symlink target text with a term fails", bool(found))
            check(
                "symlink finding withholds the term",
                not any("zanzibar" in p.lower() for p in found),
            )
        except OSError:
            check("symlink target text (symlinks unsupported here)", True)
        bom_root = root / "bom"
        bom_root.mkdir()
        (bom_root / PRIVATE_TERMS_FILE).write_bytes(b"\xef\xbb\xbfQuokka Lab\n")
        (bom_root / "n.md").write_text("the quokka lab team", encoding="utf-8")
        check(
            "a BOM does not disable the first term", bool(problems(bom_root, [bom_root / "n.md"]))
        )
        (bom_root / PRIVATE_TERMS_FILE).write_bytes("Quokka Lab\n".encode("utf-16"))
        check("a UTF-16 terms file is read", bool(problems(bom_root, [bom_root / "n.md"])))
        if os.name != "nt" and os.geteuid() != 0:
            locked = root / "Zanzibar Institute locked.md"
            locked.write_text("x", encoding="utf-8")
            locked.chmod(0)
            try:
                found = problems(root, [locked])
                check("an unreadable file fails", bool(found))
                check(
                    "an unreadable file withholds its path",
                    not any("zanzibar" in p.lower() for p in found),
                )
            finally:
                locked.chmod(0o600)
        pass3 = {
            "html markup inside": b"Zanzibar <b>Institute</b>",
            "spans": b"<span>Zanzibar</span> <span>Institute</span>",
            "markdown link inside": b"[Zanzibar](https://x.org) Institute",
            "combining grapheme joiner": "Zanzibar Insti\u034ftute".encode(),
        }
        for label, data in pass3.items():
            (root / "p3.txt").write_bytes(data)
            check(f"private term found: {label}", bool(problems(root, [root / "p3.txt"])))
        (root / "p3.txt").write_text("Bridging zanzibari institutes.\n", encoding="utf-8")
        check(
            "no collapsed match across separate prose words",
            problems(root, [root / "p3.txt"]) == [],
        )
        (root / PRIVATE_TERMS_FILE).write_text(
            "Zanzibar Institute\nZoë Park\nAl Noor\n", encoding="utf-8"
        )
        (root / "p3.txt").write_bytes(b"header " + "Zoë Park".encode("utf-16-le"))
        check("non-ASCII term in BOM-less UTF-16", bool(problems(root, [root / "p3.txt"])))
        (root / "p3.txt").write_bytes("Al No\u034for".encode())
        check("short term broken by an invisible mark", bool(problems(root, [root / "p3.txt"])))
        (root / "p3.txt").write_text("Bridging theory and practice.\n", encoding="utf-8")
        check("ordinary prose is not flagged", problems(root, [root / "p3.txt"]) == [])
        (root / "p3.txt").unlink()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ppt/slides/slide1.xml", "<a:t>Zanzibar</a:t> <a:t>Institute</a:t>")
        (root / "sample_course" / "deck.pptx").write_bytes(buf.getvalue())
        check(
            "private term inside a sample pptx",
            bool(problems(root, [root / "sample_course" / "deck.pptx"])),
        )
        (root / "decks.zip").write_bytes(b"PK\x03\x04 archive")
        check("an archive outside sample_course fails", bool(problems(root, [root / "decks.zip"])))
        (root / "slides").write_bytes(b"%PDF-1.7 no extension")
        check("PDF content without an extension fails", bool(problems(root, [root / "slides"])))
        (root / "SAMPLE_COURSE").mkdir(exist_ok=True)
        upper = root / "SAMPLE_COURSE" / "x.pdf"
        upper.write_bytes(b"%PDF")
        found = problems(root, [Path(str(upper))])
        check("SAMPLE_COURSE/ is not the sample folder", bool(found))
        pass4 = {
            "term in a markdown link URL": b"[our partner](https://zanzibar-institute.org/pilot)",
            "term in an href": b'<a href="https://zanzibar-institute.org">partner</a>',
            "term in alt text": b'<img alt="Zanzibar Institute logo">',
            "json unicode escape": b'{"n": "Zo\\u00eb Park"}',
            "cp1252 text": "Zoë Park".encode("cp1252"),
        }
        for label, data in pass4.items():
            (root / "p4.txt").write_bytes(data)
            check(f"private term found: {label}", bool(problems(root, [root / "p4.txt"])))
        (root / "p4.txt").write_text("Zanzibar, Institute of nothing.\n", encoding="utf-8")
        check("a phrase never matches across a comma", problems(root, [root / "p4.txt"]) == [])
        (root / "p4.txt").unlink()
        slug = root / "zoe-park-notes.md"
        slug.write_text("x", encoding="utf-8")
        check("an unaccented slug of an accented term fails", bool(problems(root, [slug])))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("notes.txt", "meeting with Zanzibar Institute")
        (root / "sample_course" / "extra.zip").write_bytes(buf.getvalue())
        check(
            "an archive inside sample_course is opened",
            bool(problems(root, [root / "sample_course" / "extra.zip"])),
        )
        (root / "sample_course" / "extra.gz").write_bytes(gzip.compress(b"Zanzibar Institute"))
        check(
            "a gzip inside sample_course is opened",
            bool(problems(root, [root / "sample_course" / "extra.gz"])),
        )
        (root / PRIVATE_TERMS_FILE).write_text(
            "Zanzibar Institute\nSt. Xavier College\nA. Kumar\n", encoding="utf-8"
        )
        for label, data in {
            "term holding '. '": b"Pilot at St. Xavier College next term.",
            "initial and surname": b"Mentor: A. Kumar",
            "mid-word emphasis": b"Zan**zibar** Institute",
            "hyphenated line break": b"Zanzi-\nbar Institute",
            "quoted-printable": b"Zanzibar=20Insti=\ntute",
        }.items():
            (root / "p5.txt").write_bytes(data)
            check(f"private term found: {label}", bool(problems(root, [root / "p5.txt"])))
        (root / "p5.txt").unlink()
        pdfname = root / "St. Xavier College slides.pdf"
        pdfname.write_bytes(b"%PDF-1.4")
        found = problems(root, [pdfname])
        check(
            "a term holding '. ' in a file name is withheld",
            bool(found) and not any("xavier" in p.lower() for p in found),
        )
        stream = zlib.compress(b"BT (Zanzibar Institute) Tj ET")
        pdf = b"%PDF-1.4\n1 0 obj<<>>stream\n" + stream + b"\nendstream\nendobj\n"
        (root / "sample_course" / "deck.pdf").write_bytes(pdf)
        tight = (
            b"%PDF-1.4\n1 0 obj<<>>stream\n"
            + zlib.compress(b"(Zanzibar Institute) Tj")
            + b"endstream"
        )
        (root / "sample_course" / "tight.pdf").write_bytes(tight)
        check(
            "a PDF stream with no line end before endstream is checked",
            bool(problems(root, [root / "sample_course" / "tight.pdf"])),
        )
        (root / "sample_course" / "bad.gz").write_bytes(b"\x1f\x8b\x08\x00garbage")
        try:
            problems(root, [root / "sample_course" / "bad.gz"])
            check("a corrupt gzip does not crash the guard", True)
        except Exception:
            check("a corrupt gzip does not crash the guard", False)
        check(
            "text inside a sample PDF stream is checked",
            bool(problems(root, [root / "sample_course" / "deck.pdf"])),
        )
        (root / "notes.txt.bz2").write_bytes(bz2.compress(b"Zanzibar Institute"))
        check(
            "a bz2 archive outside sample_course fails",
            bool(problems(root, [root / "notes.txt.bz2"])),
        )
        (root / "lecture.vtt").write_bytes(b"WEBVTT\n\n00:00.000 --> 00:01.000\nhello")
        check(
            "a lecture transcript outside sample_course fails",
            bool(problems(root, [root / "lecture.vtt"])),
        )
        (root / PRIVATE_TERMS_FILE).write_text(
            "Bjørn Ødegaard\nJürgen Müller\nBo Yin\nHalvorsen Polytechnic\nMirela Ștefănescu\n",
            encoding="utf-8",
        )
        for label, data in {
            "slug of a non-decomposing letter": b"bjorn-odegaard-feedback",
            "umlaut written out": b"juergen.mueller@example.org",
            "json string escape": b'{"source": ["Halvorsen\\nPolytechnic"]}',
            "table cells": b"<td>Bo</td><td>Yin</td>",
            "line-break tag": b"Bo<br>Yin",
            "reference-style link": b"[Halvorsen][hp] Polytechnic",
            "utf-16 after ascii letters": b"ab" + "Mirela Ștefănescu".encode("utf-16-le"),
        }.items():
            (root / "p6.txt").write_bytes(data)
            check(f"private term found: {label}", bool(problems(root, [root / "p6.txt"])))
        (root / "p6.txt").write_text("Bob Yintang reviewed the table.\n", encoding="utf-8")
        check(
            "short term is not found inside longer words", problems(root, [root / "p6.txt"]) == []
        )
        (root / "p6.txt").unlink()
        (root / PRIVATE_TERMS_FILE).write_text(
            "Søren Kjeldby\nPaweł Lisowski\nAna Lima\n", encoding="utf-8"
        )
        for label, data in {
            "entity for ø": b"S&oslash;ren Kjeldby",
            "numeric entity for ł": b"Pawe&#322; Lisowski",
            "%-encoded ø slug": b"S%C3%B8ren-Kjeldby",
            "bom-less utf-32": b"x" + "Paweł Lisowski".encode("utf-32-le"),
        }.items():
            (root / "p7.txt").write_bytes(data)
            check(f"private term found: {label}", bool(problems(root, [root / "p7.txt"])))
        (root / "p7.txt").write_text("Ana,Lima are two cities.\n", encoding="utf-8")
        check("no phrase match across an unspaced comma", problems(root, [root / "p7.txt"]) == [])
        (root / "p7.txt").unlink()
        ent = root / "S&oslash;ren Kjeldby.pdf"
        ent.write_bytes(b"%PDF")
        found = problems(root, [ent])
        check(
            "an entity-encoded name in a path is withheld",
            bool(found) and not any("kjeldby" in p.lower() for p in found),
        )
        (root / PRIVATE_TERMS_FILE).write_text("A. Kumar\nSøren Kjeldby\n", encoding="utf-8")
        for label, data in {
            "spaced comma vs a term holding a full stop": b"We chose plan A, Kumar agreed.",
            "unspaced comma inside a slug": b"Soren,Kjeldby",
            "sentence-ending full stop": b"I met Soren. Kjeldby is a town.",
        }.items():
            (root / "p8.txt").write_bytes(data)
            check(f"no match: {label}", problems(root, [root / "p8.txt"]) == [])
        (root / "p8.txt").write_bytes(b"S%25C3%25B8ren Kjeldby")
        check("private term found: double %-encoding", bool(problems(root, [root / "p8.txt"])))
        (root / "p8.txt").write_bytes(b"Mentor: A. Kumar.")
        check("a term holding a full stop still matches", bool(problems(root, [root / "p8.txt"])))
        (root / "p8.txt").unlink()
        (root / PRIVATE_TERMS_FILE).write_text("Bo Yin\nSøren Kjeldby\n", encoding="utf-8")
        for name in ("Bo=20Yin.pdf", "=?utf-8?Q?S=C3=B8ren_Kjeldby?=.pdf"):
            qp = root / name
            qp.write_bytes(b"%PDF")
            found = problems(root, [qp])
            check(
                f"quoted-printable name found and withheld: {name[:6]}",
                bool(found)
                and not any("yin" in p.lower() or "kjeldby" in p.lower() for p in found),
            )
        for label, data in {
            "oe for ø": b"Soeren Kjeldby",
        }.items():
            (root / "p9.txt").write_bytes(data)
            check(f"private term found: {label}", bool(problems(root, [root / "p9.txt"])))
        for label, data in {
            "soft hyphen inside a longer word": "Bo Yin\u00adtang reviewed the table.".encode(),
            "zero-width space inside a longer word": "Tur\u200bbo Yin".encode(),
            "full stop before a tag": b"I met Bo.<br>Yin came later",
            "full stop before a closing quote": b'"I met Bo." Yin came later',
        }.items():
            (root / "p9.txt").write_bytes(data)
            check(f"no match: {label}", problems(root, [root / "p9.txt"]) == [])
        for label, text in {
            "utf-16 soft hyphen in a longer word": "Bo Yin\u00adtang reviewed it.",
            "utf-16 accented longer word": "Bo Yin\u00e7a met us.",
            "utf-16 a-ring before the term": "\u00c5bo Yin",
        }.items():
            for enc in ("utf-16", "utf-16-le", "utf-32"):
                (root / "p9.txt").write_bytes(text.encode(enc))
                found = problems(root, [root / "p9.txt"])
                check(
                    f"only the encoding finding, no term match: {label} ({enc})",
                    bool(found) and all("not UTF-8" in p for p in found),
                )
        (root / "p9.txt").write_bytes("Tur\u200bbo Yin".encode() + b" caf\xe9")
        found = problems(root, [root / "p9.txt"])
        check(
            "stray cp1252 byte: only the encoding finding",
            bool(found) and all("not UTF-8" in p for p in found),
        )
        (root / "p9.txt").write_bytes(b"I met **Bo.** Yin came later.")
        check("no match: full stop before emphasis", problems(root, [root / "p9.txt"]) == [])
        (root / "p9.txt").unlink()
        low = root / "golden" / "heldout" / "manifest.json"
        low.write_text('{"cases": [1]}', encoding="utf-8")
        check("held-out allowance is case-sensitive", bool(problems(root, [low])))
        (root / PRIVATE_TERMS_FILE).write_text("Zanzibar Institute\n", encoding="utf-8")
        for name, data in {
            "font.ttf": b"\x00\x01\x00\x00\x00\x10\x01\x00\x00\x04\x00\x00",
            "font.otf": b"OTTO\x00\x0b\x00\x80\x00\x03\x00\x30",
            "image.tiff": b"II*\x00\x08\x00\x00\x00",
            "movie.mov": b"\x00\x00\x00\x08wide\x00\x00",
            "icon.ico": b"\x00\x00\x01\x00\x01\x00\x10\x10",
        }.items():
            (root / "sample_course" / name).write_bytes(data)
            check(
                f"allowed binary (folder, suffix and magic agree): {name}",
                problems(root, [root / "sample_course" / name]) == [],
            )
        (root / "sample_course" / "t.png").write_bytes("\u4949* notes".encode("utf-16-le"))
        check(
            "a suffix whose magic does not match is an encoding finding",
            bool(problems(root, [root / "sample_course" / "t.png"])),
        )
        (root / "sample_course" / "t.dat").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        check(
            "magic without a known suffix is an encoding finding",
            bool(problems(root, [root / "sample_course" / "t.dat"])),
        )
        (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        check(
            "a binary outside the allowed folders fails", bool(problems(root, [root / "logo.png"]))
        )
        (root / "sample_course" / "bin.dat").write_bytes(
            "\u0100 Zanzibar Institute".encode("utf-32-be")
        )
        check(
            "UTF-32BE text that looks like an icon is an encoding finding",
            bool(problems(root, [root / "sample_course" / "bin.dat"])),
        )
        (root / "sample_course" / "bin.dat").write_bytes(b"ID3 tags and Zanzibar Institute notes")
        check(
            "UTF-8 text starting with a magic prefix is term-checked",
            bool(problems(root, [root / "sample_course" / "bin.dat"])),
        )
        (root / "sample_course" / "bin.dat").write_bytes(b"> Zanzibar Insti-\n> tute")
        check(
            "private term found: hyphen break behind >",
            bool(problems(root, [root / "sample_course" / "bin.dat"])),
        )
        (root / "sample_course" / "bin.dat").unlink()
        (root / PRIVATE_TERMS_FILE).write_text("Søren Kjeldby\nPaweł Lisowski\n", encoding="utf-8")
        for label, data in {
            "cp1252 starting BM": "BMW notes: Søren Kjeldby".encode("cp1252"),
            "cp1252 with 'free' at bytes 4-8": "Get free café, Søren Kjeldby".encode("cp1252"),
            "UTF-32LE starting with a font-like header": "\u0100da\u017ei S\u00f8ren".encode(
                "utf-32-le"
            ),
        }.items():
            (root / "sample_course" / "t.dat").write_bytes(data)
            check(
                f"non-UTF-8 text is flagged, not taken for binary: {label}",
                bool(problems(root, [root / "sample_course" / "t.dat"])),
            )
        (root / "sample_course" / "t.dat").unlink()
        for i in range(400):
            body = f"BT (Pawel Lisowski note {i}) Tj ET".encode()
            comp = zlib.compress(body)
            if comp.endswith(b"\r"):
                pdf = b"%PDF-1.4\n1 0 obj<<>>stream\n" + comp + b"\nendstream\n"
                (root / "sample_course" / "cr.pdf").write_bytes(pdf)
                check(
                    "a PDF stream whose data ends in CR is read",
                    bool(problems(root, [root / "sample_course" / "cr.pdf"])),
                )
                break
    print("self-test", "PASSED" if failures == 0 else f"FAILED ({failures})")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    found = problems(ROOT)
    if found:
        print("corpus guard: FAIL")
        for p in found:
            print(f"  - {p}")
        return 1
    print("corpus guard: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
