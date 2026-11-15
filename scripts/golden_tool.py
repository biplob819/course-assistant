#!/usr/bin/env python3
"""Build, split and check the golden set (E2.1). Holds no case text itself.

    python scripts/golden_tool.py build     # from the private source in golden/heldout/src/
    python scripts/golden_tool.py check     # validate the case files and the coverage rule
    python scripts/golden_tool.py verify    # the private held-out file matches its manifest

The tuning split is public (golden/public/cases.jsonl). The held-out split
(golden/heldout/cases.jsonl) and its authoring source are git-ignored; MANIFEST.json, which
lists the held-out ids and line hashes, is committed so anyone can confirm it is unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from course_assistant.schemas import GoldenCase

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "golden" / "public" / "cases.jsonl"
HELDOUT = ROOT / "golden" / "heldout" / "cases.jsonl"
MANIFEST = ROOT / "golden" / "heldout" / "MANIFEST.json"
SRC = ROOT / "golden" / "heldout" / "src"
SEED = 20261115
COMPOSITION = {"B1": 150, "B2": 30, "B7": 10, "B3": 30, "B4": 20, "B8": 10, "B9": 10, "B10": 20}


def corpus_version() -> str:
    m = yaml.safe_load((ROOT / "sample_course" / "manifest.yaml").read_text(encoding="utf-8"))
    joined = "".join(sorted(e["sha256"] for e in m["entries"]))
    return hashlib.sha256(joined.encode()).hexdigest()


def locators() -> set[str]:
    out = set()
    for f in sorted((ROOT / "sample_course" / "src").glob("s[0-9][0-9].yaml")):
        s = yaml.safe_load(f.read_text(encoding="utf-8"))
        sid = s["session"]
        out |= {f"{sid}:slides:{i}" for i in range(1, len(s["slides"]) + 1)}
        out |= {f"{sid}:pre_read:{i}" for i in range(1, len(s["pre_read"]["pages"]) + 1)}
        out.add(f"{sid}:agenda:1")
    return out


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SRC / f"{name}.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"private source {name}.py not found in {SRC}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _turns(prior):
    return [list(t) for t in prior] if prior else []


def build_cases() -> list[dict]:
    """Every case as a dict, with `group` (source probe id) for split bookkeeping."""
    b1, other = _load("cases_b1"), _load("cases_other")
    cv = corpus_version()
    cases: list[dict] = []

    def add(rule, question, status, group, **kw):
        n = sum(c["rule"] == rule for c in cases) + 1
        cases.append({"schema_version": "1.0", "id": f"ta-{rule.lower()}-{n:03d}", "rule": rule,
                      "question": question, "expected_status": status, "corpus_version": cv,
                      "group": group, **kw})  # fmt: skip

    for sid, rows in (("s01", b1.S1), ("s02", b1.S2), ("s03", b1.S3)):
        for row in rows:
            q, locs = row[0], list(row[1])
            prior = row[2] if len(row) > 2 else None
            # grouped by topic (the slide or page it is about), so questions on one unit, and
            # follow-ups that repeat them, never sit on both sides of the split (D-34)
            add("B1", q, "answered", f"topic:{locs[0]}", gold_units=locs, session_scope=None,
                prior_turns=_turns(prior), _session=sid)  # fmt: skip
    for i, (q, scope) in enumerate(other.B2):
        add("B2", q, "not_covered", f"b2-{i}", session_scope=scope)
    for i, q in enumerate(other.B7):
        add("B7", q, "not_covered", f"b7-{i}")
    probes = [(a, q, None) for a, q in other.B3_SINGLE] + [(a, q, p) for a, p, q in other.B3_MULTI]
    for aid, q, prior in probes:
        add("B3", q, "hint_only", f"assess:{aid}", assessment_id=aid, prior_turns=_turns(prior))
    for i, q in enumerate(other.B4):
        add("B4", q, "handed_over", f"b4-{i}")
    for i, (q, canary) in enumerate(other.B8):
        add("B8", q, "not_covered", f"b8-{i}", canary=canary)
    for i, q in enumerate(other.B9):
        add("B9", q, "not_covered", f"b9-{i}")
    for i, (q, canary, status, prior) in enumerate(other.B10):
        extra = {"assessment_id": other.B10_ASSESSMENT[i]} if i in other.B10_ASSESSMENT else {}
        topic = other.B10_TOPIC.get(i)
        group = (
            f"topic:{topic}" if topic and not topic.startswith("assess:") else (topic or f"b10-{i}")
        )
        add("B10", q, status, group, canary=canary, prior_turns=_turns(prior), **extra)
    # extension (D-19, D-31): three paraphrases of each B3 and B10 probe, same group
    for rule, para in (("B3", other.B3_PARA), ("B10", other.B10_PARA)):
        sources = [c for c in cases if c["rule"] == rule and not c.get("_ext")]
        assert len(para) == len(sources), (rule, len(para), len(sources))
        for src, variants in zip(sources, para, strict=True):
            assert len(variants) == 3, src["id"]
            for v in variants:
                n = sum(c["rule"] == rule for c in cases) + 1
                cases.append({**src, "id": f"ta-{rule.lower()}-{n:03d}", "question": v,
                              "_ext": True})  # fmt: skip
    return cases


def _merge_shared_text(cases: list[dict]) -> None:
    """Union-find over groups: two cases that share any question or prior-turn text (a
    follow-up whose earlier turn is another case's question, with its answer) join one group,
    so they land on the same side (D-34)."""
    parent: dict[str, str] = {}

    def find(g: str) -> str:
        while parent.setdefault(g, g) != g:
            parent[g] = parent[parent[g]]
            g = parent[g]
        return g

    owner: dict[str, str] = {}
    for c in cases:
        texts = [c["question"], *(turn[0] for turn in c.get("prior_turns") or [])]
        for text in texts:
            key = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
            if key in owner:
                a, b = find(owner[key]), find(c["group"])
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                owner[key] = c["group"]
    for c in cases:
        c["group"] = find(c["group"])


def split(cases: list[dict]) -> None:
    """Split by group, never by case (D-32, D-34). A group is a topic (the unit a question is
    about, shared by B1 questions, their follow-ups and B10 carriers on that topic), an
    assessment (all B3 probes for it and their paraphrases), or a single probe. Groups are
    assigned half and half within each stratum: topics per session, assessments, and each
    remaining rule; a group seen in several strata takes the side of the first."""
    rng = random.Random(SEED)
    _merge_shared_text(cases)
    strata: dict[str, list[str]] = defaultdict(list)
    for c in cases:
        g = c["group"]
        key = g.split(":")[1].split(":")[0] if g.startswith("topic:") else c["rule"]
        if g not in {x for v in strata.values() for x in v}:
            strata[key].append(g)
    side: dict[str, str] = {}
    for key in sorted(strata):
        groups = sorted(strata[key])
        rng.shuffle(groups)
        half = len(groups) // 2
        for i, g in enumerate(groups):
            side[g] = "tuning" if i < half else "heldout"
    for c in cases:
        c["split"] = side[c["group"]]


def to_line(c: dict) -> str:
    clean = {k: v for k, v in c.items() if k not in ("group", "_session", "_ext")}
    GoldenCase.model_validate_json(json.dumps(clean))  # refuses an invalid case
    return json.dumps(clean, sort_keys=True, ensure_ascii=False)


def write(cases: list[dict]) -> None:
    pub = [to_line(c) for c in cases if c["split"] == "tuning"]
    held = [to_line(c) for c in cases if c["split"] == "heldout"]
    PUBLIC.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC.write_text("\n".join(pub) + "\n", encoding="utf-8")
    HELDOUT.write_text("\n".join(held) + "\n", encoding="utf-8")
    MANIFEST.write_text(json.dumps(manifest_for(held), indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")  # fmt: skip


def manifest_for(lines: list[str]) -> dict:
    counts = Counter(json.loads(x)["rule"] for x in lines)
    return {
        "corpus_version": corpus_version(),
        "count": len(lines),
        "counts_by_rule": dict(sorted(counts.items())),
        "cases": {json.loads(x)["id"]: hashlib.sha256(x.encode()).hexdigest() for x in lines},
        "file_sha256": hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest(),
    }


def read(path: Path) -> list[GoldenCase]:
    if not path.is_file():
        return []
    return [
        GoldenCase.model_validate_json(x)
        for x in path.read_text(encoding="utf-8").splitlines()
        if x
    ]


def problems(cases: list[GoldenCase]) -> list[str]:
    found = []
    known = locators()
    for c in cases:
        for loc in c.gold_units:
            if loc not in known:
                found.append(f"{c.id}: unknown locator {loc}")
    briefs = yaml.safe_load(
        (ROOT / "sample_course" / "assessments.yaml").read_text(encoding="utf-8")
    )
    assessments = {a["assessment_id"] for a in briefs["assessments"]}
    for c in cases:
        if c.assessment_id and c.assessment_id not in assessments:
            found.append(f"{c.id}: unknown assessment {c.assessment_id}")
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        found.append("duplicate case ids")
    return found


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def leaks(public: list[GoldenCase], heldout: list[GoldenCase]) -> list[str]:
    """No held-out question may appear in the public split, as a question or a prior turn,
    and no public and held-out B1 case may share a topic unit (D-34)."""
    found = []
    pub_texts = {_norm(c.question) for c in public} | {
        _norm(q) for c in public for q, _ in c.prior_turns}  # fmt: skip
    for c in heldout:
        texts = [c.question, *(q for q, _ in c.prior_turns)]
        if any(_norm(x) in pub_texts for x in texts):
            found.append(f"leak: held-out {c.id} text appears in the public split")
    pub_topics = {c.gold_units[0] for c in public if c.rule == "B1"}
    for c in heldout:
        if c.rule == "B1" and c.gold_units[0] in pub_topics:
            found.append(f"leak: held-out {c.id} shares topic {c.gold_units[0]} with public")
    return found


def coverage(cases: list[GoldenCase]) -> list[str]:
    found = []
    b1 = Counter(c.gold_units[0].split(":")[0] for c in cases if c.rule == "B1")
    for sid in ("s01", "s02", "s03"):
        if b1[sid] < 15:
            found.append(f"coverage: {sid} has {b1[sid]} B1 cases (needs 15)")
    b3 = Counter(c.assessment_id for c in cases if c.rule == "B3")
    for aid, n in sorted(b3.items()):
        if n < 3:
            found.append(f"coverage: {aid} has {n} B3 probes (needs 3)")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("command", choices=["build", "check", "verify"])
    args = ap.parse_args(argv)
    if args.command == "build":
        cases = build_cases()
        split(cases)
        write(cases)
        print(f"built {len(cases)} cases: {sum(c['split'] == 'tuning' for c in cases)} tuning")
        return 0
    if args.command == "verify":
        if not HELDOUT.is_file():
            print("held-out file not present (only the owner has it); manifest not checked")
            return 0
        lines = [x for x in HELDOUT.read_text(encoding="utf-8").splitlines() if x]
        ok = manifest_for(lines) == json.loads(MANIFEST.read_text(encoding="utf-8"))
        print("held-out matches its manifest" if ok else "held-out does NOT match its manifest")
        return 0 if ok else 1
    pub, held = read(PUBLIC), read(HELDOUT)
    cases = pub + held
    found = problems(cases) + (coverage(cases) + leaks(pub, held) if held else [])
    for f in found:
        print(f)
    print(f"{len(cases)} cases checked, {len(found)} problems")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
