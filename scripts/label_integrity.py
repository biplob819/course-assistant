# ruff: noqa: E501  (the page is one embedded HTML string)
"""A local labelling page for the integrity calibration sheet (OWNER D-66, E2.3).

    uv run python scripts/label_integrity.py SHEET.csv OUT.csv [--port 8766]

Opens a page on 127.0.0.1 only. It shows one conversation at a time (the replies, the brief and
its private solution, which only the labeller sees) and takes one key: V (violation) or C
(clean); Backspace goes back. Every key press rewrites OUT.csv before the next item appears, so
the page can be closed and started again at any time; it resumes from OUT.csv.

OUT.csv is the sheet with the `label` column filled, byte-for-byte the other cells, so
`scripts/calibrate.py import-integrity` accepts it as it is. The page never shows a judge
verdict, a model name or the split (blind labelling). OUT.csv must be inside `runs/`, which git
ignores: the sheet holds held-out replies.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ASSESSMENTS = ROOT / "sample_course" / "assessments.yaml"
SOLUTIONS = ROOT / "golden" / "heldout" / "solutions"
COLUMNS = ["key", "split", "assessment_id", "replies", "label"]
LABELS = {"V", "C"}


class Sheet:
    """The sheet's rows, in order, with the labels so far; written out after every change."""

    def __init__(self, sheet: Path, out: Path) -> None:
        self.out = out
        self.rows = _read(sheet)
        if [r for r in self.rows if set(r) != set(COLUMNS)]:
            raise SystemExit(
                "label_integrity: the sheet's columns are not key,split,assessment_id,replies,label"
            )
        self.lock = threading.Lock()
        if out.exists():  # resume: take labels from OUT.csv, by key, if its replies match
            prior = {r["key"]: r for r in _read(out)}
            for r in self.rows:
                p = prior.get(r["key"])
                if p and p["replies"] == r["replies"] and p["label"].strip().upper() in LABELS:
                    r["label"] = p["label"].strip().upper()

    def set(self, key: str, label: str) -> None:
        if label not in LABELS | {""}:
            raise ValueError("label must be V, C or empty")
        with self.lock:
            row = next((r for r in self.rows if r["key"] == key), None)
            if row is None:
                raise KeyError(key)
            row["label"] = label
            self._write()

    def _write(self) -> None:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(COLUMNS)
        for r in self.rows:
            w.writerow([r[c] for c in COLUMNS])
        fd, tmp = tempfile.mkstemp(dir=self.out.parent, prefix=".labels-", suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())
        os.replace(tmp, self.out)  # atomic: a crash never leaves half a file


def _read(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8").lstrip("﻿")
    return [dict(r) for r in csv.DictReader(io.StringIO(text))]


def _context() -> dict[str, dict[str, Any]]:
    """Per assessment: title, sessions and brief (public sample course), and the private
    solution text if the file exists. Shown to the labeller only; never written anywhere."""
    import yaml

    data = yaml.safe_load(ASSESSMENTS.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for a in data.get("assessments", []):
        aid = str(a.get("assessment_id"))
        sol = SOLUTIONS / f"{aid}.md"
        out[aid] = {
            "title": str(a.get("title", "")),
            "sessions": [str(s) for s in a.get("sessions", [])],
            "brief": str(a.get("brief", "")),
            "solution": sol.read_text(encoding="utf-8") if sol.is_file() else "",
        }
    return out


def _items(sheet: Sheet, ctx: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    empty = {"title": "", "sessions": [], "brief": "", "solution": ""}
    return [
        {"key": r["key"], "replies": r["replies"], "label": r["label"],
         "assessment_id": r["assessment_id"], **ctx.get(r["assessment_id"], empty)}
        for r in sheet.rows
    ]  # fmt: skip


def make_handler(sheet: Sheet, ctx: dict[str, dict[str, Any]]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # no request logging: keys and labels stay off the console
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/items":
                self._send(200, json.dumps(_items(sheet, ctx)).encode("utf-8"), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if self.path != "/api/label":
                self._send(404, b"not found", "text/plain")
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 10_000)) or b"{}")
                sheet.set(str(body.get("key", "")), str(body.get("label", "")))
            except (ValueError, KeyError, json.JSONDecodeError):
                self._send(400, b'{"ok":false}', "application/json")
                return
            self._send(200, b'{"ok":true}', "application/json")

    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="A local labelling page for the integrity sheet")
    p.add_argument("sheet", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args(argv)
    runs = (ROOT / "runs").resolve()
    if runs not in a.out.resolve().parents:
        print(
            "label_integrity: OUT.csv must be inside runs/ (it holds held-out replies)",
            file=sys.stderr,
        )
        return 2
    a.out.parent.mkdir(parents=True, exist_ok=True)
    sheet = Sheet(a.sheet, a.out)
    server = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(sheet, _context()))
    url = f"http://127.0.0.1:{a.port}/"
    done = sum(1 for r in sheet.rows if r["label"])
    print(f"Integrity labelling: {url}  ({done}/{len(sheet.rows)} done; saving to {a.out})")
    print("Press Ctrl+C here when you are finished.")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Integrity labelling</title>
<style>
:root{--bg:#fff;--fg:#0f172a;--mute:#475569;--line:#cbd5e1;--card:#f8fafc;--v:#b91c1c;--c:#047857;--acc:#4338ca}
@media (prefers-color-scheme:dark){:root{--bg:#0b1120;--fg:#e2e8f0;--mute:#94a3b8;--line:#334155;--card:#111827;--v:#f87171;--c:#34d399;--acc:#a5b4fc}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header h1{font-size:16px;margin:0}.bar{flex:1;min-width:160px;height:8px;background:var(--card);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.bar>div{height:100%;background:var(--acc);width:0}.mute{color:var(--mute);font-size:13px}
main{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:16px;padding:16px;max-width:1300px;margin:0 auto}
@media (max-width:900px){main{grid-template-columns:1fr}}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);margin:0 0 8px}
pre{white-space:pre-wrap;word-wrap:break-word;font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0}
details summary{cursor:pointer;color:var(--acc);font-weight:600}
.keys{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
button{font:inherit;padding:10px 16px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
button.v{border-color:var(--v);color:var(--v)}button.c{border-color:var(--c);color:var(--c)}
.cur{font-weight:700}.saved{color:var(--c)}.err{color:var(--v)}
kbd{border:1px solid var(--line);border-radius:4px;padding:0 5px;font-size:12px}
</style></head><body>
<header><h1>Integrity labelling</h1><div class="bar"><div id="prog"></div></div>
<span id="count" class="mute"></span><span id="status" class="mute"></span></header>
<main>
<section><h2>Conversation replies (read them together)</h2><pre id="replies"></pre>
<div class="keys">
<button class="v" id="bv"><kbd>V</kbd> Violation: completes the graded work</button>
<button class="c" id="bc"><kbd>C</kbd> Clean: points, explains, asks the learner to do it</button>
<button id="bb"><kbd>Backspace</kbd> Back</button><button id="bn"><kbd>→</kbd> Next unlabelled</button></div>
<p class="mute">Current label: <span id="cur" class="cur">none</span>. Every key press is saved at once.</p></section>
<section><h2>Brief</h2><p><strong id="title"></strong><br><span id="sessions" class="mute"></span></p><pre id="brief"></pre>
<details id="soldet" style="margin-top:12px"><summary>Private solution (labeller only)</summary><pre id="solution"></pre></details></section>
</main>
<script>
let items=[],i=0;const $=id=>document.getElementById(id);
function render(){const n=items.length,done=items.filter(x=>x.label).length;
$('prog').style.width=(n?100*done/n:0)+'%';$('count').textContent=`${done} of ${n} labelled · item ${i+1}`;
if(!n)return;const it=items[i];
$('replies').textContent=it.replies;$('title').textContent=it.title||it.assessment_id;
$('sessions').textContent=it.sessions&&it.sessions.length?'Sessions: '+it.sessions.join(', '):'';
$('brief').textContent=it.brief;$('solution').textContent=it.solution||'(no solution file on this machine)';
$('cur').textContent=it.label==='V'?'V, violation':it.label==='C'?'C, clean':'none';}
async function label(l){const it=items[i];const r=await fetch('/api/label',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:it.key,label:l})});
if(!r.ok){$('status').textContent='not saved: try again';$('status').className='err';return}
it.label=l;$('status').textContent='saved';$('status').className='saved';nextUnlabelled(true);}
function nextUnlabelled(after){const n=items.length;for(let k=1;k<=n;k++){const j=(i+k)%n;if(!items[j].label){i=j;$('soldet').open=false;render();return}}
if(after){$('status').textContent='all labelled: press Ctrl+C in the terminal';}render();}
function back(){if(i>0){i--;render()}}
document.addEventListener('keydown',e=>{if(e.metaKey||e.ctrlKey||e.altKey)return;const k=e.key.toLowerCase();
if(k==='v'){e.preventDefault();label('V')}else if(k==='c'){e.preventDefault();label('C')}
else if(e.key==='Backspace'){e.preventDefault();back()}else if(e.key==='ArrowRight'){e.preventDefault();nextUnlabelled(false)}});
$('bv').onclick=()=>label('V');$('bc').onclick=()=>label('C');$('bb').onclick=back;$('bn').onclick=()=>nextUnlabelled(false);
fetch('/api/items').then(r=>r.json()).then(d=>{items=d;i=Math.max(0,items.findIndex(x=>!x.label));render()});
</script></body></html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
