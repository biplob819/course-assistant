#!/usr/bin/env python3
"""Fail if anything that looks like an API key is in the repo.

Scans tracked and untracked (not ignored) files. Lines containing the marker
`secret-scan: allow` are skipped (for documented fake examples only).

Known limit: a key split across string literals ("sk-ant-" "abc...") is not detected.
Provider-side secret scanning on GitHub is the backstop for that case.

Usage:
    python scripts/secret_scan.py [paths...]
    python scripts/secret_scan.py --self-test
"""

from __future__ import annotations

import argparse
import os
import quopri
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from harness_lib import ROOT

PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai key": re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"),
    "aws access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "aws secret key": re.compile(
        r"(?i)aws_?secret_?(?:access_?)?key\W{0,4}[=:]\W{0,4}[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"
    ),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "github fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}"),
    "pypi token": re.compile(r"\bpypi-[A-Za-z0-9_\-]{50,}"),
    "private key": re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    ),
}
ALLOW = "secret-scan: allow"
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".whl", ".gz", ".zip", ".ico"}


def candidate_files(root: Path) -> list[Path]:
    try:
        # -z: NUL-separated and never quoted, so non-ASCII paths are listed as they are
        # on disk (default core.quotepath would quote them and the scan would skip them)
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        # os.fsdecode keeps non-UTF-8 names (surrogateescape) instead of crashing
        return [root / os.fsdecode(p) for p in out.stdout.split(b"\0") if p]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", ".venv", "node_modules", "__pycache__", "dist"}
        return [p for p in root.rglob("*") if p.is_file() and not (skip & set(p.parts))]


BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)
WIDE = (("utf-16-le", 2), ("utf-16-be", 2), ("utf-32-le", 4), ("utf-32-be", 4))


def _plausible(ch: str) -> bool:
    """A character ordinary text is made of: printable ASCII, whitespace, or a Latin letter."""
    if ch.isascii():
        return ch.isprintable() or ch in "\n\r\t"
    return ch.isalpha() and ord(ch) < 0x0250


def readings(data: bytes) -> list[str]:
    """The plausible readings of a file's bytes. An alternative decoding is added only where
    the primary one fails, so ordinary text never gets a garbled second reading that splits
    words apart (W0.1 passes 10-11, L-24):
      - a BOM decides the encoding outright;
      - many NULs mean BOM-less UTF-16/32: the one byte order and alignment whose strict
        decoding reads most like text (the NUL-dropped bytes only if none decodes);
      - otherwise UTF-8, plus cp1252 only when no byte forms valid multi-byte UTF-8;
      - quoted-printable text also gets its decoded reading.
    """
    for bom, enc in BOMS:
        if data.startswith(bom):
            return [data.decode(enc, errors="replace")]
    out: list[str] = []
    if data.count(b"\x00") * 10 > len(data):  # more than 10% NULs: a wide encoding
        # Several byte orders and alignments can decode cleanly (UTF-16-LE "Åbo Yin" shifted
        # one byte is valid UTF-16-BE "bo Yin"), so keep only the most plausible reading,
        # preferring alignment from the start of the file on a tie.
        best, best_score = "", -1
        for offset in range(4):
            for enc, width in WIDE:
                if offset >= width:
                    continue
                chunk = data[offset:]
                chunk = chunk[: len(chunk) - len(chunk) % width]
                try:
                    text = chunk.decode(enc)
                except UnicodeDecodeError:
                    continue
                score = sum(_plausible(ch) for ch in text)
                if score > best_score:
                    best, best_score = text, score
        if best_score < 0:
            best = data.replace(b"\x00", b"").decode("utf-8", errors="replace")
        return [best]
    text = data.decode("utf-8", errors="replace")
    out.append(text)
    high = sum(b >= 0x80 for b in data)
    if high and text.count("\ufffd") >= high:  # no valid multi-byte UTF-8 at all: legacy text
        out.append(data.decode("cp1252", errors="replace"))
    if b"=\n" in data or b"=\r\n" in data or re.search(rb"=[0-9A-F]{2}", data):
        out.append(quopri.decodestring(data).decode("utf-8", errors="replace"))  # .eml bodies
    return out


def decode(data: bytes) -> str:
    """Every plausible reading of the bytes, joined into one text to search."""
    return "\n".join(readings(data))


def scan(files: list[Path]) -> list[str]:
    hits: list[str] = []
    for f in files:
        if f.suffix.lower() in SKIP_SUFFIXES or not f.is_file():
            continue
        try:
            # never skip a file for its encoding: every plausible reading is scanned
            texts = readings(f.read_bytes())
        except OSError:
            continue
        seen: set[tuple[str, str]] = set()  # one report per key, at its line in the first reading
        for text in texts:
            for lineno, line in enumerate(text.splitlines(), start=1):
                if ALLOW in line:
                    continue
                for name, pat in PATTERNS.items():
                    m = pat.search(line)
                    if m and (name, m.group(0)) not in seen:
                        seen.add((name, m.group(0)))
                        hits.append(f"{f}:{lineno}: possible {name}")
    return hits


def self_test() -> int:
    fake_ant = "sk-ant-" + "a1B2" * 10
    fake_oai = "sk-proj-" + "Z9y8" * 10
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "leak.py"
        bad.write_text(f'KEY = "{fake_ant}"\nOTHER = "{fake_oai}"\n', encoding="utf-8")
        allowed = Path(tmp) / "doc.md"
        allowed.write_text(f"example: {fake_ant}  # {ALLOW}\n", encoding="utf-8")
        clean = Path(tmp) / "clean.py"
        clean.write_text('KEY = os.environ["ANTHROPIC_API_KEY"]\n', encoding="utf-8")
        repo = Path(tmp) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "clé.py").write_text(f'K = "{fake_ant}"\n', encoding="utf-8")
        listed = candidate_files(repo)
        checks = [
            ("lists non-ASCII paths as they are on disk", all(p.is_file() for p in listed)),
            ("detects a key in a non-ASCII file name", len(scan(listed)) == 1),
            ("detects leaked keys", len(scan([bad])) == 2),
            ("honours allow marker", scan([allowed]) == []),
            ("clean file passes", scan([clean]) == []),
        ]
    for name, ok in checks:
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'WRONG'}] {name}")
    print("self-test", "PASSED" if failures == 0 else f"FAILED ({failures})")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    files = args.paths or candidate_files(ROOT)
    hits = scan(files)
    if hits:
        print("secret scan: FAIL")
        for h in hits:
            print(f"  - {h}")
        return 1
    print(f"secret scan: OK ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
