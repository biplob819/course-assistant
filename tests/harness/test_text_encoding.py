"""Every text file read or write names its encoding (L-32). Windows defaults to cp1252, so
an implicit encoding passes on macOS and Linux and breaks on Windows CI or for Windows users."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CALLS = {"read_text", "write_text", "open"}


def is_os_open(f: ast.expr) -> bool:
    return (isinstance(f, ast.Attribute) and f.attr == "open"
            and isinstance(f.value, ast.Name) and f.value.id == "os")  # fmt: skip


def test_only_os_open_is_exempt():
    calls = {"os.open(p, 1)": True, "open(p)": False, "path.open()": False,
             "io.open(p)": False}  # fmt: skip
    for src, exempt in calls.items():
        call = ast.parse(src).body[0].value  # type: ignore[attr-defined]
        assert is_os_open(call.func) is exempt, src


def offenders():
    found = []
    for base in ("src", "scripts", "tests"):
        for path in (ROOT / base).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name not in CALLS:
                    continue
                if name == "open":
                    mode = (
                        node.args[1]
                        if len(node.args) > 1
                        else next((k.value for k in node.keywords if k.arg == "mode"), None)
                    )
                    if isinstance(mode, ast.Constant) and "b" in str(mode.value):
                        continue  # binary
                    if is_os_open(f):
                        continue  # os.open returns a file descriptor: no text, no encoding
                    if (
                        isinstance(f, ast.Attribute)
                        and f.attr == "open"
                        and not isinstance(f.value, ast.Name | ast.Call | ast.Attribute)
                    ):
                        continue
                if any(k.arg == "encoding" for k in node.keywords):
                    continue
                positional = {"read_text": 1, "write_text": 2, "open": 4}[name]
                if len(node.args) >= positional:
                    continue  # encoding passed positionally, e.g. read_text("utf-8")
                found.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}()")
    return found


def test_every_text_io_call_names_its_encoding():
    assert offenders() == []
