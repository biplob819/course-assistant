"""X1.1: the EvalKit 0.2 contract course-assistant builds on (EKX-01..04)."""

from __future__ import annotations

import inspect
import re
from importlib import metadata
from pathlib import Path

import pytest

evalkit = pytest.importorskip("evalkit")

ROOT = Path(__file__).resolve().parents[2]


def test_the_released_0_2_line_is_installed_and_pinned():
    version = metadata.version("evalkit-harness")
    assert re.fullmatch(r"0\.2\.\d+", version), version
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"evalkit-harness>=0.2,<0.3"' in pyproject


def test_a_system_adapter_receives_the_whole_case():
    from evalkit.adapters.base import SystemAdapter, register_system

    params = list(inspect.signature(SystemAdapter.answer).parameters)
    assert params[:3] == ["self", "case", "request"]
    assert callable(register_system)


def test_cases_carry_meta_for_prior_turns():
    from evalkit.schemas.benchmark import Case

    assert "meta" in Case.model_fields


def test_tasks_name_plugin_scorers_and_slices():
    from evalkit.schemas.config import TaskConfig

    assert {"scorers", "slices"} <= set(TaskConfig.model_fields)


def test_records_and_summaries_carry_task_scores_and_slices():
    from evalkit.schemas.benchmark import Record
    from evalkit.schemas.summary import ModelSummary, RunSummary

    assert "task_scores" in Record.model_fields
    assert "task_scores" in ModelSummary.model_fields
    assert "slices" in RunSummary.model_fields
