"""Shared fixtures: the sample course split once, and small throwaway courses in tmp_path."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from course_assistant.ingest import Ledger, Split, plan, units_from_manifest
from course_assistant.schemas import Manifest, load_manifest
from harness_lib import ROOT

SAMPLE_MANIFEST = ROOT / "sample_course" / "manifest.yaml"
CONFIG = {
    "schema_version": "1.0",
    "cohort": "sample",
    "vector_store_id": "vs_test",
    "answer_model": "gpt-6.1-sol",
    "check_model": "gpt-6.1-luna",
    "reasoning_effort": {"gpt-6.1-sol": "medium", "gpt-6.1-luna": "low"},
    "grounding_bar": 0.5,
    "monthly_budget_usd": 50.0,
}


@pytest.fixture(scope="session")
def sample_manifest() -> Manifest:
    return load_manifest(SAMPLE_MANIFEST)


@pytest.fixture(scope="session")
def sample_split(sample_manifest: Manifest) -> Split:
    return units_from_manifest(sample_manifest, ROOT)


@pytest.fixture(scope="session")
def sample_plan(sample_manifest: Manifest):
    return plan(sample_manifest, Ledger.empty(), root=ROOT)


def write_course(root: Path, files: list[tuple[str, str, str, bytes]], *, notes: bool = False,
                 cohort: str = "sample") -> Manifest:  # fmt: skip
    """Write (name, session, kind, bytes) files under root and a manifest with their hashes."""
    entries = []
    for name, session, kind, data in files:
        (root / name).write_bytes(data)
        entries.append({
            "manifest_id": f"m-{name.replace('.', '-').lower()}", "path": name,
            "session": session, "kind": kind, "sha256": hashlib.sha256(data).hexdigest(),
            "licence": "CC-BY-4.0", "approved_by": "owner-1", "approved_at": "2026-11-15",
            "include_speaker_notes": notes,
        })  # fmt: skip
    doc = {"schema_version": "1.0", "cohort": cohort, "entries": entries}
    path = root / "manifest.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return load_manifest(path)


def write_config(directory: Path, **over: object) -> Path:
    path = directory / "config.yaml"
    path.write_text(yaml.safe_dump({**CONFIG, **over}, sort_keys=False), encoding="utf-8")
    return path
