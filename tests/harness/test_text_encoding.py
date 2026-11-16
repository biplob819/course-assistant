"""Every text file read or write names its encoding (L-32). Windows defaults to cp1252, so
an implicit encoding passes on macOS and Linux and breaks on Windows CI or for Windows users."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CALLS = {"read_text", "write_text", "open"}


def is_os_open(f: ast.expr) -> bool:
    return (isinstance(f, ast.Attribute) and f.attr == "open"
            and isinstance(f.value, ast.Name) and f.value.id == "os")  # fmt: skip


def open_mode(node: ast.Call) -> ast.expr | None:
    """The mode argument: open(path, mode) takes it second, path.open(mode) first."""
    keyword = next((k.value for k in node.keywords if k.arg == "mode"), None)
    if keyword is not None:
        return keyword
    index = 0 if isinstance(node.func, ast.Attribute) and not is_os_open(node.func) else 1
    return node.args[index] if len(node.args) > index else None


def test_binary_modes_are_read_from_the_right_argument():
    cases = {"open(p, 'rb')": "rb", "p.open('ab+')": "ab+", "p.open(mode='wb')": "wb",
             "open(p)": None, "p.open()": None}  # fmt: skip
    for src, want in cases.items():
        call = ast.parse(src).body[0].value  # type: ignore[attr-defined]
        got = open_mode(call)
        assert (got.value if isinstance(got, ast.Constant) else got) == want, src


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
                    mode = open_mode(node)
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
