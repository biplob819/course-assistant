"""The local integrity labelling page (scripts/label_integrity.py): the output keeps every cell
of the sheet but the label, resumes from its own file, refuses bad labels and unknown keys,
and never leaves runs/. Synthetic sheets only."""

from __future__ import annotations

import csv
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from scripts import label_integrity as L

SHEET = (
    "key,split,assessment_id,replies,label\n"
    'k1,heldout,s01-a1,"Reply one, with a comma\nand a new line",\n'
    'k2,tuning,s02-a1,"Reply ""two""",\n'
)


def _sheet(tmp_path: Path) -> Path:
    p = tmp_path / "sheet.csv"
    p.write_text(SHEET, encoding="utf-8")
    return p


def _rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


def test_labels_change_only_the_label_column(tmp_path):
    out = tmp_path / "labels.csv"
    s = L.Sheet(_sheet(tmp_path), out)
    s.set("k1", "V")
    s.set("k2", "C")
    before, after = _rows(tmp_path / "sheet.csv"), _rows(out)
    assert [r["label"] for r in after] == ["V", "C"]
    for b, a in zip(before, after, strict=True):
        assert {k: v for k, v in b.items() if k != "label"} == {
            k: v for k, v in a.items() if k != "label"
        }


def test_it_resumes_from_its_own_file_and_ignores_edited_replies(tmp_path):
    out = tmp_path / "labels.csv"
    L.Sheet(_sheet(tmp_path), out).set("k1", "C")
    assert [r["label"] for r in L.Sheet(_sheet(tmp_path), out).rows] == ["C", ""]
    edited = out.read_text(encoding="utf-8").replace("Reply one", "Reply ONE")
    out.write_text(edited, encoding="utf-8")
    assert [r["label"] for r in L.Sheet(_sheet(tmp_path), out).rows] == ["", ""]


def test_bad_labels_and_unknown_keys_are_refused(tmp_path):
    s = L.Sheet(_sheet(tmp_path), tmp_path / "labels.csv")
    with pytest.raises(ValueError):
        s.set("k1", "X")
    with pytest.raises(KeyError):
        s.set("nope", "V")


def test_out_must_be_inside_runs(tmp_path, capsys):
    assert L.main([str(_sheet(tmp_path)), str(tmp_path / "labels.csv"), "--no-browser"]) == 2
    assert "inside runs/" in capsys.readouterr().err


def _post(base: str, key: str, label: str) -> dict[str, bool]:
    body = json.dumps({"key": key, "label": label}).encode()
    req = urllib.request.Request(base + "/api/label", data=body, method="POST")
    return json.loads(urllib.request.urlopen(req).read())


def test_the_page_serves_items_without_verdicts_and_saves_each_label(tmp_path):
    out = tmp_path / "labels.csv"
    s = L.Sheet(_sheet(tmp_path), out)
    ctx = {"s01-a1": {"title": "T", "sessions": ["s01"], "brief": "B", "solution": "S"}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(s, ctx))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        items = json.loads(urllib.request.urlopen(base + "/api/items").read())
        assert {"split", "verdict", "judge"}.isdisjoint(items[0])  # blind: no split, no verdict
        assert _post(base, "k2", "V") == {"ok": True}
        assert [r["label"] for r in _rows(out)] == ["", "V"]
        with pytest.raises(urllib.error.HTTPError):
            _post(base, "k2", "maybe")
    finally:
        server.shutdown()
        server.server_close()
