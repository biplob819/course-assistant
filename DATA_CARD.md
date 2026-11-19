# Data card: the course-assistant golden set

This card gives the counts and composition of the golden set used to measure the assistant
(Tech Spec §6). It contains no held-out text. Held-out cases are private by design; the
repository holds only their ids and hashes.

Each rule's public case ids and latest results are in the behavioural spec
([`docs/trust/behavioral-spec.md`](docs/trust/behavioral-spec.md)).

## What it measures

Whether the assistant, on the sample course, answers course questions from the cited slide
or page, says "not covered" for anything the course does not contain, gives hints rather than
graded work, hands admin and personal requests to the teaching team, protects other learners'
data, never claims to be a person or to speak for the institution, and resists prompt
injection.

## Source material

The sample course in [`sample_course/`](sample_course/): three sessions (slides, pre-reads
and agendas), assessment briefs and a list of absent topics. It was written for this project
and contains no real programme material. Licence: CC BY 4.0
([`sample_course/LICENSE`](sample_course/LICENSE)).

## Authorship

- Cases were written before any prompt tuning, so they are not shaped by the system they
  measure.
- Each course question names its `gold_units`, the slide or page locators that support it
  (for example `s02:slides:9`). Builder-written cases were checked by a second reader (a fresh
  model verifier plus an owner spot-check).
- The adversarial extension (OWNER D-19, D-31) adds 3 paraphrases of each graded-work probe
  and each injection probe, written by a model outside the answer-model comparison.
  Multi-turn probes keep their turn structure.
- The public B9 supplement (D-84) adds 26 identity cases (`ta-b9-701` to `ta-b9-726`) in 18
  themes, such as role-play as the professor, "are you human", promising an extension and
  speaking for the team. An isolated author wrote them, opening nothing under
  `golden/heldout/` but their own source file. They sit on the public (tuning) side only,
  because the 5 public B9 cases all passed and gave nothing to tune identity on.
- Each held-out set is written by an isolated author who never sees an earlier set's text
  (D-74, D-82, D-88).

## Size and composition

455 cases: 240 public (tuning) and 215 held-out. The seeded split had 430; held-out case
`ta-b1-065` was retired because its question had appeared in a public test (D-54), and the
D-84 supplement added 26 public B9 cases. The held-out column is heldout-4's composition,
the same per rule as every earlier held-out set (D-88).

| Rule | What a case checks | Expected status | Public | Held-out | Total |
| --- | --- | --- | --- | --- | --- |
| B1 course questions (30 follow-ups in the plan) | Answered from the cited unit | `answered` | 74 | 75 | 149 |
| B2 absent topics | Not answered from general knowledge | `not_covered` | 15 | 15 | 30 |
| B7 planted absent topics | True in general, absent from the course | `not_covered` | 5 | 5 | 10 |
| B3 graded-work probes, with paraphrases (30 + 90) | A hint, never the work | `hint_only` | 64 | 56 | 120 |
| B4 admin and personal | Handed to the teaching team | `handed_over` | 10 | 10 | 20 |
| B8 other learners | Never reveals another learner's data | `not_covered` | 5 | 5 | 10 |
| B9 identity, with the D-84 supplement (5 + 26 public) | No false identity claim | `not_covered`, `answered` or `handed_over` | 31 | 5 | 36 |
| B10 injection, with paraphrases (20 + 60) | Rules intact, canary never appears | as for the carrier question | 36 | 44 | 80 |
| **Total** | | | **240** | **215** | **455** |

Public split by expected status: 100 `answered`, 68 `hint_only`, 53 `not_covered`, 19
`handed_over`. A B9 case may expect `handed_over`: an identity bait inside an admin request is
rightly handed over, and the identity gate judges only the false claim (D-84).

## Splits

- 50/50, stratified by rule and session, with a fixed seed.
- Split by group, not by case (D-32, D-34): every question about one unit, its follow-ups and
  the injections carried on it go to the same side; every probe for one assessment and its
  paraphrases go to the same side; cases sharing any question or earlier-turn text are merged.
  `python scripts/golden_tool.py check` refuses any held-out text found in the public split.
- Public cases: [`golden/public/cases.jsonl`](golden/public/cases.jsonl). Held-out:
  [`golden/heldout/MANIFEST.json`](golden/heldout/MANIFEST.json) only (ids, line hashes, file
  hash, corpus version and counts by rule).
- Prompts and thresholds are tuned on the public split only. A held-out set is scored once
  per verdict, then retired and replaced by a fresh set with the same composition (OWNER D-67,
  D-71). Sets so far: heldout-1 (scored 17 Nov 2026), heldout-2 (scored, D-77) and heldout-3 (scored,
  D-83), all retired and kept privately; heldout-4 is live and not scored yet (D-88).
- `golden_tool.py refresh` refuses a new set that copies or nearly copies a public or retired
  question, shares a unit with the public split, or changes the composition (ids only, D-74,
  D-82). `golden_tool.py extend` adds public cases on the tuning side only, with the same copy
  checks (D-84).
- Assessment solutions for the graded-work probes sit beside the held-out data and are never
  indexed.

## Corpus drift

Every case carries the `corpus_version` it was written against. When a course change removes
a case's gold units, the case is retired, or becomes an absent-topic case if the topic is now
gone. Every session needs at least 15 course-question cases and every assessment at least 3
graded-work probes before it can go live.

## Known gaps

- **One small course.** Three sessions in one subject, in English. Results do not show how the
  assistant does on long courses, other subjects, other languages or other file layouts.
- **Written questions.** Cases were written by the builder, not collected from learners, so
  they are cleaner than real questions. D-69 adds cases from verifier findings, flagged
  escalations and digest topics, with owner approval.
- **Small zero-tolerance sets.** With 5 held-out cases a rule (B8, B9), a perfect held-out
  score shows only a failure rate under about 45% with 95% confidence (exact binomial bound). The
  paraphrases raise B3 and B10 to 56 and 44 held-out cases. The D-84 supplement adds public B9
  cases only, so it helps tuning, not the held-out verdict.
- **Identity is monitored, not certified** (OWNER D-86). Five held-out B9 cases a set are too
  few for a kappa. A public check has the model panel label each B9 reply blind and adds one
  planted violation per case: judge and panel agreed 62 of 62. The planted replies are blatant,
  so this shows the judge catches clear claims and spares handovers, not that it gets subtle
  cases right.
- **Judging is calibrated against a model panel, not a person** (OWNER D-79). The calibration
  sets are labelled by gpt-6.1-terra and gpt-6-luna, with gpt-6.1-terra at high effort breaking
  a split, so kappa measures agreement with a model panel. All three come from the judge's
  vendor, so the panel's errors may lean the same way as the judge's. A person's labels can
  replace the panel's later.
- **A single judge** (OWNER D-80). gpt-6.1-sol alone grades every run: there is no
  cross-family check (D-12), no self-preference measure, and EvalKit publishes no headline
  from a single-judge run. The labellers are also the answer and check models. The dual judges
  return when a second vendor is funded, with a fresh calibration.
- **Three samples on zero-tolerance probes** (D-85). Integrity, identity and leak verdicts are
  the worst of three; the integrity calibration measured one sample, so the judge is now
  stricter than the calibrated one.

## Licence

The public golden cases, like the sample course they are written against, are released under
CC BY 4.0 (Tech Spec §8).
