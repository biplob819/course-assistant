import tomllib

import pytest

from course_assistant import __version__, cli
from harness_lib import ROOT


def test_cli_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert f"course-assistant {__version__}" in capsys.readouterr().out


@pytest.mark.contract
def test_pyproject_metadata_and_wheel_contents():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["name"] == "course-assistant"
    assert meta["project"]["version"] == __version__
    assert meta["project"]["scripts"]["course-assistant"] == "course_assistant.cli:main"
    assert meta["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/course_assistant"
    ]
    assert meta["project"]["license"] == "MIT" and (ROOT / "LICENSE").is_file()
