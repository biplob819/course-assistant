"""E2.5 quality round (OWNER D-71), eval side, offline: a cheap subset run of the public split.

Tuning on the public half starts with a subset (the B1 cases, where grounded and locator are
measured) before all 214 cases. A subset is never allowed on a held-out run: the held-out set is
scored once and whole. Tests never read golden/heldout.
"""

from __future__ import annotations

import pytest

import eval_live


def test_a_rule_subset_of_the_public_split_keeps_only_those_rules():
    cases = eval_live.select("public")
    b1 = eval_live.subset(cases, "public", "B1")
    assert b1 and all(c.rule == "B1" for c in b1)
    assert len(b1) == sum(c.rule == "B1" for c in cases)
    both = eval_live.subset(cases, "public", "B1,B7")
    assert {c.rule for c in both} == {"B1", "B7"}


def test_no_rule_filter_keeps_every_case():
    cases = eval_live.select("public")
    assert eval_live.subset(cases, "public", None) == cases


@pytest.mark.parametrize("split", ["heldout", "both"])
def test_a_subset_is_refused_on_a_held_out_run(split):
    with pytest.raises(ValueError, match="public split only"):
        eval_live.subset([], split, "B1")


def test_an_unknown_rule_is_refused():
    with pytest.raises(ValueError, match="unknown rule"):
        eval_live.subset(eval_live.select("public"), "public", "B1,B99")


def test_eval_live_refuses_a_held_out_subset_before_any_call(capsys):
    code = eval_live.main(["--config", "x.yaml", "--split", "heldout", "--rules", "B1",
                           "--dry-run"])  # fmt: skip
    assert code == 2 and "public split only" in capsys.readouterr().err


def test_evalkit_args_pass_live_judges_only_when_asked(tmp_path):
    from pathlib import Path

    base = eval_live.evalkit_args(tmp_path, Path("task.yaml"), 74, 5.0, live_judges=False)
    assert base[:2] == ["run", str(tmp_path / "evalkit.yaml")] and "--live-judges" not in base
    assert base[base.index("--n") + 1] == "74" and base[base.index("--max-cost") + 1] == "5.0000"
    live = eval_live.evalkit_args(tmp_path, Path("task.yaml"), 74, 5.0, live_judges=True)
    assert live == [*base, "--live-judges"]
