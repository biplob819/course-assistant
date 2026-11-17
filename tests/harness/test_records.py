import re
from pathlib import Path

import pytest

import status
from harness_lib import ROOT

WORKFLOWS = ROOT / ".github" / "workflows"


def test_decisions_seeded_d1_to_d22():
    text = (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")
    ids = {int(n) for n in re.findall(r"^\| (?:OWNER )?D-(\d+) \|", text, re.M)}
    assert set(range(1, 23)) <= ids


def test_lessons_and_changelog_exist():
    lessons = (ROOT / "docs" / "lessons.md").read_text(encoding="utf-8")
    ids = {int(n) for n in re.findall(r"^\| L-(\d+) \|", lessons, re.M)}
    assert set(range(1, 16)) <= ids
    assert "[W0.1]" in (ROOT / "changelog.md").read_text(encoding="utf-8")


def test_status_prints_graph_and_node(capsys):
    assert status.main([]) == 0
    out = capsys.readouterr().out
    assert "W0.1" in out and "P4.3" in out and "capabilities green" in out
    assert status.main(["W0.1"]) == 0
    assert "HAR-07" in capsys.readouterr().out
    assert status.main(["Z9.9"]) == 1


def _yaml_code(path: Path) -> str:
    """Workflow text with comment-only lines and trailing comments removed."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.split(" #", 1)[0].rstrip()
        if code.strip() and not code.lstrip().startswith("#"):
            lines.append(code)
    return "\n".join(lines)


@pytest.mark.contract
def test_ci_runs_on_every_push_and_pr_with_read_only_token():
    ci = _yaml_code(WORKFLOWS / "ci.yml")
    assert re.search(r"^on:\n  push:\n  pull_request:", ci, re.M)  # every branch, not only main
    assert re.search(r"^permissions:\n  contents: read$", ci, re.M)


def test_ci_workflow_covers_gates_and_matrix():
    ci = _yaml_code(WORKFLOWS / "ci.yml")
    assert re.search(r"^on:\n  push:", ci, re.M)
    for step in (
        "ruff check",
        "pyright",
        "pytest",
        "uv build",
        "secret_scan.py",
        "corpus_guard.py",
        "node_converge.py --self-test",
        "node_start.py --self-test",
        "verify_graph_eligibility.py --self-test",
        "secret_scan.py --self-test",
        "corpus_guard.py --self-test",
    ):
        assert step in ci
    assert "uv sync --locked --all-extras" in ci  # optional extras (L-20)
    assert "core-only:" in ci
    for v in ('"3.11"', '"3.12"', '"3.13"'):
        assert v in ci
    for os_name in ("ubuntu-latest", "macos-latest"):
        assert os_name in ci
    assert "pull_request" in ci


def test_portfolio_notes_stay_out_of_public_repo():
    ignore = Path(ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in ("Portfolio notes.md", ".private-terms", "corpus/", "golden/heldout/*"):
        assert entry in ignore.splitlines()


@pytest.mark.contract
def test_workflows_grant_least_privilege():
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["ci.yml", "eval-gate.yml"]
    for name in ("ci.yml", "eval-gate.yml"):
        code = _yaml_code(WORKFLOWS / name)
        assert re.search(r"^permissions:\n  contents: read$", code, re.M), name
    assert "secrets." not in _yaml_code(WORKFLOWS / "ci.yml")
    # E2.4: the release gate's secrets are read only by its live job, never by the offline one
    gate = _yaml_code(WORKFLOWS / "eval-gate.yml")
    offline, live = gate.split("\n  live:\n", 1)
    assert "secrets." not in offline
    assert set(re.findall(r"\{\{ secrets\.(\w+)", live)) == {"OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                                                        "HELDOUT_BUNDLE"}  # fmt: skip


def test_secrets_never_reach_pull_request_code():
    """E2.4 verifier pass 2: the live job runs only on a push to main, the schedule or by
    hand, with secrets from the protected `release` environment."""
    import yaml

    live = yaml.safe_load((WORKFLOWS / "eval-gate.yml").read_text(encoding="utf-8"))["jobs"]["live"]
    cond = " ".join(str(live["if"]).split())
    assert "pull_request" not in cond and "pull_request_target" not in cond
    assert cond == (
        "github.ref == 'refs/heads/main' && (github.event_name == 'push' || "
        "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')"
    )
    assert live["environment"] == "release"


def test_every_action_is_pinned_and_checkout_drops_credentials():
    import yaml

    for wf in sorted(WORKFLOWS.iterdir()):
        text = wf.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "uses:" in line:
                assert re.search(r"uses: [\w./-]+@[0-9a-f]{40}  # v\d", line), (wf.name, line)
        for job in yaml.safe_load(text)["jobs"].values():
            for step in job["steps"]:
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False, (wf.name, step)


def test_catalog_round_trip_is_byte_identical(tmp_path):
    from harness_lib import CATALOG_TSV, load_catalog, write_catalog

    out = tmp_path / "catalog.tsv"
    write_catalog(load_catalog(), out)
    assert out.read_bytes() == CATALOG_TSV.read_bytes()


def test_status_output_is_deterministic(capsys):
    status.main([])
    first = capsys.readouterr().out
    status.main([])
    assert capsys.readouterr().out == first


def test_gitattributes_force_lf():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in text
