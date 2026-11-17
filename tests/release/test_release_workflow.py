"""The release workflow and the GitHub templates (R3.1 OSS-01, OSS-05).

The workflow runs only on a version tag, builds and checks the artefacts with the exclusion
test, and publishes by PyPI Trusted Publishing (OIDC) from a protected `pypi` environment,
only for a tag on main. No stored token anywhere. The build never tags or publishes itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from harness_lib import ROOT

GH = ROOT / ".github"
RELEASE = GH / "workflows" / "release.yml"
SHA_PIN = re.compile(r"uses: [\w./-]+@[0-9a-f]{40}  # v\d")


def _wf() -> dict:
    return yaml.safe_load(RELEASE.read_text(encoding="utf-8"))


def _on(wf: dict) -> dict:
    # PyYAML reads the bare key `on` as True
    on = wf.get("on", wf.get(True))
    assert isinstance(on, dict)
    return on


def test_triggers_only_on_a_version_tag():
    on = _on(_wf())
    assert set(on) == {"push"}, "no pull_request, schedule or workflow_dispatch trigger"
    assert set(on["push"]) == {"tags"}, "no branch push trigger"
    assert on["push"]["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]


def test_top_level_token_is_read_only():
    code = RELEASE.read_text(encoding="utf-8")
    assert re.search(r"^permissions:\n  contents: read$", code, re.M)


def test_build_job_builds_and_runs_the_exclusion_test():
    build = _wf()["jobs"]["build"]
    assert "permissions" not in build or build["permissions"] == {"contents": "read"}
    runs = "\n".join(str(s.get("run", "")) for s in build["steps"])
    assert "uv sync --locked --all-extras" in runs
    assert "uv build" in runs
    # the exclusion test runs after the build, so the offline build backend is in the cache
    assert runs.index("uv build") < runs.index("tests/harness/test_packaging.py")
    assert "tests/release" in runs
    assert 'release_check.py --tag "$GITHUB_REF_NAME" --dist dist' in runs
    assert "on_main" in build["outputs"]
    assert "git merge-base --is-ancestor" in runs and "origin/main" in runs
    uploads = [s for s in build["steps"] if str(s.get("uses", "")).startswith("actions/upload-")]
    assert uploads and uploads[0]["with"]["path"] == "dist/"


def test_publish_job_uses_trusted_publishing_from_main_only():
    wf = _wf()
    pub = wf["jobs"]["publish"]
    assert pub["needs"] == "build"
    env = pub["environment"]
    assert (env["name"] if isinstance(env, dict) else env) == "pypi"
    assert pub["permissions"] == {"id-token": "write"}
    cond = " ".join(str(pub["if"]).split())
    assert "startsWith(github.ref, 'refs/tags/v')" in cond
    assert "needs.build.outputs.on_main == 'true'" in cond
    (publish,) = [s for s in pub["steps"] if "pypa/gh-action-pypi-publish" in str(s.get("uses"))]
    assert not set(publish.get("with") or {}) & {"password", "user", "username", "api-token"}
    assert set(wf["jobs"]) == {"build", "publish"}


def test_no_stored_token_anywhere_in_the_workflow():
    text = RELEASE.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert "PYPI_API_TOKEN" not in text and "TWINE_PASSWORD" not in text
    assert "git tag" not in text and "git push" not in text


def test_actions_pinned_and_checkout_drops_credentials():
    text = RELEASE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "uses:" in line:
            assert SHA_PIN.search(line), line
    for job in _wf()["jobs"].values():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False


def test_ci_and_eval_gate_do_not_publish():
    for name in ("ci.yml", "eval-gate.yml"):
        text = (GH / "workflows" / name).read_text(encoding="utf-8")
        assert "gh-action-pypi-publish" not in text and "id-token" not in text, name


# --- issue and PR templates (OSS-05) ---


def test_issue_forms_and_security_contact_link():
    tpl = GH / "ISSUE_TEMPLATE"
    for name in ("bug_report.yml", "feature_request.yml"):
        form = yaml.safe_load((tpl / name).read_text(encoding="utf-8"))
        assert form["name"] and form["description"] and form["body"], name
    cfg = yaml.safe_load((tpl / "config.yml").read_text(encoding="utf-8"))
    assert cfg["blank_issues_enabled"] is False
    security = [c for c in cfg["contact_links"] if "security" in c["name"].lower()]
    assert security and "SECURITY.md" in security[0]["url"]


def test_bug_form_warns_against_pasting_secrets_and_asks_for_doctor():
    text = (GH / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(encoding="utf-8")
    low = text.lower()
    for word in ("key", "passcode", "learner"):
        assert word in low, word
    assert "course-assistant doctor" in text
    assert "SECURITY.md" in text


def test_pr_template_points_to_security_policy():
    text = (GH / "pull_request_template.md").read_text(encoding="utf-8")
    assert "SECURITY.md" in text
    for item in ("ruff", "pyright", "pytest", "changelog"):
        assert item in text, item


def test_community_files_exist():
    for name in ("CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md"):
        assert (ROOT / name).is_file(), name


def test_no_pypi_token_in_the_repo():
    pat = re.compile(r"pypi-AgE[A-Za-z0-9_-]{20,}")
    for path in [*GH.rglob("*"), ROOT / "pyproject.toml"]:
        if isinstance(path, Path) and path.is_file():
            assert not pat.search(path.read_text(encoding="utf-8", errors="ignore")), path
