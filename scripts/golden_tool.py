#!/usr/bin/env python3
"""Build, split and check the golden set (E2.1). Holds no case text itself.

    python scripts/golden_tool.py build     # from the private source in golden/heldout/src/
    python scripts/golden_tool.py check     # validate the case files and the coverage rule
    python scripts/golden_tool.py verify    # the private held-out file matches its manifest
    python scripts/golden_tool.py refresh   # retire the used held-out set, write the fresh one

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
    absent = ROOT / "sample_course" / "absent_topics.yaml"
    topics = yaml.safe_load(absent.read_text(encoding="utf-8"))
    for i, (q, scope) in enumerate(other.B2):
        hit = [n for n, tp in enumerate(topics["topics"])
               if any(k in q.lower() for k in tp["keywords"])]  # fmt: skip
        add("B2", q, "not_covered", f"absent:{hit[0]}" if hit else f"b2-{i}", session_scope=scope)
    for i, q in enumerate(other.B7):
        add("B7", q, "not_covered", f"b7-{i}")
    probes = [(a, q, None) for a, q in other.B3_SINGLE] + [(a, q, p) for a, p, q in other.B3_MULTI]
    for aid, q, prior in probes:
        add("B3", q, "hint_only", f"assess:{aid}", assessment_id=aid, prior_turns=_turns(prior))
    for i, q in enumerate(other.B4):
        add("B4", q, "handed_over", f"b4:{other.B4_THEME[i]}")
    for i, (q, canary) in enumerate(other.B8):
        add("B8", q, "not_covered", f"b8:{other.B8_THEME[i]}", canary=canary)
    for i, q in enumerate(other.B9):
        add("B9", q, "not_covered", f"b9:{other.B9_THEME[i]}")
    for i, (q, canary, status, prior) in enumerate(other.B10):
        extra = {"assessment_id": other.B10_ASSESSMENT[i]} if i in other.B10_ASSESSMENT else {}
        topic = other.B10_TOPIC.get(i)
        group = f"topic:{topic}" if topic and topic.startswith("s0") else (topic or f"b10-{i}")
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


# Units that teach the same idea, so questions on either belong together (D-34)
CONCEPT_LINKS = [
    ("s01:slides:4", "s01:slides:5", "s01:pre_read:2"),  # when AI fits / the fit check
    ("s01:slides:10", "s01:slides:11", "s01:pre_read:3"),  # metrics and the product brief
    ("s02:slides:8", "s02:pre_read:5"),  # judge calibration
    ("s02:slides:9", "s02:pre_read:3"),  # offline and online evaluation
    ("s02:slides:3", "s02:pre_read:1", "s02:pre_read:2"),  # golden sets and good cases
    ("s03:slides:3", "s03:slides:8", "s03:pre_read:1"),  # risks and the risk register
    ("s03:slides:4", "s03:slides:5", "s03:slides:6", "s03:slides:7"),  # the four layers
    ("s03:slides:5", "s03:pre_read:2"),  # grounding and citations
    ("s03:slides:7", "s03:pre_read:3"),  # handover
    ("s03:pre_read:5", "s03:slides:4"),  # one number per trust layer (pass 5)
    ("s03:slides:18", "s03:pre_read:6"),  # incidents
    ("s03:slides:16", "s03:slides:10", "s03:slides:14"),  # privacy by design, retention, pids
    ("s03:slides:12", "s03:pre_read:4"),  # data protection roles
    ("s03:slides:15", "s03:pre_read:2"),  # transparency and citations
    ("s01:slides:3", "s01:pre_read:1"),  # start from the problem
    ("s01:slides:6", "s01:slides:14"),  # the deterministic test and its worked example
    ("s01:slides:5", "s01:slides:9"),  # poor fit and who reviews outputs
]
# an assignment slide describes that session's first assessment (D-35)
ASSIGNMENT_SLIDES = {
    "s01:slides:21": "assess:s01-a1",
    "s01:pre_read:3": "assess:s01-a1",  # the pre-read page that lays out the brief (pass 4)
    "s02:slides:19": "assess:s02-a1",
    "s03:slides:20": "assess:s03-a1",
}
LABEL = re.compile(
    r"Session (\d) (slides|pre-read), (?:slides? |p\. )(\d+)(?: to (\d+))?", re.IGNORECASE
)


def _cited(text: str) -> list[str]:
    """Locators named by citation labels inside an earlier assistant turn."""
    out = []
    for m in LABEL.finditer(text):
        kind = "slides" if m[2].lower() == "slides" else "pre_read"
        first, last = int(m[3]), int(m[4] or m[3])
        out += [f"s0{m[1]}:{kind}:{n}" for n in range(first, last + 1)]
    return out


def _merge_shared_text(cases: list[dict]) -> None:
    """Union-find over groups (D-34). Cases join one group when they share any question or
    prior-turn text, any gold unit, any unit cited in an earlier assistant turn, or units
    linked as one concept; each merged group then lands wholly on one side."""
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

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for link in CONCEPT_LINKS:
        for loc in link[1:]:
            union(f"topic:{link[0]}", f"topic:{loc}")
    for loc, assessment in ASSIGNMENT_SLIDES.items():
        union(f"topic:{loc}", assessment)
    for c in cases:
        units = list(c.get("gold_units") or [])
        for turn in c.get("prior_turns") or []:
            units += _cited(turn[1])
        for loc in units:
            union(c["group"], f"topic:{loc}")
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
    # largest groups first, each to the side with fewer cases so far in its stratum (ties by
    # a seeded order), so merged groups of uneven size still give near-even halves
    size = Counter(c["group"] for c in cases)
    side: dict[str, str] = {}
    for key in sorted(strata):
        groups = sorted(strata[key])
        rng.shuffle(groups)
        groups.sort(key=lambda g: -size[g])
        load = {"tuning": 0, "heldout": 0}
        for g in groups:
            if load["tuning"] == load["heldout"]:
                target = "tuning" if rng.random() < 0.5 else "heldout"
            else:
                target = "tuning" if load["tuning"] < load["heldout"] else "heldout"
            side[g] = target
            load[target] += size[g]
    for c in cases:
        c["split"] = side[c["group"]]


def to_line(c: dict) -> str:
    clean = {k: v for k, v in c.items() if k not in ("group", "_session", "_ext")}
    GoldenCase.model_validate_json(json.dumps(clean))  # refuses an invalid case
    return json.dumps(clean, sort_keys=True, ensure_ascii=False)


# held-out cases whose text became public (D-54): left out of the held-out file and manifest,
# never moved to the public split (that would change the tuning groups)
RETIRED = frozenset({"ta-b1-065"})


# OWNER D-71: a held-out set is scored once. A used set is retired as a set (named here with the
# sha256 of its file, kept privately under golden/heldout/retired/<name>/) and a fresh set is
# written from its own private source module, with the same composition, ids from 501 that never
# reuse an earlier id, and text distinct from the public split and from every retired set.
RETIRED_SETS = {
    "heldout-1": "242c81236fa81b9af5dad1a76a056c3b0d3ff38304f1a352ec52b4482c941457",
    # scored once at the D-73 setting (E2.5); retired for heldout-3 (OWNER D-77, D-81)
    "heldout-2": "1c05ae90a41ebe08dcb71251a66f7bfa4bcb9837af6bb1f8fc340e7b28bdea4b",
}
RETIRING = "heldout-2"  # the set `refresh` retires: the live file until the swap
HELDOUT_SOURCE = "cases_heldout3"  # the live held-out set "heldout-3" (private module in SRC)
RETIRED_DIR = ROOT / "golden" / "heldout" / "retired"
FRESH_START = 600  # fresh ids are ta-<rule>-601 and up (heldout-2 used 501 and up)
NEAR = 0.8  # word-set overlap at which two texts of 4+ words count as the same question


def build_fresh(mod) -> list[dict]:
    """The fresh held-out cases of a private source module (see HELDOUT_SOURCE), as dicts with
    their split groups. Refuses a probe without three paraphrases or a lost canary."""
    cv = corpus_version()
    cases: list[dict] = []

    def add(rule, question, status, group, **kw):
        n = FRESH_START + sum(c["rule"] == rule for c in cases) + 1
        cases.append({"schema_version": "1.0", "id": f"ta-{rule.lower()}-{n:03d}", "rule": rule,
                      "question": question, "expected_status": status, "corpus_version": cv,
                      "group": group, "split": "heldout", **kw})  # fmt: skip

    def paraphrases(rule: str, n: int, variants) -> list[str]:
        if len(variants) != 3:
            raise ValueError(f"{rule} probe {n} needs three paraphrases, has {len(variants)}")
        return list(variants)

    for row in mod.B1:
        q, locs, prior = row[0], list(row[1]), row[2]
        add("B1", q, "answered", f"topic:{locs[0]}", gold_units=locs, session_scope=None,
            prior_turns=_turns(prior))  # fmt: skip
    for i, (q, scope) in enumerate(mod.B2):
        add("B2", q, "not_covered", f"fresh-b2-{i}", session_scope=scope)
    for i, q in enumerate(mod.B7):
        add("B7", q, "not_covered", f"fresh-b7-{i}")
    for i, (aid, q, prior, variants) in enumerate(mod.B3):
        for text in [q, *paraphrases("B3", i, variants)]:
            add("B3", text, "hint_only", f"assess:{aid}", assessment_id=aid,
                prior_turns=_turns(prior))  # fmt: skip
    for q, theme in mod.B4:
        add("B4", q, "handed_over", f"b4:fresh-{theme}")
    for q, canary, theme in mod.B8:
        add("B8", q, "not_covered", f"b8:fresh-{theme}", canary=canary)
    for q, theme in mod.B9:
        add("B9", q, "not_covered", f"b9:fresh-{theme}")
    for i, (q, canary, status, prior, topic, aid, variants) in enumerate(mod.B10):
        prior_text = " ".join(t for turn in (prior or []) for t in turn)
        if canary not in q and canary not in prior_text:
            raise ValueError(f"B10 probe {i}: its canary is in neither the question nor a turn")
        texts = [q, *paraphrases("B10", i, variants)]
        if canary in q and any(canary not in t for t in texts):
            raise ValueError(f"B10 probe {i}: a paraphrase lost the canary")
        extra = {"assessment_id": aid} if aid else {}
        group = f"topic:{topic}" if topic else f"fresh-b10-{i}"
        for text in texts:
            add("B10", text, status, group, canary=canary, prior_turns=_turns(prior), **extra)
    return cases


def composition_problems(cases: list[dict], want: dict[str, int]) -> list[str]:
    counts = Counter(c["rule"] for c in cases)
    return [f"{rule}: {counts[rule]} cases, needs {n}" for rule, n in sorted(want.items())
            if counts[rule] != n] + [f"{rule}: not in the set it replaces"
                                     for rule in sorted(set(counts) - set(want))]  # fmt: skip


def _words(text: str) -> frozenset[str]:
    return frozenset(_norm(text).split())


def distinct(fresh: list[GoldenCase], others: list[str]) -> list[str]:
    """Ids of fresh cases whose question or earlier learner turn equals another text, or shares
    at least NEAR of its words with one (texts of 4+ words). Ids only, never text."""
    exact = {_norm(t) for t in others}
    sets = [w for w in (_words(t) for t in others) if len(w) >= 4]
    found = []
    for c in fresh:
        for text in [c.question, *(q for q, _ in c.prior_turns)]:
            w = _words(text)
            near = len(w) >= 4 and any(len(w & o) / len(w | o) >= NEAR for o in sets)
            if _norm(text) in exact or near:
                found.append(c.id)
                break
    return found


def _texts(cases: list[GoldenCase]) -> list[str]:
    return [t for c in cases for t in (c.question, *(q for q, _ in c.prior_turns))]


def _sha(lines: list[str]) -> str:
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


def _lines(path: Path) -> list[str]:
    return [x for x in path.read_text(encoding="utf-8").splitlines() if x]


def refresh(cases: list[dict]) -> list[str]:
    """Retire the live held-out set RETIRING (checked against its hash) and write the fresh set
    from HELDOUT_SOURCE, distinct from the public split and every retired set. A live set that
    is already the fresh one is left as it is. `cases` is the built source, whose public split
    the caller has checked. Returns problems (ids and counts only); nothing is written when
    there is one."""
    del cases
    fresh_dicts = build_fresh(_load(HELDOUT_SOURCE))
    held = [to_line(c) for c in fresh_dicts]
    live = _lines(HELDOUT)
    if live == held:
        return []  # already refreshed
    if _sha(live) != RETIRED_SETS[RETIRING]:
        return [f"the live held-out file is not the set {RETIRING} named in RETIRED_SETS"]
    earlier: list[str] = []
    for name, digest in RETIRED_SETS.items():
        if name == RETIRING:
            continue
        path = RETIRED_DIR / name / "cases.jsonl"
        if not path.is_file() or _sha(_lines(path)) != digest:
            return [f"the retired set {name} is missing or does not match its hash"]
        earlier += _lines(path)
    public = read(PUBLIC)
    retired = [GoldenCase.model_validate_json(x) for x in [*earlier, *live]]
    replaced = [GoldenCase.model_validate_json(x) for x in live]
    want = Counter(c.rule for c in replaced)
    fresh = [GoldenCase.model_validate_json(x) for x in held]
    found = composition_problems(fresh_dicts, dict(want)) + problems(public + fresh)
    found += leaks(public, fresh) + coverage(public + fresh)
    found += [f"not distinct: {i}" for i in distinct(fresh, _texts(public) + _texts(retired))]
    old_ids = {c.id for c in public} | {c.id for c in retired} | set(RETIRED)
    found += [f"id reused: {c.id}" for c in fresh if c.id in old_ids]
    if found:
        return found
    out = RETIRED_DIR / RETIRING
    out.mkdir(parents=True, exist_ok=True)
    (out / "cases.jsonl").write_text("\n".join(live) + "\n", encoding="utf-8")
    (out / "MANIFEST.json").write_text(json.dumps(manifest_for(live), indent=2, sort_keys=True)
                                       + "\n", encoding="utf-8")  # fmt: skip
    HELDOUT.write_text("\n".join(held) + "\n", encoding="utf-8")
    MANIFEST.write_text(json.dumps(manifest_for(held), indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")  # fmt: skip
    return []


def write(cases: list[dict]) -> None:
    pub = [to_line(c) for c in cases if c["split"] == "tuning"]
    held = [to_line(c) for c in cases if c["split"] == "heldout" and c["id"] not in RETIRED]
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

    def units(c: GoldenCase) -> set[str]:
        u = set(c.gold_units)
        for _, answer in c.prior_turns:
            u |= set(_cited(answer))
        return u

    pub_units = set().union(*(units(c) for c in public)) if public else set()
    for c in heldout:
        shared = units(c) & pub_units
        if shared:
            found.append(f"leak: held-out {c.id} shares unit {sorted(shared)[0]} with public")
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
    ap.add_argument("command", choices=["build", "check", "verify", "refresh"])
    args = ap.parse_args(argv)
    if args.command in ("build", "refresh"):
        cases = build_cases()
        split(cases)
        pub = [to_line(c) for c in cases if c["split"] == "tuning"]
        if args.command == "build" and not (SRC / f"{HELDOUT_SOURCE}.py").is_file():
            write(cases)  # no fresh source yet: the original split, as before D-71
        elif pub != PUBLIC.read_text(encoding="utf-8").splitlines():
            print("the source no longer builds the committed public split; nothing written")
            return 1
        else:
            found = refresh(cases)
            for f in found:
                print(f)
            if found:
                print(f"{len(found)} problems; nothing written")
                return 1
            held = read(HELDOUT)
            print(f"held-out refreshed: {len(held)} cases from {HELDOUT_SOURCE}; "
                  f"retired {', '.join(RETIRED_SETS)}")  # fmt: skip
        print(f"built {len(cases)} cases: {len(pub)} tuning")
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
