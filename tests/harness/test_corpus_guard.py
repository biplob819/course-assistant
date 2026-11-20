import subprocess

import pytest

import corpus_guard
from harness_lib import ROOT


def test_self_test_passes():
    assert corpus_guard.self_test() == 0


@pytest.mark.safety
def test_live_repo_has_no_course_material_or_private_terms():
    assert corpus_guard.problems(ROOT) == []


@pytest.mark.safety
def test_private_terms_corpus_and_heldout_are_git_ignored():
    for path in (".private-terms", "corpus/x.txt", "golden/heldout/cases.jsonl"):
        r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
        assert r.returncode == 0, path
    r = subprocess.run(
        ["git", "check-ignore", "-q", "golden/heldout/MANIFEST.json"], cwd=ROOT, check=False
    )
    assert r.returncode == 1, "the held-out manifest must be committable"


def test_course_file_outside_sample_course_fails(tmp_path):
    (tmp_path / "slides.pptx").write_bytes(b"PK")
    assert any("slides.pptx" in p for p in corpus_guard.problems(tmp_path))


def test_course_file_inside_sample_course_passes(tmp_path):
    (tmp_path / "sample_course").mkdir()
    (tmp_path / "sample_course" / "s01.pdf").write_bytes(b"%PDF")
    assert corpus_guard.problems(tmp_path) == []


@pytest.mark.safety
def test_private_term_is_caught_without_being_printed(tmp_path):
    (tmp_path / ".private-terms").write_text("# c\nAcme University\n", encoding="utf-8")
    (tmp_path / "doc.md").write_text("Pilot at ACME UNIVERSITY.\n", encoding="utf-8")
    found = corpus_guard.problems(tmp_path, [tmp_path / "doc.md"])
    assert found and not any("acme" in p.lower() for p in found)


@pytest.mark.safety
def test_non_ascii_file_names_are_scanned(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "decks").mkdir()
    (tmp_path / "decks" / "séance.pdf").write_bytes(b"%PDF")
    assert any("séance.pdf" in p for p in corpus_guard.problems(tmp_path))


@pytest.mark.safety
@pytest.mark.parametrize(
    "data",
    [
        b"# Acme\n# University",
        b"acme-university-pilot",
        "Acme Uni​versity".encode(),
        b"Acme&nbsp;University",
        b"Acme%20University",
        "Acme University".encode("utf-32"),
    ],
)
def test_disguised_private_terms_are_found(tmp_path, data):
    (tmp_path / ".private-terms").write_text("Acme University\n", encoding="utf-8")
    (tmp_path / "x.txt").write_bytes(data)
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"])


def test_short_terms_do_not_match_inside_longer_words(tmp_path):
    (tmp_path / ".private-terms").write_text("upgra\n", encoding="utf-8")
    (tmp_path / "x.txt").write_text("Run pip install --upgrade.\n", encoding="utf-8")
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"]) == []


@pytest.mark.safety
@pytest.mark.parametrize(
    "data",
    [b"Acme <b>University</b>", b"[Acme](https://x.org) University", b"acme_university.md"],
)
def test_markup_and_links_inside_a_term_are_found(tmp_path, data):
    (tmp_path / ".private-terms").write_text("Acme University\n", encoding="utf-8")
    (tmp_path / "x.txt").write_bytes(data)
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"])


def test_collapsed_match_never_joins_separate_prose_words(tmp_path):
    (tmp_path / ".private-terms").write_text("Theo Ryan\n", encoding="utf-8")
    (tmp_path / "x.txt").write_text("Bridging theory and practice.\n", encoding="utf-8")
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"]) == []


@pytest.mark.safety
def test_archives_and_unlabelled_documents_outside_the_sample_fail(tmp_path):
    (tmp_path / "decks.zip").write_bytes(b"PK\x03\x04")
    (tmp_path / "slides").write_bytes(b"%PDF-1.7")
    found = corpus_guard.problems(tmp_path, [tmp_path / "decks.zip", tmp_path / "slides"])
    assert len(found) == 2


@pytest.mark.safety
@pytest.mark.parametrize(
    "data",
    [
        b"[partner](https://acme-university.org/pilot)",
        b'<img alt="Acme University logo">',
        b'{"n": "Acme Univ\\u00e9rsity"}',
        "Acme Université".encode("cp1252"),
    ],
)
def test_terms_in_urls_attributes_and_encodings_are_found(tmp_path, data):
    (tmp_path / ".private-terms").write_text("Acme University\nAcme Université\n", encoding="utf-8")
    (tmp_path / "x.txt").write_bytes(data)
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"])


def test_phrases_do_not_match_across_a_comma(tmp_path):
    (tmp_path / ".private-terms").write_text("Answer Check\n", encoding="utf-8")
    (tmp_path / "x.txt").write_text("Write the answer, check the citations.\n", encoding="utf-8")
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"]) == []


def test_secret_scan_reports_each_key_once(tmp_path):
    import secret_scan

    f = tmp_path / "k.txt"
    f.write_bytes(b"line one\nKEY=sk-ant-" + b"Q" * 30 + b"\x00\n")
    hits = secret_scan.scan([f])
    assert len(hits) == 1 and ":2:" in hits[0]


@pytest.mark.safety
@pytest.mark.parametrize("term", ["St. Xavier College", "A. Kumar"])
def test_terms_holding_clause_punctuation_still_match(tmp_path, term):
    (tmp_path / ".private-terms").write_text(term + "\n", encoding="utf-8")
    (tmp_path / "x.txt").write_text(f"Pilot with {term} next term.\n", encoding="utf-8")
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"])


@pytest.mark.safety
def test_text_inside_a_sample_pdf_is_checked(tmp_path):
    import zlib

    (tmp_path / ".private-terms").write_text("Acme University\n", encoding="utf-8")
    (tmp_path / "sample_course").mkdir()
    pdf = b"%PDF-1.4\nstream\n" + zlib.compress(b"(Acme University) Tj") + b"\nendstream\n"
    (tmp_path / "sample_course" / "s.pdf").write_bytes(pdf)
    assert corpus_guard.problems(tmp_path, [tmp_path / "sample_course" / "s.pdf"])


@pytest.mark.safety
@pytest.mark.parametrize(
    ("term", "data"),
    [
        ("Bjørn Ødegaard", b"decks-bjorn-odegaard-notes"),
        ("Jürgen Müller", b"juergen.mueller@example.org"),
        ("Bo Yin", b"<td>Bo</td><td>Yin</td>"),
        ("Halvorsen Polytechnic", b'"Halvorsen\\nPolytechnic"'),
        ("Halvorsen Polytechnic", b"[Halvorsen][hp] Polytechnic"),
    ],
)
def test_ordinary_editing_forms_of_a_term_are_found(tmp_path, term, data):
    (tmp_path / ".private-terms").write_text(term + "\n", encoding="utf-8")
    (tmp_path / "x.txt").write_bytes(data)
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"])


def test_pre_commit_runs_both_scanners():
    cfg = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "scripts/secret_scan.py" in cfg and "scripts/corpus_guard.py" in cfg


@pytest.mark.safety
@pytest.mark.parametrize(
    "data", [b"S&oslash;ren Kjeldby", b"S%C3%B8ren-Kjeldby", "Søren Kjeldby".encode("utf-32-le")]
)
def test_letters_from_decoded_entities_are_transliterated(tmp_path, data):
    (tmp_path / ".private-terms").write_text("Søren Kjeldby\n", encoding="utf-8")
    (tmp_path / "x.txt").write_bytes(b"x " + data)
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"])


def test_a_term_with_a_full_stop_still_never_matches_across_a_comma(tmp_path):
    (tmp_path / ".private-terms").write_text("A. Kumar\n", encoding="utf-8")
    (tmp_path / "x.txt").write_text("We chose plan A, Kumar agreed.\n", encoding="utf-8")
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"]) == []
    (tmp_path / "x.txt").write_text("Mentor: A. Kumar.\n", encoding="utf-8")
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"])


@pytest.mark.safety
@pytest.mark.parametrize("name", ["Acme=20University.pdf", "=?utf-8?Q?Acme_University?=.pdf"])
def test_quoted_printable_file_names_are_found_and_withheld(tmp_path, name):
    (tmp_path / ".private-terms").write_text("Acme University\n", encoding="utf-8")
    (tmp_path / name).write_bytes(b"%PDF")
    found = corpus_guard.problems(tmp_path, [tmp_path / name])
    assert found and not any("acme" in p.lower() for p in found)


@pytest.mark.safety
@pytest.mark.parametrize("rel", ["golden/heldout/manifest.json", "Golden/HeldOut/MANIFEST.json"])
def test_held_out_allowance_is_exact(tmp_path, rel):
    assert corpus_guard.problems(tmp_path, [tmp_path / rel])


@pytest.mark.parametrize("text", ["Bo Yin­tang reviewed it.", "Tur​bo Yin", "Gum&shy;bo Yin"])
def test_invisible_characters_never_split_a_word_into_a_false_match(tmp_path, text):
    (tmp_path / ".private-terms").write_text("Bo Yin\n", encoding="utf-8")
    (tmp_path / "x.txt").write_text(text + "\n", encoding="utf-8")
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"]) == []


@pytest.mark.parametrize("enc", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
@pytest.mark.parametrize("text", ["Bo Yin­tang met us.", "Åbo Yin", "Bo Yinça"])
def test_non_utf8_text_is_an_encoding_finding_never_a_term_guess(tmp_path, enc, text):
    """D-26: the repo keeps text in UTF-8; other encodings are flagged, not decoded by guess."""
    (tmp_path / ".private-terms").write_text("Bo Yin\n", encoding="utf-8")
    (tmp_path / "x.txt").write_bytes(text.encode(enc))
    found = corpus_guard.problems(tmp_path, [tmp_path / "x.txt"])
    assert found and all("not UTF-8" in p for p in found)


@pytest.mark.safety
@pytest.mark.parametrize(
    ("name", "data"),
    [("logo.png", b"\x89PNG\r\n\x1a\n\x00\x00"), ("photo.jpg", b"\xff\xd8\xff\xe0\x00")],
)
def test_real_images_in_docs_assets_are_allowed(tmp_path, name, data):
    (tmp_path / "docs" / "assets").mkdir(parents=True)
    (tmp_path / "docs" / "assets" / name).write_bytes(data)
    assert corpus_guard.problems(tmp_path, [tmp_path / "docs" / "assets" / name]) == []


@pytest.mark.safety
@pytest.mark.parametrize(
    "rel",
    ["logo.png", "docs/assets/logo.dat", "docs/assets/fake.png"],
)
def test_binaries_need_folder_suffix_and_matching_magic(tmp_path, rel):
    """D-27: outside the allowed folders, without a known suffix, or with a suffix whose
    magic does not match, non-UTF-8 bytes are an encoding finding."""
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    data = (
        "\u4949* notes".encode("utf-16-le")
        if rel.endswith("fake.png")
        else b"\x89PNG\r\n\x1a\n\x00"
    )
    f.write_bytes(data)
    assert corpus_guard.problems(tmp_path, [f])


@pytest.mark.safety
@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("f.ttf", b"\x00\x01\x00\x00\x00\x10\x01\x00\x00\x04\x00\x00"),
        ("f.otf", b"OTTO\x00\x0b\x00\x80\x00\x03\x00\x30"),
        ("i.tiff", b"II*\x00\x08\x00"),
        ("m.mov", b"\x00\x00\x00\x08wide"),
    ],
)
def test_fonts_tiff_and_movies_in_the_sample_are_allowed(tmp_path, name, data):
    (tmp_path / "sample_course").mkdir()
    (tmp_path / "sample_course" / name).write_bytes(data)
    assert corpus_guard.problems(tmp_path, [tmp_path / "sample_course" / name]) == []


@pytest.mark.safety
def test_utf8_text_with_a_magic_prefix_is_still_checked(tmp_path):
    (tmp_path / ".private-terms").write_text("Acme University\n", encoding="utf-8")
    (tmp_path / "x.txt").write_bytes(b"BM notes from Acme University")
    assert corpus_guard.problems(tmp_path, [tmp_path / "x.txt"])


@pytest.mark.safety
def test_utf8_text_in_a_pdf_stream_is_found(tmp_path):
    import zlib

    (tmp_path / ".private-terms").write_text("Søren Kjeldby\n", encoding="utf-8")
    (tmp_path / "sample_course").mkdir()
    xmp = "<rdf:li>Søren Kjeldby</rdf:li>".encode()
    pdf = b"%PDF-1.4\nstream\n" + zlib.compress(xmp) + b"\nendstream\n"
    (tmp_path / "sample_course" / "s.pdf").write_bytes(pdf)
    assert corpus_guard.problems(tmp_path, [tmp_path / "sample_course" / "s.pdf"])


@pytest.mark.safety
def test_entity_inside_percent_encoding_is_found_and_withheld(tmp_path):
    (tmp_path / ".private-terms").write_text("Bo Yin\n", encoding="utf-8")
    f = tmp_path / "Bo%26nbsp%3BYin.pdf"
    f.write_bytes(b"%PDF-1.4")
    found = corpus_guard.problems(tmp_path, [f])
    assert found and not any("yin" in p.lower() for p in found)


@pytest.mark.safety
def test_latin1_text_outside_pdf_streams_is_found(tmp_path):
    (tmp_path / ".private-terms").write_text("Søren Kjeldby\n", encoding="utf-8")
    (tmp_path / "sample_course").mkdir()
    pdf = b"%PDF-1.4\n1 0 obj << /Author (" + "Søren Kjeldby".encode("latin-1") + b") >>\n"
    (tmp_path / "sample_course" / "s.pdf").write_bytes(pdf)
    assert corpus_guard.problems(tmp_path, [tmp_path / "sample_course" / "s.pdf"])


@pytest.mark.safety
def test_docs_assets_admits_images_and_fonts_only(tmp_path):
    (tmp_path / "docs" / "assets").mkdir(parents=True)
    f = tmp_path / "docs" / "assets" / "deck.zip"
    f.write_bytes(b"PK\x03\x04\x14\x00")
    assert corpus_guard.problems(tmp_path, [f])


def test_a_term_matches_its_plural_too():
    # T3.2 verifier: a plural ("...s") slipped past a singular term in the list
    from corpus_guard import Term

    assert Term("blue widget").found_in("notes for two blue widgets here")
    assert Term("glass box").found_in("three glass boxes")
    assert Term("blue widget").found_in("a blue widget")
    assert not Term("blue widget").found_in("blue widgetry is fine")
