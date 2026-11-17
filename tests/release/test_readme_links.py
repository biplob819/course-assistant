"""The README renders on PyPI (R3.1 OSS-09): every image and link is an absolute URL.

PyPI shows the README without the repository around it, so a relative `docs/assets/x.svg`
or `SECURITY.md` breaks there. Images and SVGs use raw.githubusercontent.com; other files use
github.com blob or tree URLs. Only in-page `#anchors` stay relative.
"""

from __future__ import annotations

import re

from harness_lib import ROOT

README = ROOT / "README.md"
REPO = "https://github.com/biplob819/course-assistant/"
RAW = "https://raw.githubusercontent.com/biplob819/course-assistant/main/"
IMG_EXT = (".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp")

INLINE = re.compile(r"(!?)\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HTML = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)
REFDEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", re.M)


def _targets(text: str) -> list[tuple[bool, str]]:
    found = [(bang == "!", url) for bang, url in INLINE.findall(text)]
    found += [(url.lower().endswith(IMG_EXT), url) for url in HTML.findall(text)]
    found += [(url.lower().endswith(IMG_EXT), url) for url in REFDEF.findall(text)]
    return found


def _code_stripped(text: str) -> str:
    text = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)
    return re.sub(r"`[^`\n]*`", "", text)


def test_parser_sees_relative_links():
    sample = '![a](docs/x.svg) [b](SECURITY.md) [c](#top) <img src="y.png">\n[d]: CONTRIBUTING.md'
    urls = [u for _, u in _targets(sample)]
    assert urls == ["docs/x.svg", "SECURITY.md", "#top", "y.png", "CONTRIBUTING.md"]


def test_every_link_and_image_is_absolute():
    text = _code_stripped(README.read_text(encoding="utf-8"))
    targets = _targets(text)
    assert targets, "README has no links"
    bad = [u for _, u in targets if not (u.startswith(("https://", "mailto:", "#")))]
    assert not bad, f"relative links break on PyPI: {bad}"


def test_repo_images_use_raw_urls():
    text = _code_stripped(README.read_text(encoding="utf-8"))
    for is_img, url in _targets(text):
        if url.startswith(REPO) and url.lower().split("?")[0].endswith(IMG_EXT):
            raise AssertionError(f"repo image must use {RAW}: {url}")
        if is_img and url.startswith("https://raw.githubusercontent.com/"):
            assert url.startswith(RAW), url


def test_repo_file_links_point_at_main():
    text = _code_stripped(README.read_text(encoding="utf-8"))
    for _, url in _targets(text):
        if url.startswith(REPO) and re.match(rf"{re.escape(REPO)}(blob|tree)/", url):
            assert re.match(rf"{re.escape(REPO)}(blob|tree)/main/", url), url


def test_linked_repo_files_exist():
    """An absolute link to this repo's main branch must name a file that is in the repo."""
    from urllib.parse import unquote

    text = _code_stripped(README.read_text(encoding="utf-8"))
    missing = []
    for _, url in _targets(text):
        path = None
        m = re.match(rf"{re.escape(REPO)}(?:blob|tree)/main/([^#?]+)", url)
        if m:
            path = m.group(1)
        elif url.startswith(RAW):
            path = url[len(RAW) :].split("?")[0]
        if path and not (ROOT / unquote(path)).exists():
            missing.append(url)
    assert not missing, missing
