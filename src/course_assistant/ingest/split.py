"""Split manifest files into units: one per slide, one per pdf page, one per agenda (ING-01).

Each file's bytes are read once and checked against the manifest's sha256 before any parser
sees them, and a path that resolves outside the corpus root is refused. pypdf and python-pptx
are the optional `ingest` extra, imported only when a file of that type is read.
"""

from __future__ import annotations

import functools
import hashlib
import io
import re
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar

from course_assistant.schemas._base import (
    Ident,
    Model,
    NonBlank,
    NonNegInt,
    PosInt,
    SessionId,
    Sha256,
)
from course_assistant.schemas.config import Locator
from course_assistant.schemas.corpus import Kind, Manifest, ManifestEntry

MIN_WORDS = 3  # a unit with fewer words after its title is dropped (image-only slides)
KIND_LABEL = {"slides": "slides", "pre_read": "pre-read", "agenda": "agenda",
              "post_session": "post-session notes", "faq": "FAQ"}  # fmt: skip
ErrorKind = Literal["config", "reconcile"]


class IngestError(Exception):
    """Ingest could not finish. `kind` maps to an exit code (both are 2, Tech Spec §4); the
    message names manifest ids, locators and file ids only, never provider text or paths."""

    def __init__(self, kind: ErrorKind, detail: str, ids: Iterable[str] = ()) -> None:
        self.kind: ErrorKind = kind
        self.detail = detail
        self.ids = tuple(ids)
        super().__init__(f"{kind} error: {detail}")


P = ParamSpec("P")
R = TypeVar("R")


def single_exit(fn: Callable[P, R]) -> Callable[P, R]:
    """Every failure leaves as a fresh IngestError raised outside any except block, so no
    library exception rides along as cause or context (L-30)."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        failure: tuple[ErrorKind, str, tuple[str, ...]]
        try:
            return fn(*args, **kwargs)
        except IngestError as exc:
            failure = (exc.kind, exc.detail, exc.ids)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            failure = ("config", f"unexpected {type(exc).__name__}", ())
        raise IngestError(*failure)

    return wrapper


class Unit(Model):
    """One slide, page or agenda before upload. `key` is the D-30 locator (`s02:slides:9`)."""

    manifest_id: Ident
    session: SessionId
    kind: Kind
    number: PosInt
    key: Locator
    locator: NonBlank  # "slide 9" | "p. 3" | "agenda"
    label: NonBlank  # "Session 2 slides, slide 9"
    text: NonBlank
    version: Sha256


class Dropped(Model):
    manifest_id: Ident
    key: Locator
    words: NonNegInt


class Split(Model):
    units: tuple[Unit, ...]
    dropped: tuple[Dropped, ...]


def unit_version(key: str, text: str) -> str:
    """sha256 of locator and text: changes when and only when either changes (ING-02)."""
    return hashlib.sha256(f"{key}\n{text}".encode()).hexdigest()


_CONTROL = re.compile(r"[\x00-\x08\x0c\x0e-\x1f\x7f]")


def clean(text: str) -> str:
    """Line breaks normalised, control characters dropped, trailing spaces and blank runs gone."""
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n"))
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln.strip()).strip()


def words(text: str) -> int:
    return len(text.split())


def _read_verified(entry: ManifestEntry, root: Path) -> bytes:
    base = root.resolve()
    path = (base / entry.norm_path).resolve()
    if not path.is_relative_to(base):
        raise IngestError("config", f"{entry.manifest_id}: path leaves the corpus root")
    failed: str | None = None
    data = b""
    try:
        data = path.read_bytes()
    except OSError as exc:
        failed = exc.strerror or type(exc).__name__
    if failed is not None:
        raise IngestError("config", f"{entry.manifest_id}: cannot read the file ({failed})")
    if hashlib.sha256(data).hexdigest() != entry.sha256:
        raise IngestError(
            "config",
            f"{entry.manifest_id}: file hash differs from the manifest "
            "(review the change and update the manifest's sha256)",
        )
    return data


def _missing_extra(module: str) -> IngestError:
    return IngestError(
        "config", f"{module} is not installed: pip install 'course-assistant[ingest]'"
    )


def _pdf_pages(data: bytes) -> list[str]:
    try:
        import pypdf
    except ImportError:
        raise _missing_extra("pypdf") from None
    reader = pypdf.PdfReader(io.BytesIO(data))
    return [clean(page.extract_text() or "") for page in reader.pages]


def _shapes(shapes: Iterable[Any]) -> Iterator[Any]:
    for shape in shapes:
        if getattr(shape, "shape_type", None) is not None and hasattr(shape, "shapes"):
            yield from _shapes(shape.shapes)  # group shape: its children, in order
        else:
            yield shape


def _slides(data: bytes, notes: bool) -> list[tuple[str, str]]:
    """(title, rest) per slide: body text frames in shape order, then tables, then notes."""
    try:
        from pptx import Presentation
    except ImportError:
        raise _missing_extra("python-pptx") from None
    out: list[tuple[str, str]] = []
    for slide in Presentation(io.BytesIO(data)).slides:
        title_shape = slide.shapes.title
        title_id = title_shape.shape_id if title_shape is not None else None
        title = clean(title_shape.text_frame.text) if title_shape is not None else ""
        bodies: list[str] = []
        tables: list[str] = []
        for shape in _shapes(slide.shapes):
            if shape.shape_id == title_id:
                continue
            if getattr(shape, "has_text_frame", False):
                text = clean(shape.text_frame.text)
                if text:
                    bodies.append(text)
            elif getattr(shape, "has_table", False):
                rows = [" | ".join(clean(c.text).replace("\n", " ") for c in row.cells)
                        for row in shape.table.rows]  # fmt: skip
                tables.append("\n".join(rows))
        parts = bodies + tables
        if notes and slide.has_notes_slide:
            frame = slide.notes_slide.notes_text_frame
            note = clean(frame.text) if frame is not None else ""
            if note:
                parts.append("Speaker notes:\n" + note)
        out.append((title, "\n".join(parts)))
    return out


def _session_label(session: str, kind: str) -> str:
    return f"Session {int(session[1:])} {KIND_LABEL[kind]}"


def _entry_units(
    entry: ManifestEntry, data: bytes, start: int
) -> tuple[list[Unit], list[Dropped], int]:
    """Units of one entry, numbered from start + 1; returns how many numbers it used."""
    suffix = Path(entry.norm_path).suffix.lower()
    # (number, human locator, title prefix, text after the title)
    raw: list[tuple[int, str, str, str]] = []
    if suffix == ".pdf":
        pages = _pdf_pages(data)
        if entry.kind == "agenda":
            raw.append((start + 1, "agenda", "", "\n".join(p for p in pages if p)))
        else:
            raw += [(start + i, f"p. {start + i}", "", p) for i, p in enumerate(pages, 1)]
    elif suffix == ".pptx":
        slides = _slides(data, entry.include_speaker_notes)
        if entry.kind == "agenda":
            joined = "\n".join("\n".join(x for x in s if x) for s in slides)
            raw.append((start + 1, "agenda", "", joined))
        else:
            raw += [(start + i, f"slide {start + i}", t, rest)
                    for i, (t, rest) in enumerate(slides, 1)]  # fmt: skip
    else:
        raise IngestError(
            "config", f"{entry.manifest_id}: unsupported file type {suffix or '(none)'}"
        )
    units: list[Unit] = []
    dropped: list[Dropped] = []
    for n, locator, title, rest in raw:
        key = f"{entry.session}:{entry.kind}:{n}"
        if locator == "agenda" and n > 1:
            locator = f"agenda {n}"
        label = _session_label(entry.session, entry.kind)
        label = label if locator.startswith("agenda") else f"{label}, {locator}"
        count = words(rest)
        if count < MIN_WORDS:
            dropped.append(Dropped(manifest_id=entry.manifest_id, key=key, words=count))
            continue
        text = f"{title or label}\n{rest}" if locator.startswith("slide") else rest
        units.append(Unit(manifest_id=entry.manifest_id, session=entry.session, kind=entry.kind,
                          number=n, key=key, locator=locator, label=label, text=text,
                          version=unit_version(key, text)))  # fmt: skip
    return units, dropped, len(raw)


@single_exit
def units_from_manifest(manifest: Manifest, root: Path) -> Split:
    """Every unit of every manifest entry, and the units dropped as near-empty. Two entries
    with the same session and kind number on in manifest order, so locators never collide."""
    used: dict[tuple[str, str], int] = {}
    units: list[Unit] = []
    dropped: list[Dropped] = []
    for entry in manifest.entries:
        data = _read_verified(entry, Path(root))
        group = (entry.session, entry.kind)
        failed: str | None = None
        result: tuple[list[Unit], list[Dropped], int] = ([], [], 0)
        try:
            result = _entry_units(entry, data, used.get(group, 0))
        except IngestError:
            raise
        except Exception as exc:
            failed = type(exc).__name__
        if failed is not None:
            raise IngestError("config", f"{entry.manifest_id}: file cannot be parsed ({failed})")
        got, gone, n = result
        used[group] = used.get(group, 0) + n
        units += got
        dropped += gone
    return Split(units=tuple(units), dropped=tuple(dropped))
