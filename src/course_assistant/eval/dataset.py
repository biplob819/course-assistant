"""Golden cases as EvalKit cases (E2.2 EVK-01, D-10).

The context of every case is the corpus: each unit's text in `unit_index` order, so EvalKit's
1-based sentence number is the unit index and its judges read the cited unit's own text. The
label is the expected `Answer.status`. Everything else the system adapter and the task scorers
need (rule, split, earlier turns, gold units, assessment id, canary, corpus versions) rides in
`Case.meta`, from a fixed list of keys: no solution text ever enters a case (D-7).

The written dataset holds course text and may hold held-out cases, so it is only ever written
under a git-ignored run folder (`runs/`), never committed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from course_assistant.ingest.corpus import Corpus
from course_assistant.schemas.config import GoldenCase
from course_assistant.schemas.corpus import Manifest

TASK_TEMPLATE = Path(__file__).with_name("course-ta.yaml")
TASK_FILE = "course-ta.yaml"
DATASET_FILE = "cases.jsonl"
LABELS: tuple[str, ...] = ("answered", "not_covered", "hint_only", "handed_over")
META_KEYS = frozenset({
    "rule", "split", "prior_turns", "session_scope", "gold_units", "gold_indices",
    "assessment_id", "canary", "corpus_version", "golden_corpus_version",
})  # fmt: skip


class DatasetError(ValueError):
    """A golden file or case cannot become an EvalKit case. Messages name ids, never text."""


def manifest_version(manifest: Manifest) -> str:
    """E2.1's corpus version of a golden case: sha256 of the sorted manifest entry hashes
    (the same rule as `scripts/golden_tool.py`)."""
    joined = "".join(sorted(e.sha256 for e in manifest.entries))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def golden_version_for(corpus: Corpus, manifest: Manifest, root: Path) -> str:
    """E2.1's corpus version, but only for the corpus that was actually ingested: the
    manifest's files (sha256-checked by N1.4's splitter) must split into exactly the units of
    `corpus`. A manifest edited, or files changed, after ingest is refused, so cases are never
    matched against a repo manifest that no longer describes the corpus answering them."""
    from course_assistant.ingest import corpus_version, units_from_manifest

    try:
        split = units_from_manifest(manifest, root)
    except Exception:
        raise DatasetError("the manifest's files cannot be split (missing or changed files)"
                           ) from None  # fmt: skip
    if corpus_version(split.units) != corpus.corpus_version:
        raise DatasetError(
            f"the manifest does not describe the ingested corpus {corpus.corpus_version[:12]}: "
            "re-ingest, or check out the manifest the corpus was built from"
        )
    return manifest_version(manifest)


def heldout_ids(manifest_path: Path) -> frozenset[str]:
    """The held-out case ids from the committed `golden/heldout/MANIFEST.json` (ids and
    hashes only). Guards trust these ids, never a case's own `meta.split`."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = data.get("cases")
    if not isinstance(cases, dict) or not cases:
        raise DatasetError("held-out MANIFEST.json has no case ids")
    return frozenset(str(k) for k in cases)


def _lines(path: Path) -> list[str]:
    return [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _parse(lines: Iterable[str], what: str) -> list[GoldenCase]:
    out = []
    for i, line in enumerate(lines, start=1):
        try:
            out.append(GoldenCase.model_validate_json(line))
        except ValueError:
            raise DatasetError(f"{what}: line {i} is not a valid golden case") from None
    return out


def load_public(path: Path) -> list[GoldenCase]:
    """The public (tuning) split."""
    cases = _parse(_lines(path), "public golden file")
    bad = [c.id for c in cases if c.split != "tuning"]
    if bad:
        raise DatasetError(f"public golden file holds non-tuning cases: {bad[:5]}")
    return cases


def load_heldout(directory: Path) -> list[GoldenCase]:
    """The private held-out split, read at run time only, and only if every line and the file
    match the committed MANIFEST.json (E2.1). Errors name ids and counts, never text."""
    manifest = json.loads((directory / "MANIFEST.json").read_text(encoding="utf-8"))
    lines = _lines(directory / "cases.jsonl")
    file_hash = hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()
    if file_hash != manifest.get("file_sha256") or len(lines) != manifest.get("count"):
        raise DatasetError("held-out cases do not match MANIFEST.json (file hash or count)")
    expected: dict[str, str] = manifest.get("cases", {})
    cases = _parse(lines, "held-out golden file")
    for case, line in zip(cases, lines, strict=True):
        if expected.get(case.id) != hashlib.sha256(line.encode("utf-8")).hexdigest():
            raise DatasetError(f"held-out case {case.id} does not match MANIFEST.json")
    bad = [c.id for c in cases if c.split != "heldout"]
    if bad:
        raise DatasetError(f"held-out file holds non-heldout cases: {bad[:5]}")
    return cases


def corpus_context(corpus: Corpus) -> tuple[str, ...]:
    """Every unit's text in unit_index order: sentence n is unit n (D-10)."""
    blank = [u.label for u in corpus.units if not u.text.strip()]
    if blank:
        raise DatasetError(f"{len(blank)} corpus unit(s) have no text, e.g. {blank[0]}")
    if not corpus.units:
        raise DatasetError("the corpus has no units")
    return tuple(u.text for u in corpus.units)


def _gold_indices(case: GoldenCase, corpus: Corpus) -> list[int]:
    out = []
    for locator in case.gold_units:
        try:
            out.append(corpus.resolve(locator).unit_index)
        except Exception:
            raise DatasetError(f"case {case.id}: gold unit {locator} is not in the corpus"
                               ) from None  # fmt: skip
    return out


def to_case(case: GoldenCase, corpus: Corpus, golden_version: str,
            context: tuple[str, ...] | None = None) -> dict[str, Any]:  # fmt: skip
    """One golden case as an EvalKit JSONL row (id, question, context, label, meta)."""
    if case.corpus_version != golden_version:
        raise DatasetError(f"case {case.id} is for another corpus version (retire it, §6.3)")
    meta: dict[str, Any] = {
        "rule": case.rule,
        "split": case.split,
        "prior_turns": [list(t) for t in case.prior_turns],
        "corpus_version": corpus.corpus_version,
        "golden_corpus_version": case.corpus_version,
    }
    if case.session_scope is not None:
        meta["session_scope"] = case.session_scope
    if case.gold_units:
        meta["gold_units"] = list(case.gold_units)
        meta["gold_indices"] = _gold_indices(case, corpus)
    if case.assessment_id is not None:
        meta["assessment_id"] = case.assessment_id
    if case.canary is not None:
        meta["canary"] = case.canary
    if not set(meta) <= META_KEYS:  # pragma: no cover - the keys above are the list
        raise DatasetError(f"case {case.id}: meta keys outside the allow-list")
    return {
        "id": case.id,
        "question": case.question,
        "context": list(context if context is not None else corpus_context(corpus)),
        "label": case.expected_status,
        "meta": meta,
    }


def build_rows(cases: Sequence[GoldenCase], corpus: Corpus,
               golden_version: str) -> list[dict[str, Any]]:  # fmt: skip
    context = corpus_context(corpus)
    ids = [c.id for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise DatasetError(f"duplicate case ids: {dupes[:5]}")
    return [to_case(c, corpus, golden_version, context) for c in cases]


def write_task_dir(out_dir: Path, rows: Sequence[dict[str, Any]]) -> Path:
    """Write `cases.jsonl` and the task YAML beside it; return the YAML path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / DATASET_FILE).write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    task = out_dir / TASK_FILE
    task.write_text(TASK_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return task
