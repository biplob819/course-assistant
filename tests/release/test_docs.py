"""User docs, privacy statement, example gate Action and release notes (R3.1 OSS-04, OSS-06,
OSS-07, OSS-08 drafts).

Offline: the docs and the example workflow are read as data. Every command a guide tells a
reader to run must exist (a `course-assistant` subcommand and its flags, a Make target or a
script in `scripts/`), so a guide cannot drift from the CLI unnoticed.
"""

from __future__ import annotations

import argparse
import re
import shlex
import tomllib

import yaml

from course_assistant.cli import build_parser
from harness_lib import ROOT

GUIDES = ROOT / "docs" / "guide"
GUIDE_FILES = ("quickstart.md", "own-course.md", "evaluate.md", "cost-and-privacy.md")
EXAMPLE = ROOT / "docs" / "examples" / "course-ta-gate.yml"
SHA_PIN = re.compile(r"uses: [\w./-]+@[0-9a-f]{40}  # v\d")
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
TELEMETRY = ("sentry", "posthog", "segment", "mixpanel", "amplitude", "opentelemetry",
             "datadog", "newrelic", "rollbar", "bugsnag", "honeycomb")  # fmt: skip


def _subcommands() -> dict[str, set[str]]:
    ap = build_parser()
    out: dict[str, set[str]] = {"": {o for a in ap._actions for o in a.option_strings}}
    for action in ap._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                out[name] = {o for a in sub._actions for o in a.option_strings}
                for nested in sub._actions:  # `calibrate export` and the like
                    if isinstance(nested, argparse._SubParsersAction):
                        for step, leaf in nested.choices.items():
                            out[f"{name} {step}"] = {o for a in leaf._actions
                                                     for o in a.option_strings}  # fmt: skip
    return out


def _make_targets() -> set[str]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Za-z][\w-]*):", text, re.M))


def _command_lines(text: str) -> list[str]:
    lines: list[str] = []
    for block in FENCE.findall(text):
        for raw in block.splitlines():
            line = raw.split(" #", 1)[0].strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def command_problems(lines: list[str]) -> list[str]:
    """Each command that names a missing subcommand, flag, Make target or script."""
    subs, targets = _subcommands(), _make_targets()
    problems: list[str] = []
    for line in lines:
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        for i, tok in enumerate(tokens):
            if tok == "course-assistant" and (i == 0 or tokens[i - 1] in ("run", "exec")):
                rest = tokens[i + 1 :]
                cmd = rest[0] if rest and not rest[0].startswith("-") else ""
                if len(rest) > 1 and f"{cmd} {rest[1]}" in subs:
                    cmd = f"{cmd} {rest[1]}"
                if cmd not in subs:
                    problems.append(f"no subcommand {cmd!r}: {line}")
                    continue
                for flag in (t.split("=", 1)[0] for t in rest if t.startswith("--")):
                    if flag not in subs[cmd] | subs[""]:
                        problems.append(f"no flag {flag} for {cmd or 'course-assistant'}: {line}")
            elif tok == "make" and i == 0:
                target = next((t for t in tokens[1:] if "=" not in t), None)
                if target is not None and target not in targets:
                    problems.append(f"no make target {target!r}: {line}")
            elif re.fullmatch(r"scripts/[\w-]+\.py", tok) and not (ROOT / tok).is_file():
                problems.append(f"no script {tok}: {line}")
    return problems


def _guide(name: str) -> str:
    return (GUIDES / name).read_text(encoding="utf-8")


# ---------- OSS-04 docs ----------


def test_command_checker_catches_drift():
    bad = ["uv run course-assistant ask --x", "make deploy-everything", "python scripts/nope.py",
           "course-assistant doctor --nope"]  # fmt: skip
    assert len(command_problems(bad)) == 4
    assert command_problems(["course-assistant calibrate export r --nope"]) != []
    good = ["uv run course-assistant doctor --config c.yaml", "course-assistant --version",
            "course-assistant calibrate score r --heldout h.jsonl --labeller owner",
            "make eval-replay", "uv run python scripts/release_gate.py offline"]  # fmt: skip
    assert command_problems(good) == []


def test_every_guide_exists():
    for name in GUIDE_FILES:
        assert (GUIDES / name).is_file(), name


def test_every_command_in_the_guides_exists():
    lines = [ln for name in GUIDE_FILES for ln in _command_lines(_guide(name))]
    assert len(lines) >= 15, "the guides should show the commands they describe"
    assert command_problems(lines) == []


def test_quickstart_runs_the_sample_course_offline_then_with_a_key():
    text = _guide("quickstart.md")
    for needed in ("course-assistant eval-replay", "course-assistant init",
                   "course-assistant doctor", "course-assistant ingest", "OPENAI_API_KEY",
                   "10 minutes"):  # fmt: skip
        assert needed in text, needed


def test_own_course_guide_uses_init_doctor_and_a_dry_run():
    text = _guide("own-course.md")
    for needed in ("course-assistant init", "course-assistant doctor", "--dry-run"):
        assert needed in text, needed


def test_evaluate_guide_covers_golden_cases_and_the_gate():
    text = _guide("evaluate.md")
    for needed in ("golden_tool.py check", "release_gate.py", "held-out", "public"):
        assert needed in text, needed


def test_cost_and_privacy_guide_links_the_privacy_statement():
    text = _guide("cost-and-privacy.md")
    assert "PRIVACY.md" in text
    assert "$" in text, "the cost guide gives figures"


def test_guides_never_show_a_key_value():
    for name in GUIDE_FILES:
        text = _guide(name)
        assert not re.search(r"sk-[A-Za-z0-9_-]{8,}", text), name
        assert not re.search(r"OPENAI_API_KEY=\S", text), name


# ---------- OSS-06 privacy ----------


def test_privacy_statement_says_no_telemetry_and_names_the_destination():
    text = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    assert "no telemetry" in text.lower()
    assert "OpenAI project you configure" in text


def test_no_telemetry_dependency_in_the_package():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = list(meta["dependencies"])
    deps += [d for group in meta["optional-dependencies"].values() for d in group]
    names = [re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0].lower() for d in deps]
    assert not [n for n in names if any(t in n for t in TELEMETRY)]


def test_no_telemetry_import_in_the_package():
    src = ROOT / "src" / "course_assistant"
    pattern = re.compile(r"^\s*(?:import|from)\s+(" + "|".join(TELEMETRY) + r")\b", re.M)
    hits = [str(p.relative_to(ROOT)) for p in sorted(src.rglob("*.py"))
            if pattern.search(p.read_text(encoding="utf-8"))]  # fmt: skip
    assert hits == []


def test_streamlit_usage_statistics_are_off():
    text = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["browser"]["gatherUsageStats"] is False


# ---------- OSS-07 example gate Action ----------


def _example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_example_action_is_a_doc_not_an_active_workflow():
    assert EXAMPLE.is_file()
    assert not (ROOT / ".github" / "workflows" / EXAMPLE.name).exists()


def test_example_action_runs_the_gate_and_fails_on_a_failed_threshold():
    wf = _example()
    assert wf["permissions"] == {"contents": "read"}
    steps = [s for job in wf["jobs"].values() for s in job["steps"]]
    gate = [s for s in steps if "scripts/release_gate.py" in str(s.get("run", ""))]
    assert gate, "a step runs the release gate"
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "continue-on-error" not in text, "a failed gate must fail the job"
    assert "|| true" not in text and "|| exit 0" not in text
    for s in gate:
        assert "make " not in s["run"], "call the script: make turns every exit code into 2"


def test_example_action_actions_are_pinned_and_drop_credentials():
    text = EXAMPLE.read_text(encoding="utf-8")
    uses = [ln.strip().removeprefix("- ") for ln in text.splitlines() if "uses:" in ln]
    assert uses
    for u in uses:
        assert SHA_PIN.match(u), u
    wf = _example()
    for job in wf["jobs"].values():
        for s in job["steps"]:
            if str(s.get("uses", "")).startswith("actions/checkout@"):
                assert s["with"]["persist-credentials"] is False


def test_example_action_keeps_secrets_off_pull_requests():
    wf = _example()
    for name, job in wf["jobs"].items():
        if "secrets." in yaml.safe_dump(job):
            cond = str(job.get("if", ""))
            assert "pull_request" not in cond and "refs/heads/main" in cond, name


def test_example_action_commands_exist():
    wf = _example()
    lines = [ln.strip() for job in wf["jobs"].values() for s in job["steps"]
             for ln in str(s.get("run", "")).splitlines() if ln.strip()]  # fmt: skip
    assert command_problems(lines) == []


# ---------- OSS-08 drafts: release notes and semver ----------


def test_changelog_has_a_draft_0_1_0_section():
    text = (ROOT / "changelog.md").read_text(encoding="utf-8")
    assert re.search(r"^## 0\.1\.0\b", text, re.M)
    assert "[R3.1]" in text


def test_semver_policy_is_written_down():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    section = text.split("## Versioning", 1)
    assert len(section) == 2, "CONTRIBUTING.md has a Versioning section"
    body = section[1].split("\n## ", 1)[0].lower()
    assert "semantic versioning" in body
    assert "0.x" in body and "patch" in body


def test_doc_paths_are_relative_to_the_repo():
    # the guides are linked from the README by absolute URL; inside them, relative links work
    for name in GUIDE_FILES:
        for target in re.findall(r"\]\(([^)#\s]+)", _guide(name)):
            if "://" in target:
                continue
            assert (GUIDES / target).resolve().exists(), f"{name}: {target}"
